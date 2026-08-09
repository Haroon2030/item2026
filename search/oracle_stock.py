"""
قراءة موجود المخزون والموردين ومبيعات نقاط البيع من أوراكل أونكس — SELECT فقط، بلا أي كتابة.

الجداول المستخدمة (قراءة):
- IAS_ITM_WCODE   : الكمية/التكلفة حسب المخزن
- المبيعات غير المرحلة (BILL_POST/POSTED=0) لحساب الرصيد المتوقع
- IAS_ITM_MST     : اسم الصنف والمجموعة وحالة Inactive
- IAS_ITM_DTL     : وحدات الصنف والعبوة والوحدة الرئيسية
- IAS_ITEM_PRICE  : أسعار البيع حسب المخزن/الوحدة
- WAREHOUSE_DETAILS : أسماء المخازن وربط الفرع (CONN_BRN_NO)
- IAS_PI_BILL_*   : فواتير الشراء (الموردون وآخر سعر توريد لكل مخزن)
- IAS_VNDR_ITM    : موردو الصنف المرتبطون
- V_DETAILS       : أسماء الموردين
- IAS_BILL_MST    : فواتير المبيعات / الآجل
- IAS_RT_BILL_MST : مرتجعات المبيعات (إن وُجدت)
- IAS_POS_BILL_MST / IAS_POS_RT_BILL_MST : فواتير ومرتجعات نقاط البيع (مخطط YSPOS)
- IAS_POS_BILL_DTL / IAS_POS_RT_BILL_DTL : تفاصيل أصناف نقاط البيع
- IAS_ITM_MST / GROUP_DETAILS : ربط الصنف بالمجموعة واسمها
- S_BRN / جداول الفروع : أسماء الفروع
- USER_R         : أسماء مستخدمي النظام (البائعون)
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

_WRITE_KW = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|CREATE|TRUNCATE|"
    r"GRANT|REVOKE|EXECUTE|CALL|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|"
    r"DECLARE|DBMS_|UTL_|EXECUTE\s+IMMEDIATE)\b",
    re.IGNORECASE,
)

_client_ready = False
_client_lock = threading.Lock()
_tls = threading.local()
_SALES_CACHE_TTL = 7200  # ساعتان — الداشبورد يعتمد على الكاش أولاً
_LOOKUP_CACHE_TTL = 60 * 60 * 24 * 7  # 7 أيام — خريطة الأصناف ثقيلة على WAN
_pool = None
_pool_lock = threading.Lock()
_pool_dsn = None
_pool_user = None
_pool_opts = None


class OracleStockError(Exception):
    """فشل قراءة المخزون من أوراكل."""


def _is_interpreter_shutdown_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return "interpreter shutdown" in msg or "cannot schedule" in msg


def _run_parallel(jobs: list, *, max_workers: int = 2, timeout_sec: float | None = None) -> list:
    """يشغّل دوال بلا وسيط متوازياً؛ عند إيقاف المفسّر (إعادة تحميل runserver) يعود تسلسلياً.

    مهلة واحدة إجمالية للدفعة، مع إيقاف فوري عند أول خطأ (مثل انقطاع أوراكل).
    """
    return _run_parallel_ex(
        jobs,
        max_workers=max_workers,
        timeout_sec=timeout_sec,
        soft_fail=False,
    )


def _run_parallel_ex(
    jobs: list,
    *,
    max_workers: int = 2,
    timeout_sec: float | None = None,
    soft_fail: bool = False,
) -> list:
    """parallel runner؛ soft_fail=True يُبقي النتائج الناجحة ويتجاهل فشل فرع واحد."""
    import sys
    from concurrent.futures import wait, FIRST_EXCEPTION, ALL_COMPLETED

    if not jobs:
        return []
    if len(jobs) == 1:
        try:
            return [jobs[0]()]
        except Exception:
            if soft_fail:
                return [[]]
            raise
    if getattr(sys, "is_finalizing", lambda: False)():
        out = []
        for fn in jobs:
            try:
                out.append(fn())
            except Exception:
                if soft_fail:
                    out.append([])
                else:
                    raise
        return out

    workers = max(1, min(int(max_workers or 1), len(jobs)))
    pool = None
    try:
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=workers)
        futs = [pool.submit(fn) for fn in jobs]
        if timeout_sec is None:
            if not soft_fail:
                return [fut.result() for fut in futs]
            out = []
            for fut in futs:
                try:
                    out.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Parallel soft-fail job: %s", exc)
                    out.append([])
            return out

        wait_s = max(5.0, float(timeout_sec))
        if soft_fail:
            done, not_done = wait(futs, timeout=wait_s, return_when=ALL_COMPLETED)
            out = []
            for fut in futs:
                if fut in not_done:
                    fut.cancel()
                    out.append([])
                    continue
                try:
                    out.append(fut.result(timeout=0))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Parallel soft-fail job: %s", exc)
                    out.append([])
            if not_done:
                logger.warning(
                    "Parallel soft-fail: %s/%s jobs timed out",
                    len(not_done),
                    len(futs),
                )
            # عند فشل الكل نُرجع قوائم فارغة ليتسنّى للمسار الأعلى تجربة بديل/كاش
            return out

        done, not_done = wait(futs, timeout=wait_s, return_when=FIRST_EXCEPTION)
        for fut in done:
            exc = fut.exception()
            if exc is not None:
                for other in not_done:
                    other.cancel()
                for other in done:
                    if other is not fut:
                        other.cancel()
                raise exc

        if not_done:
            for fut in not_done:
                fut.cancel()
            raise OracleStockError(
                "انتهت مهلة جلب البيانات من أوراكل. أعد المحاولة بعد لحظات."
            )

        _ = wait(futs, timeout=0, return_when=ALL_COMPLETED)
        return [fut.result() for fut in futs]
    except RuntimeError as exc:
        if _is_interpreter_shutdown_error(exc):
            logger.warning("Parallel jobs fell back to serial: %s", exc)
            return [fn() for fn in jobs]
        raise
    finally:
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                try:
                    pool.shutdown(wait=False)
                except Exception:
                    pass
            except Exception:
                pass


def _month_slices(date_from, date_to) -> list[tuple[date, date]]:
    """توافق — استخدم _month_spans."""
    return _month_spans(date_from, date_to)


def _merge_group_total_parts(parts: list, *, by_branch: bool) -> list[dict]:
    acc: dict[str, dict] = {}
    for rows in parts:
        for row in rows or []:
            key = _group_branch_key(
                str(row.get("group_code") or ""),
                str(row.get("branch_code") or "") if by_branch else "",
            )
            cur = acc.get(key)
            if cur is None:
                acc[key] = dict(row)
                continue
            cur["invoice_count"] = int(cur.get("invoice_count") or 0) + int(
                row.get("invoice_count") or 0
            )
            cur["return_count"] = int(cur.get("return_count") or 0) + int(
                row.get("return_count") or 0
            )
            cur["qty_total"] = float(cur.get("qty_total") or 0) + float(
                row.get("qty_total") or 0
            )
            cur["gross_total"] = float(cur.get("gross_total") or 0) + float(
                row.get("gross_total") or 0
            )
            cur["net_total"] = float(cur.get("net_total") or 0) + float(
                row.get("net_total") or 0
            )
            cur["vat_total"] = float(cur.get("vat_total") or 0) + float(
                row.get("vat_total") or 0
            )
            cur["sales_total"] = float(cur.get("sales_total") or 0) + float(
                row.get("sales_total") or 0
            )
    out = list(acc.values())
    for row in out:
        inv = int(row.get("invoice_count") or 0)
        sales = float(row.get("sales_total") or 0)
        row["avg_basket"] = round(sales / inv, 2) if inv else 0.0
        if not by_branch:
            row["branch_code"] = ""
            row["branch_name"] = "كل الفروع"
    out.sort(
        key=lambda r: (
            -float(r.get("sales_total") or 0),
            str(r.get("group_name") or ""),
            str(r.get("branch_name") or ""),
            str(r.get("group_code") or ""),
            str(r.get("branch_code") or ""),
        )
    )
    return out


def _is_connect_timeout(exc: BaseException) -> bool:
    text = str(exc or "").upper()
    return any(
        token in text
        for token in (
            "ORA-12170",
            "ORA-12541",
            "ORA-12543",
            "ORA-12535",
            "ORA-12547",
            "DPY-6005",
            "DPY-4011",
            "TIMEOUT",
            "TIMED OUT",
            "CANNOT CONNECT",
            "CONNECTION REFUSED",
            "NETWORK",
        )
    )


def _is_disconnect_error(exc: BaseException) -> bool:
    text = str(exc or "").upper()
    return any(
        token in text
        for token in (
            "DPY-1001",
            "DPI-1010",
            "DPI-1080",
            "ORA-03113",
            "ORA-03114",
            "ORA-03135",
            "ORA-00028",
            "ORA-01012",
            "NOT CONNECTED",
            "CONNECTION WAS CLOSED",
            "NOT LOGGED ON",
        )
    )


def _friendly_connect_error(exc: BaseException) -> str:
    if _is_disconnect_error(exc):
        return "انقطع الاتصال بأوراكل مؤقتاً. أعد المحاولة بعد لحظات."
    if _is_connect_timeout(exc):
        return (
            "تعذّر الاتصال بأوراكل (انتهت مهلة الشبكة). "
            "تحقق من VPN/الإنترنت أو أعد المحاولة بعد لحظات."
        )
    return f"تعذّر الاتصال بأوراكل: {exc}"


def _connect_kwargs() -> dict:
    """خيارات اتصال مشتركة للمجمّع والاتصال المباشر — بلا إعادة محاولة لتفادي البطء."""
    cfg = _cfg()
    tcp_timeout = max(5, int(cfg.get("TCP_CONNECT_TIMEOUT") or 20))
    call_ms = max(30_000, int(cfg.get("CALL_TIMEOUT_MS") or 120_000))
    return {
        "tcp_connect_timeout": tcp_timeout,
        "retry_count": 0,
        "retry_delay": 0,
        "call_timeout_ms": call_ms,
    }


def _apply_call_timeout(conn) -> None:
    """يمنع استعلاماً معلّقاً من إبقاء الطلب مفتوحاً عشرات الدقائق."""
    try:
        ms = int(_connect_kwargs().get("call_timeout_ms") or 120_000)
        conn.call_timeout = ms
    except Exception:
        pass


def _resolve_host_ipv4(host: str) -> str:
    """يفضّل IPv4 الصريح — يتجنّب تعثّر DDNS/IPv6 على بعض الشبكات."""
    import socket

    raw = str(host or "").strip()
    if not raw:
        return raw
    try:
        socket.inet_pton(socket.AF_INET, raw)
        return raw
    except OSError:
        pass
    try:
        infos = socket.getaddrinfo(raw, None, socket.AF_INET, socket.SOCK_STREAM)
        if infos:
            ip = str(infos[0][4][0] or "").strip()
            if ip:
                if ip != raw:
                    logger.info("Oracle host %s resolved to IPv4 %s", raw, ip)
                return ip
    except Exception as exc:  # noqa: BLE001
        logger.warning("Oracle host resolve failed for %s: %s", raw, exc)
    return raw


def oracle_enabled() -> bool:
    cfg = getattr(settings, "ORACLE", {}) or {}
    return bool(cfg.get("ENABLED"))


def use_oracle_stock() -> bool:
    source = (getattr(settings, "STOCK_QTY_SOURCE", "api") or "api").strip().lower()
    return oracle_enabled() and source == "oracle"


def _cfg() -> dict:
    return getattr(settings, "ORACLE", {}) or {}


def _assert_readonly_sql(sql: str) -> str:
    text = (sql or "").strip().rstrip(";")
    if not text:
        raise OracleStockError("استعلام فارغ.")
    head = text.lstrip().upper()
    if not (head.startswith("SELECT") or head.startswith("WITH")):
        raise OracleStockError("مرفوض: يُسمح بـ SELECT فقط (قراءة).")
    if _WRITE_KW.search(text):
        raise OracleStockError("مرفوض: الاستعلام يحتوي أوامر تعديل/إنشاء.")
    return text


def _init_thick_client() -> None:
    """تفعيل Thick mode — مطلوب لقواعد تستخدم مصادق كلمة سر قديمة (DPY-3015)."""
    global _client_ready
    if _client_ready:
        return
    with _client_lock:
        if _client_ready:
            return
        import platform
        import oracledb

        system = platform.system()
        lib_dir = str(_cfg().get("CLIENT_LIB_DIR") or "").strip()
        try:
            if system == "Windows":
                # على Windows يُمرَّر مسار Instant Client إن وُجد
                if lib_dir:
                    oracledb.init_oracle_client(lib_dir=lib_dir)
                else:
                    oracledb.init_oracle_client()
            elif system == "Darwin":
                if lib_dir:
                    oracledb.init_oracle_client(lib_dir=lib_dir)
                else:
                    oracledb.init_oracle_client()
            else:
                # Linux: لا يُمرَّر lib_dir — الاعتماد على ldconfig في الصورة
                oracledb.init_oracle_client()
            logger.info("Oracle thick mode ready (%s)", system)
        except Exception as exc:  # noqa: BLE001
            logger.error("Oracle thick client init failed: %s", exc)
            raise OracleStockError(
                "تعذّر تفعيل Oracle Thick mode. ثبّت Instant Client في السيرفر "
                "(Thin mode لا يدعم نوع كلمة السر الحالي — DPY-3015)."
            ) from exc
        _client_ready = True


def _oracle_dsn() -> tuple[str, str, str]:
    """يعيد (user, password, dsn) مع CONNECT_TIMEOUT داخل الوصف (Thick mode)."""
    cfg = _cfg()
    user = str(cfg.get("USER") or "").strip()
    password = str(cfg.get("PASSWORD") or "")
    host = _resolve_host_ipv4(str(cfg.get("HOST") or "").strip())
    port = int(cfg.get("PORT") or 1521)
    service = str(cfg.get("SERVICE_NAME") or "").strip()
    sid = str(cfg.get("SID") or "").strip()
    if not (user and password and host and (service or sid)):
        raise OracleStockError("إعدادات أوراكل غير مكتملة.")
    _init_thick_client()
    opts = _connect_kwargs()
    tcp_timeout = int(opts["tcp_connect_timeout"])
    retry_count = int(opts["retry_count"])
    retry_delay = int(opts["retry_delay"])
    if service:
        connect_data = f"(SERVICE_NAME={service})"
    else:
        connect_data = f"(SID={sid})"
    # CONNECT_TIMEOUT داخل الوصف يعمل مع Instant Client (Thick)
    dsn = (
        "(DESCRIPTION="
        "(ADDRESS="
        f"(PROTOCOL=TCP)(HOST={host})(PORT={port})"
        ")"
        f"(CONNECT_TIMEOUT={tcp_timeout})"
        f"(RETRY_COUNT={retry_count})"
        f"(RETRY_DELAY={retry_delay})"
        f"(CONNECT_DATA={connect_data})"
        ")"
    )
    return user, password, dsn


def _reset_pool() -> None:
    """إغلاق مجمع الاتصالات التالف لإعادة إنشائه لاحقاً."""
    global _pool, _pool_dsn, _pool_user, _pool_opts
    with _pool_lock:
        if _pool is None:
            return
        try:
            _pool.close(force=True)
        except Exception:
            pass
        _pool = None
        _pool_dsn = None
        _pool_user = None
        _pool_opts = None
        logger.warning("Oracle connection pool reset")


def _drop_conn(conn) -> None:
    """يتخلّص من اتصال ميت — لا يعيده للمجمّع كاتصال صالح."""
    if conn is None:
        return
    pool = getattr(conn, "_pool_ref", None)
    if pool is not None:
        try:
            pool.drop(conn)
            return
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass


def _get_pool():
    """مجمع اتصالات أوراكل مشترك — يقلّل زمن فتح الاتصال."""
    global _pool, _pool_dsn, _pool_user, _pool_opts
    user, password, dsn = _oracle_dsn()
    opts = _connect_kwargs()
    expire_time = max(1, int(_cfg().get("POOL_EXPIRE_TIME") or 4))
    pool_max = max(8, int(_cfg().get("POOL_MAX") or 12))
    ping_interval = int(_cfg().get("POOL_PING_INTERVAL") or 30)
    opts_key = (
        opts.get("tcp_connect_timeout"),
        expire_time,
        pool_max,
        ping_interval,
    )
    with _pool_lock:
        if (
            _pool is not None
            and _pool_dsn == dsn
            and _pool_user == user
            and _pool_opts == opts_key
        ):
            return _pool
        import oracledb

        if _pool is not None:
            try:
                _pool.close(force=True)
            except Exception:
                pass
            _pool = None
        try:
            pool_kwargs: dict[str, Any] = {
                "user": user,
                "password": password,
                "dsn": dsn,
                "min": 0,
                "max": pool_max,
                "increment": 1,
                "getmode": oracledb.POOL_GETMODE_WAIT,
                "wait_timeout": 30_000,
                "expire_time": expire_time,
                "tcp_connect_timeout": opts["tcp_connect_timeout"],
                "retry_count": 0,
                "retry_delay": 0,
                "ping_interval": ping_interval,
            }
            try:
                _pool = oracledb.create_pool(**pool_kwargs)
            except TypeError:
                pool_kwargs.pop("ping_interval", None)
                _pool = oracledb.create_pool(**pool_kwargs)
            _pool_dsn = dsn
            _pool_user = user
            _pool_opts = opts_key
            logger.info(
                "Oracle connection pool ready (max=%s, tcp=%ss, ping=%ss)",
                pool_max,
                opts["tcp_connect_timeout"],
                ping_interval,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Oracle pool create failed: %s", exc)
            _pool = None
            _pool_dsn = None
            _pool_user = None
            _pool_opts = None
        return _pool


def _connect():
    """اتصال أوراكل مرة واحدة — بلا حلقات إعادة محاولة."""
    import oracledb

    opts = _connect_kwargs()
    pool = _get_pool()
    if pool is not None:
        try:
            conn = pool.acquire()
            setattr(conn, "_from_pool", True)
            setattr(conn, "_pool_ref", pool)
            _apply_call_timeout(conn)
            return conn
        except Exception as exc:  # noqa: BLE001
            logger.warning("Oracle pool acquire failed: %s", exc)
            if _is_connect_timeout(exc):
                _reset_pool()
    try:
        user, password, dsn = _oracle_dsn()
        conn = oracledb.connect(
            user=user,
            password=password,
            dsn=dsn,
            tcp_connect_timeout=opts["tcp_connect_timeout"],
            retry_count=0,
            retry_delay=0,
        )
        setattr(conn, "_from_pool", False)
        setattr(conn, "_pool_ref", None)
        _apply_call_timeout(conn)
        return conn
    except Exception as exc:  # noqa: BLE001
        raise OracleStockError(_friendly_connect_error(exc)) from exc


def _release_conn(conn) -> None:
    if conn is None:
        return
    pool = getattr(conn, "_pool_ref", None)
    if pool is not None:
        try:
            pool.release(conn)
            return
        except Exception:
            try:
                pool.drop(conn)
                return
            except Exception:
                pass
    try:
        conn.close()
    except Exception:
        pass


def _schema() -> str:
    schema = str(_cfg().get("SCHEMA") or "").strip()
    if not schema:
        raise OracleStockError("ORACLE_SCHEMA مطلوب.")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", schema):
        raise OracleStockError("ORACLE_SCHEMA غير صالح.")
    return schema


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _date_span_days(date_from, date_to) -> int:
    return (_as_date(date_to) - _as_date(date_from)).days + 1


# فترات ≥14 يوم: تأجيل لوحات ثقيلة في الواجهة فقط — الأرقام دائماً صافي بعد المرتجعات
_LONG_RANGE_MIN_DAYS = 14


def _use_fast_sales(date_from, date_to) -> bool:
    """سابقاً: تخطّي مرتجعات. أُلغي — المالك يحتاج أرقاماً صافية دائماً."""
    return False


def sales_fast_mode(date_from, date_to) -> bool:
    """لا يتخطى المرتجعات؛ للتوافق فقط (دائماً False)."""
    return False


def sales_long_range(date_from, date_to) -> bool:
    """فترة طويلة → تحميل تصاعدي/مؤجّل في الواجهة مع الإبقاء على خصم المرتجعات."""
    return _date_span_days(date_from, date_to) >= _LONG_RANGE_MIN_DAYS


def _skip_mst_returns(date_from, date_to) -> bool:
    """لا يُتخطى خصم مرتجعات رأس الفاتورة أبداً."""
    return False


def _date_params(date_from, date_to) -> dict[str, date]:
    """حدود تاريخ نصف مفتوحة [from, to+1) لاستخدام فهارس BILL_DATE."""
    d_from = _as_date(date_from)
    d_to_excl = _as_date(date_to) + timedelta(days=1)
    return {"d_from": d_from, "d_to_excl": d_to_excl}


def _bind_brn(branch_code: str):
    """يربط رقم الفرع كرقم إن أمكن — أفضل لاستخدام فهرس BRN_NO."""
    s = str(branch_code or "").strip()
    if not s:
        return s
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    return s


def _bind_gcode(group_code: str):
    s = str(group_code or "").strip()
    if not s:
        return s
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return s
    return s


def _sales_cache_ttl(date_from=None, date_to=None) -> int:
    return _SALES_CACHE_TTL


def _hung_ok(alias: str = "p") -> str:
    """شرط مبيعات غير معلّقة — يفضّل الفهارس على NVL(col,0)."""
    return f"({alias}.HUNG IS NULL OR {alias}.HUNG = 0)"


def _pos_mst_ok(alias: str = "p") -> str:
    """POS رأس فاتورة: غير معلّق + مبلغ منطقي (يستبعد بيانات تالفة)."""
    return f"{_hung_ok(alias)} AND {_bill_amt_ok(alias, 'BILL_AMT')}"


def _cncl_ok(alias: str = "b") -> str:
    return f"({alias}.CNCL_FLG IS NULL OR {alias}.CNCL_FLG = 0)"


# سقف منطقي لفاتورة واحدة — يستبعد بيانات تالفة (وُجدت فاتورة بـ ~18 تريليون)
_MAX_SANE_BILL_AMT = 5_000_000.0


def _bill_amt_ok(alias: str = "b", column: str = "BILL_AMT") -> str:
    """يستبعد فواتير بمبالغ غير منطقية تفسد الإجماليات."""
    return f"ABS(NVL({alias}.{column}, 0)) <= {_MAX_SANE_BILL_AMT}"


def _bill_mst_ok(alias: str = "b") -> str:
    return f"{_cncl_ok(alias)} AND {_bill_amt_ok(alias, 'BILL_AMT')}"


def _rt_bill_mst_ok(alias: str = "r", amount_col: str = "BILL_AMT") -> str:
    return f"{_cncl_ok(alias)} AND {_bill_amt_ok(alias, amount_col)}"


def _django_lookup_get(key: str):
    hit, cached = _lookup_cache_get(key)
    if hit:
        return True, cached
    try:
        val = cache.get(f"sales:lookup:{key}")
    except Exception:
        val = None
    if val is not None:
        _lookup_cache_set(key, val)
        return True, val
    return False, None


def _django_lookup_set(key: str, value: Any) -> Any:
    _lookup_cache_set(key, value)
    try:
        cache.set(f"sales:lookup:{key}", value, _LOOKUP_CACHE_TTL)
    except Exception:
        pass
    return value


@contextmanager
def oracle_session() -> Iterator[None]:
    """اتصال أوراكل واحد + كاش lookups طوال كتلة with."""
    depth = int(getattr(_tls, "depth", 0) or 0)
    if depth == 0:
        _tls.conn = _connect()
        _tls.lookup_cache = {}
    _tls.depth = depth + 1
    try:
        yield
    finally:
        _tls.depth = int(getattr(_tls, "depth", 1) or 1) - 1
        if _tls.depth <= 0:
            conn = getattr(_tls, "conn", None)
            _tls.conn = None
            _tls.lookup_cache = {}
            _tls.depth = 0
            _release_conn(conn)


def _lookup_cache_get(key: str):
    cache_map = getattr(_tls, "lookup_cache", None)
    if isinstance(cache_map, dict) and key in cache_map:
        return True, cache_map[key]
    return False, None


def _lookup_cache_set(key: str, value: Any) -> Any:
    cache_map = getattr(_tls, "lookup_cache", None)
    if isinstance(cache_map, dict):
        cache_map[key] = value
    return value


def _fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    safe_sql = _assert_readonly_sql(sql)
    owned = False
    conn = getattr(_tls, "conn", None)
    if conn is None:
        conn = _connect()
        if int(getattr(_tls, "depth", 0) or 0) > 0:
            _tls.conn = conn
        else:
            owned = True
    try:
        cur = conn.cursor()
        try:
            cur.arraysize = 2000
        except Exception:
            pass
        cur.execute(safe_sql, params or {})
        cols = [d[0].upper() for d in (cur.description or [])]
        rows = []
        for tup in cur:
            rows.append({cols[i]: tup[i] for i in range(len(cols))})
        return rows
    except Exception as exc:  # noqa: BLE001
        if owned:
            _drop_conn(conn)
            owned = False
        elif _is_disconnect_error(exc) or _is_connect_timeout(exc):
            dead = getattr(_tls, "conn", None)
            _tls.conn = None
            _drop_conn(dead)
        if _is_connect_timeout(exc):
            raise OracleStockError(_friendly_connect_error(exc)) from exc
        raise
    finally:
        if owned:
            _release_conn(conn)


def _sales_cache_get(key: str):
    try:
        return cache.get(key)
    except Exception:
        return None


def _sales_cache_get_stale(key: str):
    try:
        return cache.get(f"{key}:stale")
    except Exception:
        return None


def pop_groups_fetch_warning() -> str:
    """تحذير واجهة بعد جلب مجموعات (مثلاً كاش قديم) — يُستهلك مرة واحدة."""
    msg = str(getattr(_tls, "groups_warning", "") or "").strip()
    try:
        _tls.groups_warning = ""
        _tls.groups_stale = False
    except Exception:
        pass
    return msg


def pop_groups_incomplete() -> bool:
    """هل نتيجة المجموعات جزئية (شهور ناقصة) — يُستهلك مرة واحدة."""
    flag = bool(getattr(_tls, "groups_incomplete", False))
    try:
        _tls.groups_incomplete = False
    except Exception:
        pass
    return flag


def _mark_groups_partial(warning: str) -> None:
    try:
        _tls.groups_stale = True
        _tls.groups_incomplete = True
        _tls.groups_warning = warning
    except Exception:
        pass


def _sales_cache_set(
    key: str,
    value: Any,
    ttl: int | None = None,
    *,
    date_from=None,
    date_to=None,
    keep_stale: bool = True,
) -> None:
    try:
        if ttl is None:
            ttl = _sales_cache_ttl(date_from, date_to)
        cache.set(key, value, ttl)
        if keep_stale:
            # نسخة احتياطية أطول عند انقطاع أوراكل
            cache.set(f"{key}:stale", value, max(int(ttl), 86400))
    except Exception:
        pass


def _pick_cost(row: dict) -> Any:
    return row.get("COST") if row.get("COST") is not None else row.get("I_CWTAVG")


def _row_to_stock(row: dict) -> dict:
    code = str(row.get("I_CODE") or "").strip()
    qty = row.get("QTY")
    if qty is None:
        qty = row.get("AVL_QTY")
    cost = _pick_cost(row)
    return {
        "code": code,
        "name": str(row.get("I_NAME") or "").strip(),
        "unit": str(row.get("ITM_UNT") or "").strip(),
        "quantity": "" if qty is None else str(qty),
        "avg_cost": "" if cost is None else str(cost),
        "cost": "" if cost is None else str(cost),
        "barcode": "",
        "inactive": int(row.get("INACTIVE") or 0),
        "_source": "oracle",
    }


def fetch_oracle_group_stock(warehouse: str, group_code: str) -> list[dict]:
    """
    صفوف مخزون مجموعة بمخزن بكمية > 0 من أوراكل، بما فيها غير النشط.
    صف لكل وحدة تخزين — مثل تقرير أونكس (الصنف بوحدتين يظهر مرتين).
    SELECT فقط.
    """
    schema = _schema()
    wh = str(warehouse or "").strip()
    g = str(group_code or "").strip()
    if not wh or not g:
        raise OracleStockError("المخزن والمجموعة مطلوبان.")

    sql = f"""
        SELECT
            w.I_CODE AS I_CODE,
            m.I_NAME AS I_NAME,
            w.ITM_UNT AS ITM_UNT,
            w.AVL_QTY AS QTY,
            NVL(w.I_CWTAVG, w.PRIMARY_COST) AS COST,
            NVL(m.INACTIVE, 0) AS INACTIVE
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        WHERE TO_CHAR(w.W_CODE) = TO_CHAR(:wh)
          AND TO_CHAR(m.G_CODE) = TO_CHAR(:g)
          AND NVL(w.AVL_QTY, 0) > 0
        ORDER BY m.I_NAME, w.I_CODE, NVL(w.P_SIZE, 1), w.ITM_UNT
    """
    rows = _fetch_all(sql, {"wh": wh, "g": g})
    return [_row_to_stock(r) for r in rows]


def count_oracle_group_catalog(warehouse: str, group_code: str) -> tuple[int, int]:
    """(عدد صفوف المجموعة في المخزن، عدد الصفوف بلا رصيد). SELECT فقط."""
    schema = _schema()
    sql = f"""
        SELECT
            COUNT(*) AS CATALOG_COUNT,
            SUM(CASE WHEN NVL(w.AVL_QTY, 0) > 0 THEN 0 ELSE 1 END) AS ZERO_COUNT
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        WHERE TO_CHAR(w.W_CODE) = TO_CHAR(:wh)
          AND TO_CHAR(m.G_CODE) = TO_CHAR(:g)
    """
    rows = _fetch_all(sql, {"wh": warehouse, "g": group_code})
    if not rows:
        return 0, 0
    return int(rows[0].get("CATALOG_COUNT") or 0), int(rows[0].get("ZERO_COUNT") or 0)


def fetch_warehouse_options(*, active_only: bool = True) -> list[dict]:
    """قائمة المخازن من WAREHOUSE_DETAILS مع فرع الربط."""
    if not oracle_enabled():
        return []
    cache_key = f"inv:wh_options:v1:{int(active_only)}"
    hit, cached = _django_lookup_get(cache_key)
    if hit:
        return cached
    schema = _schema()
    active_filter = "AND NVL(w.INACTIVE, 0) = 0" if active_only else ""
    try:
        rows = _fetch_all(
            f"""
            SELECT
                TO_CHAR(w.W_CODE) AS W_CODE,
                w.W_NAME,
                w.W_E_NAME,
                TO_CHAR(w.CONN_BRN_NO) AS BRANCH_CODE,
                NVL(w.INACTIVE, 0) AS INACTIVE
            FROM {schema}.WAREHOUSE_DETAILS w
            WHERE w.W_CODE IS NOT NULL
              {active_filter}
            ORDER BY w.W_NAME, w.W_CODE
            """
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Warehouse options failed: %s", exc)
        raise
    names = _branch_names()
    out: list[dict] = []
    for row in rows:
        code = str(row.get("W_CODE") or "").strip()
        if not code:
            continue
        brn = str(row.get("BRANCH_CODE") or "").strip()
        name = str(row.get("W_NAME") or row.get("W_E_NAME") or "").strip() or code
        out.append(
            {
                "code": code,
                "name": name,
                "branch_code": brn,
                "branch_name": names.get(brn) or brn or "—",
                "inactive": int(row.get("INACTIVE") or 0),
            }
        )
    return _django_lookup_set(cache_key, out)


def _pending_pos_net_sql() -> str:
    """
    جدول فرعي لكل صنف×مخزن:
    - QTY: صافي كمية غير مرحلة بوحدة المخزون (مبيعات − مرتجع)
    """
    pos = _pos_owner()
    days = int(_PENDING_SALES_LOOKBACK_DAYS)
    qty = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
    return f"""
    (
      SELECT I_CODE, W_CODE,
             ROUND(SUM(QTY), 4) AS QTY
      FROM (
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
               SUM({qty}) AS QTY
        FROM {pos}.IAS_POS_BILL_DTL d
        JOIN {pos}.IAS_POS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO
         AND m.BRN_NO = d.BRN_NO
         AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
        WHERE NVL(m.POSTED, 0) = 0
          AND NVL(m.HUNG, 0) = 0
          AND m.BILL_DATE >= TRUNC(SYSDATE) - {days}
          AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
        GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
        UNION ALL
        SELECT TO_CHAR(d.I_CODE) AS I_CODE,
               TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
               -SUM({qty}) AS QTY
        FROM {pos}.IAS_POS_RT_BILL_DTL d
        JOIN {pos}.IAS_POS_RT_BILL_MST m
          ON m.RT_BILL_NO = d.RT_BILL_NO
         AND m.BRN_NO = d.BRN_NO
        WHERE NVL(m.POSTED, 0) = 0
          AND NVL(m.HUNG, 0) = 0
          AND m.RT_BILL_DATE >= TRUNC(SYSDATE) - {days}
          AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
        GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
      )
      GROUP BY I_CODE, W_CODE
    )
    """


def _inv_expected_qty_sql(alias_w: str = "w", alias_pend: str = "pend") -> str:
    """كمية بعد خصم غير المرحّل: لا تقل عن صفر ولا تزيد عن الرصيد."""
    return (
        f"GREATEST(0, NVL({alias_w}.AVL_QTY, 0) - "
        f"GREATEST(0, NVL({alias_pend}.QTY, 0)))"
    )


def _inventory_stock_filters(
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> tuple[str, dict]:
    """فلاتر مشتركة لتحليل المخزون — كمية موجبة فقط."""
    params: dict = {}
    filters = ["NVL(w.AVL_QTY, 0) > 0"]
    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()
    if wh:
        params["wh"] = wh
        filters.append("TO_CHAR(w.W_CODE) = :wh")
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        filters.append("m.G_CODE = :gcode")
    if brn:
        params["brn"] = _bind_brn(brn)
        filters.append("wh.CONN_BRN_NO = :brn")
    return " AND ".join(filters), params


def _fmt_inv_money(value: float) -> str:
    return f"{float(value or 0):,.2f}"


def _fmt_inv_qty(value: float) -> str:
    num = float(value or 0)
    if abs(num - round(num)) < 1e-9:
        return f"{int(round(num)):,}"
    return f"{num:,.2f}"


def _assemble_inventory_rows(
    rows: list[dict],
    *,
    key_field: str,
    name_lookup: dict[str, str] | None = None,
    extra_name_field: str | None = None,
) -> list[dict]:
    """صفوف تحليل مخزون موحّدة مع حصة من الإجمالي."""
    out: list[dict] = []
    total_value = 0.0
    for row in rows:
        code = str(row.get(key_field) or "").strip() or "(بلا)"
        value = round(float(row.get("STOCK_VALUE") or 0), 2)
        qty = round(float(row.get("QTY_TOTAL") or 0), 2)
        items = int(row.get("ITEM_COUNT") or 0)
        stock_rows = int(row.get("ROW_COUNT") or 0)
        wh_count = int(row.get("WH_COUNT") or 0)
        stock_before = round(float(row.get("STOCK_BEFORE") or 0), 2)
        pending_cost = round(float(row.get("PENDING_COST") or 0), 2)
        pending_qty = round(float(row.get("PENDING_QTY") or 0), 2)
        name = ""
        if extra_name_field:
            name = str(row.get(extra_name_field) or "").strip()
        if not name and name_lookup is not None:
            name = name_lookup.get(code) or ""
        if not name:
            name = code
        total_value += value
        out.append(
            {
                "code": code,
                "name": name,
                "stock_value": value,
                "qty_total": qty,
                "item_count": items,
                "row_count": stock_rows,
                "warehouse_count": wh_count,
                "stock_before": stock_before,
                "pending_cost": pending_cost,
                "pending_qty": pending_qty,
                "stock_value_display": _fmt_inv_money(value),
                "stock_before_display": _fmt_inv_money(stock_before),
                "pending_cost_display": _fmt_inv_money(pending_cost),
                "pending_qty_display": _fmt_inv_qty(pending_qty),
                "qty_display": _fmt_inv_qty(qty),
                "item_count_display": f"{items:,}",
                "row_count_display": f"{stock_rows:,}",
            }
        )
    out.sort(key=lambda r: (-r["stock_value"], r["name"], r["code"]))
    for row in out:
        share = (row["stock_value"] / total_value * 100.0) if total_value else 0.0
        row["share_pct"] = round(share, 1)
        row["share_display"] = f"{share:.1f}%"
    return out


def _inventory_value_select_sql(qty_sql: str) -> str:
    """أعمدة قيمة قبل/بعد الترحيل + غير المرحّل (تكلفة وكمية)."""
    cost = "NVL(w.I_CWTAVG, w.PRIMARY_COST)"
    return f"""
            ROUND(SUM({qty_sql}), 2) AS QTY_TOTAL,
            ROUND(SUM({qty_sql} * {cost}), 2) AS STOCK_VALUE,
            ROUND(SUM(NVL(w.AVL_QTY, 0) * {cost}), 2) AS STOCK_BEFORE,
            ROUND(
              SUM(GREATEST(0, NVL(pend.QTY, 0)) * {cost}),
              2
            ) AS PENDING_COST,
            ROUND(SUM(GREATEST(0, NVL(pend.QTY, 0))), 2) AS PENDING_QTY
    """


def fetch_inventory_by_warehouse(
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> list[dict]:
    """إجماليات المخزون حسب المخزن — قيمة بعد خصم مبيعات POS غير المرحلة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    schema = _schema()
    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()
    cache_key = f"inv:by_wh:v6:{wh}:{gcode}:{brn}"
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    where, params = _inventory_stock_filters(
        warehouse=wh, group_code=gcode, branch_code=brn
    )
    qty_sql = _inv_expected_qty_sql()
    pend_sql = _pending_pos_net_sql()
    value_cols = _inventory_value_select_sql(qty_sql)
    rows = _fetch_all(
        f"""
        SELECT
            TO_CHAR(w.W_CODE) AS WAREHOUSE_CODE,
            MAX(NVL(wh.W_NAME, TO_CHAR(w.W_CODE))) AS WAREHOUSE_NAME,
            TO_CHAR(MAX(wh.CONN_BRN_NO)) AS BRANCH_CODE,
            COUNT(*) AS ROW_COUNT,
            COUNT(DISTINCT w.I_CODE) AS ITEM_COUNT,
            {value_cols}
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
        LEFT JOIN {pend_sql} pend
          ON pend.I_CODE = TO_CHAR(w.I_CODE)
         AND pend.W_CODE = TO_CHAR(w.W_CODE)
        WHERE {where}
        GROUP BY TO_CHAR(w.W_CODE)
        HAVING ROUND(SUM({qty_sql}), 2) > 0
        """,
        params,
    )
    branch_names = _branch_names()
    branch_by_wh = {
        str(r.get("WAREHOUSE_CODE") or "").strip(): str(r.get("BRANCH_CODE") or "").strip()
        for r in rows
    }
    assembled = _assemble_inventory_rows(
        rows,
        key_field="WAREHOUSE_CODE",
        extra_name_field="WAREHOUSE_NAME",
    )
    for row in assembled:
        brn_code = branch_by_wh.get(row["code"], "")
        row["branch_code"] = brn_code
        row["branch_name"] = branch_names.get(brn_code) or brn_code or "—"
    _sales_cache_set(cache_key, assembled, ttl=1800)
    return assembled


def fetch_inventory_by_group(
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> list[dict]:
    """إجماليات المخزون حسب المجموعة — بعد خصم مبيعات POS غير المرحلة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    schema = _schema()
    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()
    cache_key = f"inv:by_group:v6:{wh}:{gcode}:{brn}"
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    where, params = _inventory_stock_filters(
        warehouse=wh, group_code=gcode, branch_code=brn
    )
    qty_sql = _inv_expected_qty_sql()
    pend_sql = _pending_pos_net_sql()
    value_cols = _inventory_value_select_sql(qty_sql)
    rows = _fetch_all(
        f"""
        SELECT
            NVL(TO_CHAR(m.G_CODE), '(بلا)') AS GROUP_CODE,
            COUNT(*) AS ROW_COUNT,
            COUNT(DISTINCT w.I_CODE) AS ITEM_COUNT,
            COUNT(DISTINCT TO_CHAR(w.W_CODE)) AS WH_COUNT,
            {value_cols}
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
        LEFT JOIN {pend_sql} pend
          ON pend.I_CODE = TO_CHAR(w.I_CODE)
         AND pend.W_CODE = TO_CHAR(w.W_CODE)
        WHERE {where}
        GROUP BY NVL(TO_CHAR(m.G_CODE), '(بلا)')
        HAVING ROUND(SUM({qty_sql}), 2) > 0
        """,
        params,
    )
    group_names = {
        str(g.get("code") or "").strip(): str(g.get("name") or "").strip()
        for g in fetch_sales_group_options()
        if str(g.get("code") or "").strip()
    }
    assembled = _assemble_inventory_rows(
        rows, key_field="GROUP_CODE", name_lookup=group_names
    )
    _sales_cache_set(cache_key, assembled, ttl=1800)
    return assembled


def fetch_inventory_by_branch(
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> list[dict]:
    """إجماليات المخزون حسب الفرع — بعد خصم مبيعات POS غير المرحلة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    schema = _schema()
    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()
    cache_key = f"inv:by_brn:v6:{wh}:{gcode}:{brn}"
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    where, params = _inventory_stock_filters(
        warehouse=wh, group_code=gcode, branch_code=brn
    )
    qty_sql = _inv_expected_qty_sql()
    pend_sql = _pending_pos_net_sql()
    value_cols = _inventory_value_select_sql(qty_sql)
    rows = _fetch_all(
        f"""
        SELECT
            NVL(TO_CHAR(wh.CONN_BRN_NO), '(بلا)') AS BRANCH_CODE,
            COUNT(*) AS ROW_COUNT,
            COUNT(DISTINCT w.I_CODE) AS ITEM_COUNT,
            COUNT(DISTINCT TO_CHAR(w.W_CODE)) AS WH_COUNT,
            {value_cols}
        FROM {schema}.IAS_ITM_WCODE w
        JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
        LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
          ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
        LEFT JOIN {pend_sql} pend
          ON pend.I_CODE = TO_CHAR(w.I_CODE)
         AND pend.W_CODE = TO_CHAR(w.W_CODE)
        WHERE {where}
        GROUP BY NVL(TO_CHAR(wh.CONN_BRN_NO), '(بلا)')
        HAVING ROUND(SUM({qty_sql}), 2) > 0
        """,
        params,
    )
    assembled = _assemble_inventory_rows(
        rows, key_field="BRANCH_CODE", name_lookup=_branch_names()
    )
    _sales_cache_set(cache_key, assembled, ttl=1800)
    return assembled


def fetch_stagnant_items(
    date_from,
    date_to,
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
    limit: int = 15,
) -> list[dict]:
    """أصناف بأعلى كمية بعد الترحيل وأقل/بلا حركة مبيعات POS في الفترة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    schema = _schema()
    pos = _pos_owner()
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()
    lim = max(1, min(int(limit or 15), 30))
    cache_key = (
        f"inv:stagnant_items:v2:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{wh}:{gcode}:{brn}:{lim}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    where, params = _inventory_stock_filters(
        warehouse=wh, group_code=gcode, branch_code=brn
    )
    qty_sql = _inv_expected_qty_sql()
    pend_sql = _pending_pos_net_sql()
    # مرشحون: أعلى كمية أولاً ثم نستبعد من لهم مبيعات
    candidate_lim = max(lim * 12, 120)
    stock_rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT
              TO_CHAR(w.I_CODE) AS ITEM_CODE,
              MAX(NVL(m.I_NAME, TO_CHAR(w.I_CODE))) AS ITEM_NAME,
              ROUND(SUM({qty_sql}), 2) AS QTY_TOTAL,
              ROUND(
                SUM({qty_sql} * NVL(w.I_CWTAVG, w.PRIMARY_COST)),
                2
              ) AS STOCK_VALUE
          FROM {schema}.IAS_ITM_WCODE w
          JOIN {schema}.IAS_ITM_MST m ON m.I_CODE = w.I_CODE
          LEFT JOIN {schema}.WAREHOUSE_DETAILS wh
            ON TO_CHAR(wh.W_CODE) = TO_CHAR(w.W_CODE)
          LEFT JOIN {pend_sql} pend
            ON pend.I_CODE = TO_CHAR(w.I_CODE)
           AND pend.W_CODE = TO_CHAR(w.W_CODE)
          WHERE {where}
          GROUP BY TO_CHAR(w.I_CODE)
          HAVING ROUND(SUM({qty_sql}), 2) > 0
          ORDER BY QTY_TOTAL DESC, STOCK_VALUE DESC
        ) WHERE ROWNUM <= :cand_lim
        """,
        {**params, "cand_lim": candidate_lim},
    )
    if not stock_rows:
        _sales_cache_set(cache_key, [], ttl=1800)
        return []

    item_codes = [
        str(r.get("ITEM_CODE") or "").strip()
        for r in stock_rows
        if str(r.get("ITEM_CODE") or "").strip()
    ]
    sales_params: dict[str, Any] = {
        "d_from": d_from,
        "d_to_excl": d_to + timedelta(days=1),
    }
    sales_filters = [
        "m.BILL_DATE >= :d_from",
        "m.BILL_DATE < :d_to_excl",
        "NVL(m.HUNG, 0) = 0",
        "d.I_CODE IS NOT NULL",
    ]
    ret_filters = [
        "m.RT_BILL_DATE >= :d_from",
        "m.RT_BILL_DATE < :d_to_excl",
        "NVL(m.HUNG, 0) = 0",
        "d.I_CODE IS NOT NULL",
    ]
    if brn:
        sales_params["brn"] = _bind_brn(brn)
        sales_filters.append("m.BRN_NO = :brn")
        ret_filters.append("m.BRN_NO = :brn")
    if wh:
        sales_params["wh"] = wh
        sales_filters.append("TO_CHAR(NVL(d.W_CODE, m.W_CODE)) = :wh")
        ret_filters.append("TO_CHAR(NVL(d.W_CODE, m.W_CODE)) = :wh")
    item_filter = _item_in_filter("d.I_CODE", item_codes, sales_params)
    qty_expr = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"

    sales_qty: dict[str, float] = {}
    for row in _fetch_all(
        f"""
        SELECT
            TO_CHAR(d.I_CODE) AS ITEM_CODE,
            ROUND(SUM({qty_expr}), 4) AS SALES_QTY
        FROM {pos}.IAS_POS_BILL_DTL d
        JOIN {pos}.IAS_POS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO
         AND m.BRN_NO = d.BRN_NO
         AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
        WHERE {" AND ".join(sales_filters)}
          {item_filter}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        sales_params,
    ):
        code = str(row.get("ITEM_CODE") or "").strip()
        if code:
            sales_qty[code] = float(row.get("SALES_QTY") or 0)

    ret_params = dict(sales_params)
    ret_item_filter = _item_in_filter("d.I_CODE", item_codes, ret_params)
    for row in _fetch_all(
        f"""
        SELECT
            TO_CHAR(d.I_CODE) AS ITEM_CODE,
            ROUND(SUM({qty_expr}), 4) AS RET_QTY
        FROM {pos}.IAS_POS_RT_BILL_DTL d
        JOIN {pos}.IAS_POS_RT_BILL_MST m
          ON m.RT_BILL_NO = d.RT_BILL_NO
         AND m.BRN_NO = d.BRN_NO
        WHERE {" AND ".join(ret_filters)}
          {ret_item_filter}
        GROUP BY TO_CHAR(d.I_CODE)
        """,
        ret_params,
    ):
        code = str(row.get("ITEM_CODE") or "").strip()
        if code:
            sales_qty[code] = float(sales_qty.get(code, 0)) - float(
                row.get("RET_QTY") or 0
            )

    out: list[dict] = []
    for row in stock_rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        if not code:
            continue
        moved = float(sales_qty.get(code, 0) or 0)
        # الأقل حركة: بلا صافي مبيعات خلال الفترة
        if moved > 0:
            continue
        qty = round(float(row.get("QTY_TOTAL") or 0), 2)
        value = round(float(row.get("STOCK_VALUE") or 0), 2)
        if qty <= 0:
            continue
        out.append(
            {
                "code": code,
                "name": str(row.get("ITEM_NAME") or "").strip() or code,
                "qty_total": qty,
                "qty_display": _fmt_inv_qty(qty),
                "stock_value": value,
                "stock_value_display": _fmt_inv_money(value),
                "sales_qty": round(moved, 2),
            }
        )
        if len(out) >= lim:
            break

    total_qty = round(sum(r["qty_total"] for r in out), 2)
    for row in out:
        share = (row["qty_total"] / total_qty * 100.0) if total_qty else 0.0
        row["share_pct"] = round(share, 1)
        row["share_display"] = f"{share:.1f}%"

    _sales_cache_set(cache_key, out, ttl=1800)
    return out


def fetch_inventory_wastage(
    date_from,
    date_to,
    *,
    warehouse: str = "",
    group_code: str = "",
    branch_code: str = "",
) -> dict[str, Any]:
    """توالف الصرف المخزني من حساب 41101006، مجمعة حسب الفرع والمجموعة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    schema = _schema()
    d_from = _as_date(date_from)
    d_to = _as_date(date_to)
    if d_from > d_to:
        raise OracleStockError("تاريخ البداية بعد النهاية.")

    wh = str(warehouse or "").strip()
    gcode = str(group_code or "").strip()
    brn = str(branch_code or "").strip()
    cache_key = (
        f"inv:wastage:v3:{d_from.isoformat()}:{d_to.isoformat()}:"
        f"{wh}:{gcode}:{brn}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    filters = [
        "TO_CHAR(m.A_CODE) = '41101006'",
        "m.OUT_DATE >= :d_from",
        "m.OUT_DATE < :d_to_excl",
    ]
    params: dict[str, Any] = {
        "d_from": d_from,
        "d_to_excl": d_to + timedelta(days=1),
    }
    if wh:
        filters.append("TO_CHAR(m.W_CODE) = :wh")
        params["wh"] = wh
    if gcode:
        filters.append("i.G_CODE = :gcode")
        params["gcode"] = _bind_gcode(gcode)
    if brn:
        filters.append("m.BRN_NO = :brn")
        params["brn"] = _bind_brn(brn)

    rows = _fetch_all(
        f"""
        SELECT
            NVL(TO_CHAR(m.BRN_NO), '(بلا)') AS BRANCH_CODE,
            NVL(TO_CHAR(i.G_CODE), '(بلا)') AS GROUP_CODE,
            COUNT(DISTINCT m.OUT_SER) AS DOC_COUNT,
            ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
            ROUND(SUM(NVL(d.I_QTY, 0) * NVL(d.STK_COST, 0)), 2) AS WASTE_VALUE
        FROM {schema}.IAS_OUTGOING_MST m
        JOIN {schema}.IAS_OUTGOING_DTL d
          ON d.OUT_SER = m.OUT_SER
         AND d.OUT_TYPE = m.OUT_TYPE
        JOIN {schema}.IAS_ITM_MST i
          ON i.I_CODE = d.I_CODE
        WHERE {" AND ".join(filters)}
        GROUP BY
            NVL(TO_CHAR(m.BRN_NO), '(بلا)'),
            NVL(TO_CHAR(i.G_CODE), '(بلا)')
        HAVING SUM(NVL(d.I_QTY, 0) * NVL(d.STK_COST, 0)) <> 0
        ORDER BY WASTE_VALUE DESC
        """,
        params,
    )

    branch_names = _branch_names()
    group_names = {
        str(g.get("code") or "").strip(): str(g.get("name") or "").strip()
        for g in fetch_sales_group_options()
        if str(g.get("code") or "").strip()
    }
    total_value = round(sum(float(r.get("WASTE_VALUE") or 0) for r in rows), 2)
    total_qty = round(sum(float(r.get("QTY_TOTAL") or 0) for r in rows), 2)
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        branch = str(row.get("BRANCH_CODE") or "").strip() or "(بلا)"
        group = str(row.get("GROUP_CODE") or "").strip() or "(بلا)"
        value = round(float(row.get("WASTE_VALUE") or 0), 2)
        qty = round(float(row.get("QTY_TOTAL") or 0), 2)
        share = (value / total_value * 100.0) if total_value else 0.0
        out_rows.append(
            {
                "branch_code": branch,
                "branch_name": branch_names.get(branch) or branch,
                "group_code": group,
                "group_name": group_names.get(group) or group,
                "doc_count": int(row.get("DOC_COUNT") or 0),
                "qty_total": qty,
                "qty_display": _fmt_inv_qty(qty),
                "waste_value": value,
                "waste_value_display": _fmt_inv_money(value),
                "share_pct": round(share, 1),
                "share_display": f"{share:.1f}%",
            }
        )

    by_group: dict[str, dict[str, Any]] = {}
    for row in out_rows:
        code = row["group_code"]
        grouped = by_group.setdefault(
            code,
            {
                "code": code,
                "name": row["group_name"],
                "waste_value": 0.0,
                "qty_total": 0.0,
                "doc_count": 0,
                "branches": set(),
            },
        )
        grouped["waste_value"] += row["waste_value"]
        grouped["qty_total"] += row["qty_total"]
        grouped["doc_count"] += row["doc_count"]
        grouped["branches"].add(row["branch_code"])

    top_group_raw = max(
        by_group.values(),
        key=lambda item: (item["waste_value"], item["qty_total"]),
        default=None,
    )
    if top_group_raw:
        top_value = round(float(top_group_raw["waste_value"]), 2)
        top_qty = round(float(top_group_raw["qty_total"]), 2)
        top_share = (top_value / total_value * 100.0) if total_value else 0.0
        top_group = {
            "code": top_group_raw["code"],
            "name": top_group_raw["name"],
            "waste_value": top_value,
            "waste_value_display": _fmt_inv_money(top_value),
            "qty_total": top_qty,
            "qty_display": _fmt_inv_qty(top_qty),
            "doc_count": top_group_raw["doc_count"],
            "branch_count": len(top_group_raw["branches"]),
            "share_pct": round(top_share, 1),
            "share_display": f"{top_share:.1f}%",
        }
    else:
        top_group = {
            "code": "",
            "name": "—",
            "waste_value": 0.0,
            "waste_value_display": "0.00",
            "qty_total": 0.0,
            "qty_display": "0",
            "doc_count": 0,
            "branch_count": 0,
            "share_pct": 0.0,
            "share_display": "0%",
        }

    result = {
        "rows": out_rows[:15],
        "count": len(out_rows),
        "doc_count": sum(r["doc_count"] for r in out_rows),
        "qty_total": total_qty,
        "qty_display": _fmt_inv_qty(total_qty),
        "waste_value": total_value,
        "waste_value_display": _fmt_inv_money(total_value),
        "period_label": f"{d_from.isoformat()} → {d_to.isoformat()}",
        "top_group": top_group,
    }
    _sales_cache_set(cache_key, result, date_from=d_from, date_to=d_to)
    return result


def _supplier_row(row: dict, *, source: str) -> dict:
    code = str(row.get("V_CODE") or "").strip()
    name = str(row.get("V_NAME") or row.get("V_A_NAME") or "").strip()
    last_dt = row.get("LAST_DT")
    last_date = ""
    if last_dt is not None:
        try:
            last_date = last_dt.strftime("%Y-%m-%d")
        except Exception:
            last_date = str(last_dt)[:10]
    return {
        "code": code,
        "name": name or code,
        "last_date": last_date,
        "bill_count": int(row.get("BILL_COUNT") or 0),
        "main": bool(int(row.get("MAIN_VNDR") or 0)),
        "source": source,
    }


def fetch_item_suppliers(item_code: str, *, limit: int = 40) -> list[dict]:
    """
    موردو الصنف الذين نُزّل منهم (فواتير شراء)، مع احتياطي من جدول ربط الموردين.
    SELECT فقط.
    """
    if not oracle_enabled():
        return []
    code = str(item_code or "").strip()
    if not code:
        return []
    lim = max(1, min(int(limit or 40), 100))
    schema = _schema()

    purchase_sql = f"""
        SELECT * FROM (
            SELECT
                TO_CHAR(m.V_CODE) AS V_CODE,
                NVL(
                    NULLIF(TRIM(m.V_NAME), ''),
                    vd.V_A_NAME
                ) AS V_NAME,
                MAX(m.BILL_DATE) AS LAST_DT,
                COUNT(*) AS BILL_COUNT,
                0 AS MAIN_VNDR
            FROM {schema}.IAS_PI_BILL_DTL d
            JOIN {schema}.IAS_PI_BILL_MST m
              ON m.BILL_NO = d.BILL_NO
             AND m.BILL_SER = d.BILL_SER
             AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
            LEFT JOIN {schema}.V_DETAILS vd
              ON TO_CHAR(vd.V_CODE) = TO_CHAR(m.V_CODE)
            WHERE d.I_CODE = :code
              AND m.V_CODE IS NOT NULL
            GROUP BY
                TO_CHAR(m.V_CODE),
                NVL(NULLIF(TRIM(m.V_NAME), ''), vd.V_A_NAME)
            ORDER BY MAX(m.BILL_DATE) DESC NULLS LAST
        )
        WHERE ROWNUM <= :lim
    """
    try:
        rows = _fetch_all(purchase_sql, {"code": code, "lim": lim})
    except OracleStockError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Item suppliers (purchase) failed: %s", exc)
        rows = []

    if rows:
        out = [_supplier_row(r, source="purchase") for r in rows]
        return [r for r in out if r["code"] or r["name"]]

    linked_sql = f"""
        SELECT * FROM (
            SELECT
                TO_CHAR(vi.V_CODE) AS V_CODE,
                vd.V_A_NAME AS V_NAME,
                NVL(vi.UP_DATE, vi.AD_DATE) AS LAST_DT,
                0 AS BILL_COUNT,
                NVL(vi.MAIN_VNDR, 0) AS MAIN_VNDR
            FROM {schema}.IAS_VNDR_ITM vi
            LEFT JOIN {schema}.V_DETAILS vd
              ON TO_CHAR(vd.V_CODE) = TO_CHAR(vi.V_CODE)
            WHERE vi.I_CODE = :code
              AND vi.V_CODE IS NOT NULL
            ORDER BY NVL(vi.MAIN_VNDR, 0) DESC,
                     NVL(vi.UP_DATE, vi.AD_DATE) DESC NULLS LAST
        )
        WHERE ROWNUM <= :lim
    """
    try:
        rows = _fetch_all(linked_sql, {"code": code, "lim": lim})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Item suppliers (linked) failed: %s", exc)
        rows = []

    if rows:
        out = [_supplier_row(r, source="linked") for r in rows]
        return [r for r in out if r["code"] or r["name"]]

    default_sql = f"""
        SELECT
            TO_CHAR(m.V_CODE) AS V_CODE,
            vd.V_A_NAME AS V_NAME,
            CAST(NULL AS DATE) AS LAST_DT,
            0 AS BILL_COUNT,
            1 AS MAIN_VNDR
        FROM {schema}.IAS_ITM_MST m
        LEFT JOIN {schema}.V_DETAILS vd
          ON TO_CHAR(vd.V_CODE) = TO_CHAR(m.V_CODE)
        WHERE m.I_CODE = :code
          AND m.V_CODE IS NOT NULL
    """
    try:
        rows = _fetch_all(default_sql, {"code": code})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Item suppliers (default) failed: %s", exc)
        return []
    return [_supplier_row(r, source="default") for r in rows if r.get("V_CODE")]


def fetch_last_purchase_by_warehouse(
    item_code: str,
    warehouses: list[str] | None = None,
) -> tuple[dict[str, dict], dict[str, float], str]:
    """
    آخر سعر شراء/توريد من أونكس (فواتير الشراء) لكل مخزن.

    المنطق كأونكس: يأخذ آخر فاتورة شراء بأي وحدة (الوحدة الأب/الأكبر
    مثل باكت)، ثم يُشتق سعر الوحدة الأساسية عبر I_PRICE / P_SIZE
    (باكت بعبوة 4 كيلو وسعر 20 → سعر الكيلو = 5).

    يعيد: (
      {w_code: {price, price_base, p_size, unit, date}},
      {unit: p_size},
      main_unit,
    )
    """
    empty: tuple[dict[str, dict], dict[str, float], str] = ({}, {}, "")
    if not oracle_enabled():
        return empty
    code = str(item_code or "").strip()
    if not code:
        return empty
    wh_list = [str(w).strip() for w in (warehouses or []) if str(w).strip()]
    schema = _schema()

    wh_filter = ""
    params: dict[str, Any] = {"code": code}
    if wh_list:
        placeholders = []
        for i, wh in enumerate(wh_list):
            key = f"w{i}"
            placeholders.append(f":{key}")
            params[key] = wh
        wh_filter = (
            f"AND TO_CHAR(NVL(d.W_CODE, m.W_CODE)) IN ({', '.join(placeholders)})"
        )

    sql = f"""
        SELECT
            TO_CHAR(w_code) AS W_CODE,
            ROUND(price, 4) AS PRICE,
            ROUND(price_base, 6) AS PRICE_BASE,
            ROUND(p_size, 4) AS P_SIZE,
            unt AS UNIT,
            dt AS BILL_DATE
        FROM (
            SELECT
                NVL(d.W_CODE, m.W_CODE) AS w_code,
                d.I_PRICE AS price,
                CASE
                  WHEN NVL(d.P_SIZE, 0) > 0 THEN d.I_PRICE / d.P_SIZE
                  ELSE d.I_PRICE
                END AS price_base,
                NVL(d.P_SIZE, 0) AS p_size,
                d.ITM_UNT AS unt,
                m.BILL_DATE AS dt,
                ROW_NUMBER() OVER (
                    PARTITION BY TO_CHAR(NVL(d.W_CODE, m.W_CODE))
                    ORDER BY m.BILL_DATE DESC NULLS LAST,
                             m.BILL_NO DESC NULLS LAST
                ) AS rn
            FROM {schema}.IAS_PI_BILL_DTL d
            JOIN {schema}.IAS_PI_BILL_MST m
              ON m.BILL_NO = d.BILL_NO
             AND m.BILL_SER = d.BILL_SER
             AND m.BILL_DOC_TYPE = d.BILL_DOC_TYPE
            WHERE TO_CHAR(d.I_CODE) = :code
              AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
              {wh_filter}
        )
        WHERE rn = 1
    """
    try:
        rows = _fetch_all(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Last purchase by warehouse failed: %s", exc)
        rows = []

    out: dict[str, dict] = {}
    for row in rows:
        wh = str(row.get("W_CODE") or "").strip()
        if not wh:
            continue
        price = row.get("PRICE")
        price_base = row.get("PRICE_BASE")
        p_size = row.get("P_SIZE")
        last_dt = row.get("BILL_DATE")
        last_date = ""
        if last_dt is not None:
            try:
                last_date = last_dt.strftime("%Y-%m-%d")
            except Exception:
                last_date = str(last_dt)[:10]
        out[wh] = {
            "price": "" if price in (None, "") else str(price).strip(),
            "price_base": (
                "" if price_base in (None, "") else str(price_base).strip()
            ),
            "p_size": "" if p_size in (None, "") else str(p_size).strip(),
            "unit": str(row.get("UNIT") or "").strip(),
            "date": last_date,
        }

    unit_packs: dict[str, float] = {}
    main_unit = ""
    try:
        unit_rows = _fetch_all(
            f"""
            SELECT ITM_UNT, P_SIZE, MAIN_UNIT, STOCK_UNIT, SALE_UNIT
            FROM {schema}.IAS_ITM_DTL
            WHERE TO_CHAR(I_CODE) = :code
              AND NVL(P_SIZE, 0) > 0
            """,
            {"code": code},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Item unit packs failed: %s", exc)
        unit_rows = []

    for row in unit_rows:
        unit = str(row.get("ITM_UNT") or "").strip()
        try:
            psz = float(row.get("P_SIZE") or 0)
        except (TypeError, ValueError):
            psz = 0.0
        if not unit or psz <= 0:
            continue
        unit_packs[unit] = psz
        if not main_unit and int(row.get("MAIN_UNIT") or 0) == 1:
            main_unit = unit
        if not main_unit and int(row.get("STOCK_UNIT") or 0) == 1:
            main_unit = unit

    if not main_unit:
        for row in unit_rows:
            if int(row.get("SALE_UNIT") or 0) == 1:
                main_unit = str(row.get("ITM_UNT") or "").strip()
                break
    if not main_unit and unit_packs:
        # أصغر عبوة غالباً الوحدة الأساسية
        main_unit = min(unit_packs.items(), key=lambda kv: kv[1])[0]

    return out, unit_packs, main_unit


def fetch_item_stock_by_warehouses(
    item_code: str,
    warehouses: list[str] | None = None,
) -> dict[str, dict]:
    """
    رصيد الصنف من IAS_ITM_WCODE لكل مخزن: وحدة + كمية + متوسط تكلفة.
    يعيد: {w_code: {unit, quantity, avg_cost}}
    """
    if not oracle_enabled():
        return {}
    code = str(item_code or "").strip()
    if not code:
        return {}
    wh_list = [str(w).strip() for w in (warehouses or []) if str(w).strip()]
    schema = _schema()

    wh_filter = ""
    params: dict[str, Any] = {"code": code}
    if wh_list:
        placeholders = []
        for i, wh in enumerate(wh_list):
            key = f"w{i}"
            placeholders.append(f":{key}")
            params[key] = wh
        wh_filter = f"AND TO_CHAR(w.W_CODE) IN ({', '.join(placeholders)})"

    sql = f"""
        SELECT
            TO_CHAR(w.W_CODE) AS W_CODE,
            w.ITM_UNT AS UNIT,
            ROUND(NVL(w.AVL_QTY, 0), 4) AS QTY,
            ROUND(NVL(w.I_CWTAVG, w.PRIMARY_COST), 6) AS COST
        FROM {schema}.IAS_ITM_WCODE w
        WHERE TO_CHAR(w.I_CODE) = :code
          {wh_filter}
    """
    try:
        rows = _fetch_all(sql, params)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Item stock by warehouse failed: %s", exc)
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        wh = str(row.get("W_CODE") or "").strip()
        if not wh:
            continue
        qty = row.get("QTY")
        cost = row.get("COST")
        out[wh] = {
            "unit": str(row.get("UNIT") or "").strip(),
            "quantity": "" if qty in (None, "") else str(qty).strip(),
            "avg_cost": "" if cost in (None, "") else str(cost).strip(),
        }
    return out


# نافذة احتياطية لمبيعات نقاط البيع غير المرحلة (أيام)
_PENDING_SALES_LOOKBACK_DAYS = 400


def fetch_pending_sales_qty_map(
    item_codes: list[str],
    *,
    warehouse_codes: list[str] | None = None,
    lookback_days: int | None = None,
) -> dict[str, dict[str, float]]:
    """
    صافي كميات مبيعات نقاط البيع غير المرحلة لكل صنف×مخزن.

    يعتمد تقرير أونكس «مبيعات الأصناف» غير المرحّل من POS فقط
    (BILL_POST للآجل لا يُخصم هنا لأنه يضخّم الرصيد خطأ).

    الناتج: {item_code: {warehouse_code: pending_qty}} بوحدة المخزون (P_QTY).
    """
    codes = [str(c).strip() for c in item_codes if str(c).strip()]
    if not codes or not oracle_enabled():
        return {}

    days = int(lookback_days or _PENDING_SALES_LOOKBACK_DAYS)
    if days < 1:
        days = _PENDING_SALES_LOOKBACK_DAYS

    wh_list = [str(w).strip() for w in (warehouse_codes or []) if str(w).strip()]
    pos = _pos_owner()
    params: dict[str, Any] = {"lookback_days": days}
    code_keys = []
    for i, code in enumerate(codes):
        key = f"c{i}"
        code_keys.append(f":{key}")
        params[key] = code
    code_in = ", ".join(code_keys)

    wh_filter = ""
    if wh_list:
        wh_keys = []
        for i, wh in enumerate(wh_list):
            key = f"w{i}"
            wh_keys.append(f":{key}")
            params[key] = wh
        wh_filter = f"AND TO_CHAR(NVL(d.W_CODE, m.W_CODE)) IN ({', '.join(wh_keys)})"

    qty_expr = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
    pending: dict[str, dict[str, float]] = {code: {} for code in codes}

    def _add(item: str, wh: str, qty: float) -> None:
        item = str(item or "").strip()
        wh = str(wh or "").strip()
        if not item or not wh or abs(qty) < 1e-9:
            return
        bucket = pending.setdefault(item, {})
        bucket[wh] = round(float(bucket.get(wh) or 0) + float(qty), 4)

    # نقاط البيع غير المرحلة
    try:
        for row in _fetch_all(
            f"""
            SELECT TO_CHAR(d.I_CODE) AS ITEM_CODE,
                   TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS WAREHOUSE_CODE,
                   ROUND(SUM({qty_expr}), 4) AS QTY
            FROM {pos}.IAS_POS_BILL_DTL d
            JOIN {pos}.IAS_POS_BILL_MST m
              ON m.BILL_NO = d.BILL_NO
             AND m.BRN_NO = d.BRN_NO
             AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
            WHERE TO_CHAR(d.I_CODE) IN ({code_in})
              AND NVL(m.POSTED, 0) = 0
              AND NVL(m.HUNG, 0) = 0
              AND m.BILL_DATE >= TRUNC(SYSDATE) - :lookback_days
              AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
              {wh_filter}
            GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
            """,
            params,
        ):
            _add(row.get("ITEM_CODE"), row.get("WAREHOUSE_CODE"), float(row.get("QTY") or 0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unposted POS sales qty failed: %s", exc)

    # مرتجعات نقاط البيع غير المرحلة
    try:
        for row in _fetch_all(
            f"""
            SELECT TO_CHAR(d.I_CODE) AS ITEM_CODE,
                   TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS WAREHOUSE_CODE,
                   ROUND(SUM({qty_expr}), 4) AS QTY
            FROM {pos}.IAS_POS_RT_BILL_DTL d
            JOIN {pos}.IAS_POS_RT_BILL_MST m
              ON m.RT_BILL_NO = d.RT_BILL_NO
             AND m.BRN_NO = d.BRN_NO
            WHERE TO_CHAR(d.I_CODE) IN ({code_in})
              AND NVL(m.POSTED, 0) = 0
              AND NVL(m.HUNG, 0) = 0
              AND m.RT_BILL_DATE >= TRUNC(SYSDATE) - :lookback_days
              AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
              {wh_filter}
            GROUP BY TO_CHAR(d.I_CODE), TO_CHAR(NVL(d.W_CODE, m.W_CODE))
            """,
            dict(params),
        ):
            _add(
                row.get("ITEM_CODE"),
                row.get("WAREHOUSE_CODE"),
                -float(row.get("QTY") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unposted POS return qty failed: %s", exc)

    # لا نسمح بصافي سالب (لا يزيد الرصيد عن الحالي بسبب مرتجعات)
    for item, by_wh in pending.items():
        for wh, qty in list(by_wh.items()):
            by_wh[wh] = round(max(0.0, float(qty or 0)), 4)
            if by_wh[wh] <= 0:
                by_wh.pop(wh, None)
    return pending


def expected_stock_qty(avl_qty: float, pending_qty: float) -> float:
    """الرصيد الحقيقي المتوقع: لا يزيد عن الحالي ولا ينزل عن صفر."""
    avl = max(0.0, float(avl_qty or 0))
    pending = max(0.0, float(pending_qty or 0))
    return round(max(0.0, avl - pending), 4)


# أنواع مستندات IAS_BILL التي يطابق مجموعها تقرير «مبيعات الأصناف» في أونكس
_ONIX_ITEM_SALES_DOC_TYPES = (1, 5)


def fetch_posted_item_sales_by_warehouses(
    item_code: str,
    warehouses: list[str],
    date_from,
    date_to,
    *,
    warehouse_names: dict[str, str] | None = None,
    system: str = "bill",
) -> dict[str, Any]:
    """
    مبيعات صنف واحد عبر مخازن محددة، صافي بعد المرتجع.

    system:
      - bill: فواتير IAS_BILL (أنواع 1 و5) — مطابقة تقرير أونكس «مبيعات الأصناف»
      - pos: نقاط البيع غير المعلّقة
    """
    code = str(item_code or "").strip()
    wh_list = [str(w).strip() for w in (warehouses or []) if str(w).strip()]
    sys = str(system or "bill").strip().lower()
    if sys not in ("pos", "bill"):
        sys = "bill"
    empty = {
        "item_code": code,
        "item_name": "",
        "system": sys,
        "rows": [],
        "totals": {
            "qty": 0.0,
            "qty_display": "0",
            "avg_cost": None,
            "avg_cost_display": "—",
            "vat_total": 0.0,
            "vat_total_display": "0.00",
            "sales_total": 0.0,
            "sales_total_display": "0.00",
            "invoice_count": 0,
            "invoice_count_display": "0",
            "return_qty": 0.0,
            "return_qty_display": "0",
            "return_total": 0.0,
            "return_total_display": "0.00",
        },
    }
    if not code or not wh_list or not oracle_enabled():
        return empty

    def _norm_wh(raw: object) -> str:
        s = str(raw or "").strip()
        if s.endswith(".0") and s[:-2].replace("-", "", 1).isdigit():
            s = s[:-2]
        if s.isdigit():
            try:
                return str(int(s))
            except ValueError:
                return s
        return s

    schema = _schema()
    params: dict[str, Any] = {
        **_date_params(date_from, date_to),
        "code": code,
    }
    wh_norm_list = [_norm_wh(w) for w in wh_list]
    wh_keys = []
    for i, wh in enumerate(wh_norm_list):
        key = f"w{i}"
        wh_keys.append(f":{key}")
        params[key] = wh
    wh_in = ", ".join(wh_keys)
    # كمية بوحدة المخزون (مثل تقرير أونكس) — ليست I_QTY وحدة البيع
    qty_expr = (
        "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(NULLIF(d.P_SIZE, 0), 1))"
    )

    sales_by_wh: dict[str, dict[str, float]] = {}
    returns_by_wh: dict[str, dict[str, float]] = {}

    if sys == "pos":
        pos = _pos_owner()
        hung_m = _hung_ok("m")
        wh_expr = "TRIM(TO_CHAR(NVL(d.W_CODE, m.W_CODE)))"
        wh_match_sql = (
            f"NVL(NULLIF(LTRIM(REGEXP_REPLACE({wh_expr}, '\\.0+$', ''), '0'), ''), '0')"
        )
        try:
            for row in _fetch_all(
                f"""
                SELECT {wh_match_sql} AS WAREHOUSE_CODE,
                       COUNT(
                         DISTINCT TO_CHAR(m.BILL_NO) || ':' || TO_CHAR(m.BRN_NO)
                         || ':' || TO_CHAR(NVL(m.BILL_SRL, 0))
                       ) AS INVOICE_COUNT,
                       ROUND(SUM({qty_expr}), 2) AS QTY_TOTAL,
                       ROUND(
                         SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)),
                         2
                       ) AS NET_TOTAL,
                       ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
                FROM {pos}.IAS_POS_BILL_MST m
                JOIN {pos}.IAS_POS_BILL_DTL d
                  ON d.BILL_NO = m.BILL_NO
                 AND d.BRN_NO = m.BRN_NO
                 AND NVL(d.BILL_SRL, 0) = NVL(m.BILL_SRL, 0)
                WHERE TO_CHAR(d.I_CODE) = :code
                  AND m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                  AND {hung_m}
                  AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
                  AND {wh_match_sql} IN ({wh_in})
                GROUP BY {wh_match_sql}
                """,
                params,
            ):
                wh = _norm_wh(row.get("WAREHOUSE_CODE"))
                if not wh:
                    continue
                sales_by_wh[wh] = {
                    "invoice_count": float(row.get("INVOICE_COUNT") or 0),
                    "qty": float(row.get("QTY_TOTAL") or 0),
                    "net": float(row.get("NET_TOTAL") or 0),
                    "vat": float(row.get("VAT_TOTAL") or 0),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Item POS sales by warehouse failed: %s", exc)
            raise OracleStockError("تعذر جلب مبيعات الصنف حسب المخزن.") from exc

        try:
            for row in _fetch_all(
                f"""
                SELECT {wh_match_sql} AS WAREHOUSE_CODE,
                       ROUND(SUM({qty_expr}), 2) AS RET_QTY,
                       ROUND(
                         SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)),
                         2
                       ) AS RET_NET,
                       ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {pos}.IAS_POS_RT_BILL_MST m
                JOIN {pos}.IAS_POS_RT_BILL_DTL d
                  ON d.RT_BILL_NO = m.RT_BILL_NO
                 AND d.BRN_NO = m.BRN_NO
                WHERE TO_CHAR(d.I_CODE) = :code
                  AND m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                  AND NVL(m.HUNG, 0) = 0
                  AND NVL(d.W_CODE, m.W_CODE) IS NOT NULL
                  AND {wh_match_sql} IN ({wh_in})
                GROUP BY {wh_match_sql}
                """,
                dict(params),
            ):
                wh = _norm_wh(row.get("WAREHOUSE_CODE"))
                if not wh:
                    continue
                returns_by_wh[wh] = {
                    "qty": float(row.get("RET_QTY") or 0),
                    "net": float(row.get("RET_NET") or 0),
                    "vat": float(row.get("RET_VAT") or 0),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Item POS returns by warehouse failed: %s", exc)
    else:
        # فواتير أونكس (IAS_BILL) — أنواع 1 و5 كما في تقرير مبيعات الأصناف
        bill_wh_expr = "TRIM(TO_CHAR(NVL(d.W_CODE, b.W_CODE)))"
        bill_wh_match = (
            "NVL(NULLIF(LTRIM(REGEXP_REPLACE("
            f"{bill_wh_expr}, '\\.0+$', ''), '0'), ''), '0')"
        )
        bill_conf = {"doc_types": _ONIX_ITEM_SALES_DOC_TYPES}
        doc_filter = _doc_type_filter(bill_conf, "b", "BILL_DOC_TYPE", params)
        try:
            for row in _fetch_all(
                f"""
                SELECT {bill_wh_match} AS WAREHOUSE_CODE,
                       COUNT(DISTINCT b.BILL_SER) AS INVOICE_COUNT,
                       ROUND(SUM({qty_expr}), 2) AS QTY_TOTAL,
                       ROUND(
                         SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)),
                         2
                       ) AS NET_TOTAL,
                       ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
                FROM {schema}.IAS_BILL_DTL d
                JOIN {schema}.IAS_BILL_MST b
                  ON b.BILL_NO = d.BILL_NO
                 AND b.BILL_SER = d.BILL_SER
                 AND b.BILL_DOC_TYPE = d.BILL_DOC_TYPE
                WHERE TO_CHAR(d.I_CODE) = :code
                  AND b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
                  AND {_bill_mst_ok("b")}
                  {doc_filter}
                  AND NVL(d.W_CODE, b.W_CODE) IS NOT NULL
                  AND {bill_wh_match} IN ({wh_in})
                GROUP BY {bill_wh_match}
                """,
                params,
            ):
                wh = _norm_wh(row.get("WAREHOUSE_CODE"))
                if not wh:
                    continue
                sales_by_wh[wh] = {
                    "invoice_count": float(row.get("INVOICE_COUNT") or 0),
                    "qty": float(row.get("QTY_TOTAL") or 0),
                    "net": float(row.get("NET_TOTAL") or 0),
                    "vat": float(row.get("VAT_TOTAL") or 0),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Item bill sales by warehouse failed: %s", exc)
            raise OracleStockError("تعذر جلب مبيعات الصنف حسب المخزن.") from exc

        try:
            ret_params = dict(params)
            ret_wh_expr = "TRIM(TO_CHAR(NVL(d.W_CODE, r.W_CODE)))"
            ret_wh_match = (
                "NVL(NULLIF(LTRIM(REGEXP_REPLACE("
                f"{ret_wh_expr}, '\\.0+$', ''), '0'), ''), '0')"
            )
            ret_doc = _doc_type_filter(
                bill_conf, "r", "RT_BILL_DOC_TYPE", ret_params
            )
            for row in _fetch_all(
                f"""
                SELECT {ret_wh_match} AS WAREHOUSE_CODE,
                       ROUND(SUM({qty_expr}), 2) AS RET_QTY,
                       ROUND(
                         SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)),
                         2
                       ) AS RET_NET,
                       ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {schema}.IAS_RT_BILL_DTL d
                JOIN {schema}.IAS_RT_BILL_MST r
                  ON r.RT_BILL_SER = d.RT_BILL_SER
                 AND r.BRN_NO = d.BRN_NO
                WHERE TO_CHAR(d.I_CODE) = :code
                  AND r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                  AND {_rt_bill_mst_ok("r")}
                  {ret_doc}
                  AND NVL(d.W_CODE, r.W_CODE) IS NOT NULL
                  AND {ret_wh_match} IN ({wh_in})
                GROUP BY {ret_wh_match}
                """,
                ret_params,
            ):
                wh = _norm_wh(row.get("WAREHOUSE_CODE"))
                if not wh:
                    continue
                returns_by_wh[wh] = {
                    "qty": float(row.get("RET_QTY") or 0),
                    "net": float(row.get("RET_NET") or 0),
                    "vat": float(row.get("RET_VAT") or 0),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Item bill returns by warehouse failed: %s", exc)

    item_name = ""
    try:
        name_rows = _fetch_all(
            f"""
            SELECT NVL(I_NAME, TO_CHAR(I_CODE)) AS ITEM_NAME
            FROM {schema}.IAS_ITM_MST
            WHERE TO_CHAR(I_CODE) = :code
            """,
            {"code": code},
        )
        if name_rows:
            item_name = str(name_rows[0].get("ITEM_NAME") or "").strip()
    except Exception:  # noqa: BLE001
        item_name = ""

    names = {
        _norm_wh(k): str(v).strip() for k, v in (warehouse_names or {}).items()
    }
    for k, v in (warehouse_names or {}).items():
        names.setdefault(str(k).strip(), str(v).strip())

    stock_raw = fetch_item_stock_by_warehouses(code, wh_list)
    stock_by_wh: dict[str, dict] = {}
    for k, v in stock_raw.items():
        stock_by_wh[_norm_wh(k)] = v
        stock_by_wh.setdefault(str(k).strip(), v)

    rows_out: list[dict] = []
    tot_qty = tot_net = tot_vat = tot_ret_qty = tot_ret = 0.0
    tot_inv = 0
    cost_weight = 0.0
    cost_sum = 0.0
    for wh_raw, wh in zip(wh_list, wh_norm_list):
        sale = sales_by_wh.get(wh) or {}
        ret = returns_by_wh.get(wh) or {}
        qty = round(float(sale.get("qty") or 0) - float(ret.get("qty") or 0), 2)
        net = round(float(sale.get("net") or 0) - float(ret.get("net") or 0), 2)
        vat = round(float(sale.get("vat") or 0) - float(ret.get("vat") or 0), 2)
        sales_total = round(net + vat, 2)
        inv_count = int(sale.get("invoice_count") or 0)
        ret_qty = round(float(ret.get("qty") or 0), 2)
        ret_total = round(float(ret.get("net") or 0) + float(ret.get("vat") or 0), 2)
        st = stock_by_wh.get(wh) or stock_by_wh.get(str(wh_raw).strip()) or {}
        avg_cost = None
        raw_cost = str(st.get("avg_cost") or "").strip().replace(",", "")
        if raw_cost:
            try:
                avg_cost = round(float(raw_cost), 4)
            except (TypeError, ValueError):
                avg_cost = None
        label = names.get(wh) or names.get(str(wh_raw).strip()) or f"مخزن {wh_raw}"
        for suffix in (
            f" - {wh_raw}",
            f"-{wh_raw}",
            f"({wh_raw})",
            f" - {wh}",
            f"-{wh}",
        ):
            if label.endswith(suffix):
                label = label[: -len(suffix)].strip()
                break
        rows_out.append(
            {
                "warehouse_code": wh_raw,
                "warehouse_name": label or f"مخزن {wh_raw}",
                "qty": qty,
                "qty_display": _fmt_inv_qty(qty),
                "avg_cost": avg_cost,
                "avg_cost_display": (
                    _fmt_inv_money(avg_cost) if avg_cost is not None else "—"
                ),
                "vat_total": vat,
                "vat_total_display": _fmt_inv_money(vat),
                "sales_total": sales_total,
                "sales_total_display": _fmt_inv_money(sales_total),
                "invoice_count": inv_count,
                "invoice_count_display": f"{inv_count:,}",
                "return_qty": ret_qty,
                "return_qty_display": _fmt_inv_qty(ret_qty),
                "return_total": ret_total,
                "return_total_display": _fmt_inv_money(ret_total),
                "has_sales": abs(float(sale.get("qty") or 0)) > 1e-9
                or abs(float(ret.get("qty") or 0)) > 1e-9
                or inv_count > 0,
            }
        )
        tot_qty += qty
        tot_net += net
        tot_vat += vat
        tot_inv += inv_count
        tot_ret_qty += ret_qty
        tot_ret += ret_total
        if avg_cost is not None:
            w = abs(float(qty)) if abs(float(qty)) > 1e-9 else 1.0
            cost_weight += w
            cost_sum += avg_cost * w

    tot_sales = round(tot_net + tot_vat, 2)
    tot_avg = round(cost_sum / cost_weight, 4) if cost_weight > 1e-9 else None
    return {
        "item_code": code,
        "item_name": item_name or code,
        "system": sys,
        "rows": rows_out,
        "totals": {
            "qty": round(tot_qty, 2),
            "qty_display": _fmt_inv_qty(tot_qty),
            "avg_cost": tot_avg,
            "avg_cost_display": (
                _fmt_inv_money(tot_avg) if tot_avg is not None else "—"
            ),
            "vat_total": round(tot_vat, 2),
            "vat_total_display": _fmt_inv_money(tot_vat),
            "sales_total": tot_sales,
            "sales_total_display": _fmt_inv_money(tot_sales),
            "invoice_count": tot_inv,
            "invoice_count_display": f"{tot_inv:,}",
            "return_qty": round(tot_ret_qty, 2),
            "return_qty_display": _fmt_inv_qty(tot_ret_qty),
            "return_total": round(tot_ret, 2),
            "return_total_display": _fmt_inv_money(tot_ret),
        },
    }


def fetch_item_compare_from_oracle(
    item_code: str,
    warehouses: list[str] | None = None,
) -> dict[str, Any]:
    """
    مقارنة صنف عبر المخازن بالكامل من أوراكل (بدون REST):
    - الوحدة الرئيسية من IAS_ITM_DTL
    - الكمية/التكلفة من IAS_ITM_WCODE
    - سعر البيع من IAS_ITEM_PRICE (LEV_NO=1)
    - آخر توريد من IAS_PI_BILL_* محوّل للوحدة الرئيسية
    """
    empty = {
        "main_unit": "",
        "unit_packs": {},
        "rows": {},
    }
    if not oracle_enabled():
        return empty
    code = str(item_code or "").strip()
    if not code:
        return empty
    wh_list = [str(w).strip() for w in (warehouses or []) if str(w).strip()]
    if not wh_list:
        return empty

    buys, unit_packs, main_unit = fetch_last_purchase_by_warehouse(code, wh_list)
    stock = fetch_item_stock_by_warehouses(code, wh_list)
    pending_map = fetch_pending_sales_qty_map([code], warehouse_codes=wh_list)
    pending_by_wh = pending_map.get(code) or {}

    schema = _schema()
    params: dict[str, Any] = {"code": code, "lev": 1}
    placeholders = []
    for i, wh in enumerate(wh_list):
        key = f"w{i}"
        placeholders.append(f":{key}")
        params[key] = wh
    wh_in = ", ".join(placeholders)

    prices: dict[str, dict[str, str]] = {}
    try:
        price_rows = _fetch_all(
            f"""
            SELECT
                TO_CHAR(p.W_CODE) AS W_CODE,
                p.ITM_UNT AS UNIT,
                ROUND(p.I_PRICE, 4) AS PRICE
            FROM {schema}.IAS_ITEM_PRICE p
            WHERE TO_CHAR(p.I_CODE) = :code
              AND NVL(p.LEV_NO, 1) = :lev
              AND TO_CHAR(p.W_CODE) IN ({wh_in})
              AND NVL(p.I_PRICE, 0) <> 0
            """,
            params,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Item compare prices failed: %s", exc)
        price_rows = []

    for row in price_rows:
        wh = str(row.get("W_CODE") or "").strip()
        unit = str(row.get("UNIT") or "").strip()
        if not wh or not unit:
            continue
        price = row.get("PRICE")
        prices.setdefault(wh, {})[unit] = (
            "" if price in (None, "") else str(price).strip()
        )

    def _unit_key(unit: str) -> str:
        text = str(unit or "").strip()
        for ch in ("\u200e", "\u200f", "\u0640", " ", "\u00a0"):
            text = text.replace(ch, "")
        return text.casefold()

    def _pack_for(unit: str) -> float | None:
        if not unit:
            return None
        if unit in unit_packs:
            return unit_packs[unit]
        key = _unit_key(unit)
        for name, psz in unit_packs.items():
            if _unit_key(name) == key:
                return psz
        return None

    def _price_for(wh: str, unit: str) -> str:
        by_unit = prices.get(wh) or {}
        if unit in by_unit:
            return by_unit[unit]
        key = _unit_key(unit)
        for name, val in by_unit.items():
            if _unit_key(name) == key:
                return val
        return ""

    def _last_buy_for(wh: str, unit: str) -> tuple[str, str]:
        buy = buys.get(wh) or {}
        if not buy:
            return "", ""
        date = str(buy.get("date") or "")
        buy_unit = str(buy.get("unit") or "").strip()
        if unit and buy_unit and _unit_key(unit) == _unit_key(buy_unit):
            raw = buy.get("price")
            return ("" if raw in (None, "") else str(raw).strip()), date
        try:
            base = float(str(buy.get("price_base") or "").replace(",", ""))
        except (TypeError, ValueError):
            base = None
            try:
                raw = float(str(buy.get("price") or "").replace(",", ""))
                psz = float(str(buy.get("p_size") or "0").replace(",", "") or 0)
                if psz > 0:
                    base = raw / psz
                else:
                    base = raw
            except (TypeError, ValueError):
                base = None
        if base is None:
            return "", date
        disp = _pack_for(unit)
        if disp and disp > 0:
            return str(round(base * disp, 4)), date
        return str(round(base, 4)), date

    compare_unit = str(main_unit or "").strip()
    if not compare_unit:
        for st in stock.values():
            u = str((st or {}).get("unit") or "").strip()
            if u:
                compare_unit = u
                break

    rows_out: dict[str, dict] = {}
    for wh in wh_list:
        st = stock.get(wh) or {}
        unit = compare_unit or str(st.get("unit") or "").strip()
        # إن اختلفت وحدة الرصيد عن وحدة المقارنة لا نخلط الكمية
        qty = ""
        cost = ""
        st_unit = str(st.get("unit") or "").strip()
        if st and (
            not unit
            or not st_unit
            or _unit_key(st_unit) == _unit_key(unit)
        ):
            qty = str(st.get("quantity") or "").strip()
            cost = str(st.get("avg_cost") or "").strip()
        price = _price_for(wh, unit) if unit else ""
        last_buy, last_date = _last_buy_for(wh, unit) if unit else ("", "")
        try:
            avl_num = float(str(qty).replace(",", "")) if qty not in ("", None) else None
        except (TypeError, ValueError):
            avl_num = None
        pending_num = float(pending_by_wh.get(wh) or 0)
        if avl_num is None and pending_num:
            avl_num = 0.0
            qty = "0"
        expected_num = (
            expected_stock_qty(avl_num, pending_num) if avl_num is not None else None
        )
        rows_out[wh] = {
            "unit": unit,
            "price": price,
            "quantity": qty,
            "pending_qty": pending_num,
            "expected_qty": "" if expected_num is None else str(expected_num),
            "avg_cost": cost,
            "last_buy": last_buy,
            "last_buy_date": last_date,
        }

    return {
        "main_unit": compare_unit,
        "unit_packs": unit_packs,
        "rows": rows_out,
    }


def _branch_names() -> dict[str, str]:
    """أسماء الفروع من S_BRN مفهرسة برقم الفرع."""
    hit, cached = _django_lookup_get("branch_names")
    if hit:
        return cached
    try:
        rows = _fetch_all(
            f"SELECT BRN_NO, BRN_LNAME, BRN_FNAME FROM {_schema()}.S_BRN"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Branch names unavailable: %s", exc)
        return _django_lookup_set("branch_names", {})
    names: dict[str, str] = {}
    for row in rows:
        code = str(row.get("BRN_NO") or "").strip()
        if not code:
            continue
        label = str(row.get("BRN_LNAME") or row.get("BRN_FNAME") or "").strip()
        label = label.lstrip("-").strip() or code
        names[code] = label
    return _django_lookup_set("branch_names", names)


# نقاط البيع من جدول POS الحقيقي؛ الآجل من فواتير المبيعات العامة.
# أونكس = أنواع مستند تقرير «مبيعات الأصناف» في أونكس (1 و5).
SALES_SYSTEMS: dict[str, dict] = {
    "pos": {
        "label": "نقاط البيع",
        "source": "pos",  # YSPOS1.IAS_POS_BILL_MST
    },
    "wholesale": {
        "label": "الآجل",
        "source": "bill",  # IAS_BILL_MST
        "doc_types": (4, 8),
        "require_cash": False,
    },
    "onix": {
        "label": "أونكس",
        "source": "bill",
        "doc_types": _ONIX_ITEM_SALES_DOC_TYPES,
        "require_cash": False,
    },
}


def _system_conf(system: str) -> dict:
    conf = SALES_SYSTEMS.get(str(system or "").strip().lower())
    if not conf:
        raise OracleStockError("نظام مبيعات غير معروف.")
    return conf


def _pos_owner() -> str:
    """مالك جدول فواتير نقاط البيع (عادة YSPOS1 عبر مرادف IAS20261)."""
    hit, cached = _django_lookup_get("pos_owner")
    if hit:
        return cached
    owner = "YSPOS1"
    try:
        rows = _fetch_all(
            """
            SELECT TABLE_OWNER
            FROM ALL_SYNONYMS
            WHERE OWNER = :owner AND SYNONYM_NAME = 'IAS_POS_BILL_MST'
            """,
            {"owner": _schema()},
        )
        if rows and rows[0].get("TABLE_OWNER"):
            owner = str(rows[0]["TABLE_OWNER"]).upper()
    except Exception as exc:  # noqa: BLE001
        logger.warning("POS owner lookup failed: %s", exc)
    return _django_lookup_set("pos_owner", owner)


def _doc_type_filter(conf: dict, alias: str, column: str, params: dict) -> str:
    """شرط أنواع المستندات مع ربط آمن للقيم."""
    names = []
    for index, doc_type in enumerate(conf.get("doc_types") or ()):
        key = f"dt_{index}"
        params[key] = doc_type
        names.append(f":{key}")
    if not names:
        return ""
    return f"AND {alias}.{column} IN ({', '.join(names)})"


def _assemble_branch_rows(sales_rows, returns_by_brn) -> list[dict]:
    names = _branch_names()
    out: list[dict] = []
    for row in sales_rows:
        code = str(row.get("BRANCH_CODE") or "").strip()
        ret_count, ret_net, ret_vat = returns_by_brn.get(code, (0, 0.0, 0.0))
        sales_count = int(row.get("INVOICE_COUNT") or 0)
        gross_net = float(row.get("NET_TOTAL") or 0)
        gross_vat = float(row.get("VAT_TOTAL") or 0)
        gross_total = float(row.get("GROSS_TOTAL") or (gross_net + gross_vat))
        net = gross_net - ret_net
        vat = gross_vat - ret_vat
        sales_total = round(net + vat, 2)
        avg_basket = round(sales_total / sales_count, 2) if sales_count else 0.0
        out.append(
            {
                "branch_code": code,
                "branch_name": names.get(code) or code,
                "invoice_count": sales_count,
                "return_count": ret_count,
                "return_total": round(ret_net + ret_vat, 2),
                "net_invoice_count": sales_count - ret_count,
                "gross_total": round(gross_total, 2),
                "net_total": round(net, 2),
                "vat_total": round(vat, 2),
                "sales_total": sales_total,
                "avg_basket": avg_basket,
            }
        )
    out.sort(key=lambda r: (-r["sales_total"], r["branch_code"]))
    return out


def _fetch_pos_branch_totals(date_from, date_to) -> list[dict]:
    """إجماليات نقاط البيع من IAS_POS_BILL_MST / IAS_POS_RT_BILL_MST.

    صافي بعد المرتجع — مرجع الفترة لنقاط البيع (يطابقه جدول المجموعات بعد المطابقة).
    """
    pos = _pos_owner()
    params = _date_params(date_from, date_to)
    hung = _hung_ok("p")
    amt_ok = _bill_amt_ok("p", "BILL_AMT")
    sales_rows = _fetch_all(
        f"""
        SELECT
            TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
            COUNT(DISTINCT p.BILL_NO) AS INVOICE_COUNT,
            ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL,
            ROUND(SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)), 2) AS GROSS_TOTAL
        FROM {pos}.IAS_POS_BILL_MST p
        WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
          AND {hung}
          AND {amt_ok}
        GROUP BY TO_CHAR(p.BRN_NO)
        """,
        params,
    )
    returns_by_brn: dict[str, tuple[int, float, float]] = {}
    if _skip_mst_returns(date_from, date_to):
        return _assemble_branch_rows(sales_rows, returns_by_brn)
    try:
        ret_hung = _hung_ok("r")
        ret_amt = _bill_amt_ok("r", "RT_BILL_AMT")
        for row in _fetch_all(
            f"""
            SELECT
                TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                COUNT(DISTINCT r.RT_BILL_NO) AS RET_COUNT,
                ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {pos}.IAS_POS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND {ret_hung}
              AND {ret_amt}
            GROUP BY TO_CHAR(r.BRN_NO)
            """,
            params,
        ):
            code = str(row.get("BRANCH_CODE") or "").strip()
            returns_by_brn[code] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("POS returns totals failed: %s", exc)
        raise
    return _assemble_branch_rows(sales_rows, returns_by_brn)


def _fetch_bill_branch_totals(date_from, date_to, conf: dict) -> list[dict]:
    """إجماليات من IAS_BILL_MST (الآجل وغيرها)."""
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    sales_rows = _fetch_all(
        f"""
        SELECT
            TO_CHAR(b.BRN_NO) AS BRANCH_CODE,
            COUNT(DISTINCT b.BILL_SER) AS INVOICE_COUNT,
            ROUND(SUM(NVL(b.BILL_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(b.VAT_AMT, 0)), 2) AS VAT_TOTAL,
            ROUND(SUM(NVL(b.BILL_AMT, 0) + NVL(b.VAT_AMT, 0)), 2) AS GROSS_TOTAL
        FROM {schema}.IAS_BILL_MST b
        WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          {doc_filter}
          {cash_filter}
          AND {_bill_mst_ok("b")}
        GROUP BY TO_CHAR(b.BRN_NO)
        """,
        params,
    )
    returns_by_brn: dict[str, tuple[int, float, float]] = {}
    if _skip_mst_returns(date_from, date_to):
        return _assemble_branch_rows(sales_rows, returns_by_brn)
    try:
        ret_params: dict = _date_params(date_from, date_to)
        ret_doc_filter = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash_filter = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        for row in _fetch_all(
            f"""
            SELECT
                TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                COUNT(DISTINCT r.RT_BILL_SER) AS RET_COUNT,
                ROUND(SUM(NVL(r.BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {schema}.IAS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              {ret_doc_filter}
              {ret_cash_filter}
              AND {_rt_bill_mst_ok("r")}
            GROUP BY TO_CHAR(r.BRN_NO)
            """,
            ret_params,
        ):
            code = str(row.get("BRANCH_CODE") or "").strip()
            returns_by_brn[code] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bill returns totals failed: %s", exc)
        raise
    return _assemble_branch_rows(sales_rows, returns_by_brn)


def fetch_branch_sales_totals(date_from, date_to, system: str = "pos") -> list[dict]:
    """
    إجماليات مبيعات فرع خلال فترة — SELECT فقط.

    نقاط البيع: من IAS_POS_BILL_MST (عدد BILL_NO غير المعلّقة).
    الآجل: من IAS_BILL_MST حسب نوع المستند.
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    cache_key = (
        f"sales:branches:v7:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:r{int(not _skip_mst_returns(date_from, date_to))}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached
    conf = _system_conf(system)
    if conf.get("source") == "pos":
        rows = _fetch_pos_branch_totals(date_from, date_to)
    else:
        rows = _fetch_bill_branch_totals(date_from, date_to, conf)
    _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows


def fetch_branch_sales_activity(
    date_from,
    date_to,
    branch_code: str = "",
) -> list[dict]:
    """نشاط الفروع من رأس فاتورة POS: ساعات البيع وأيام العمل.

    يقيس الاستمرارية عبر متوسط الساعات النشطة يومياً (فترات HH24 فيها فواتير).
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    cache_key = (
        f"sales:branch_activity:v1:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    pos = _pos_owner()
    params = _date_params(date_from, date_to)
    branch_filter = ""
    if brn:
        params["brn"] = _bind_brn(brn)
        branch_filter = "AND p.BRN_NO = :brn"
    names = _branch_names()
    raw = _fetch_all(
        f"""
        SELECT
            TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
            COUNT(DISTINCT p.BILL_NO) AS INVOICE_COUNT,
            ROUND(SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)), 2) AS SALES_TOTAL,
            COUNT(DISTINCT TRUNC(p.BILL_DATE)) AS ACTIVE_DAYS,
            COUNT(DISTINCT TO_CHAR(p.BILL_DATE, 'YYYYMMDDHH24')) AS ACTIVE_SLOTS,
            MIN(TO_NUMBER(TO_CHAR(p.BILL_DATE, 'HH24'))) AS FIRST_HOUR,
            MAX(TO_NUMBER(TO_CHAR(p.BILL_DATE, 'HH24'))) AS LAST_HOUR
        FROM {pos}.IAS_POS_BILL_MST p
        WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
          AND NVL(p.HUNG, 0) = 0
          AND p.BRN_NO IS NOT NULL
          {branch_filter}
        GROUP BY TO_CHAR(p.BRN_NO)
        """,
        params,
    )
    out: list[dict] = []
    for row in raw:
        code = str(row.get("BRANCH_CODE") or "").strip()
        if not code:
            continue
        invoices = int(row.get("INVOICE_COUNT") or 0)
        sales = round(float(row.get("SALES_TOTAL") or 0), 2)
        days = max(1, int(row.get("ACTIVE_DAYS") or 0))
        slots = int(row.get("ACTIVE_SLOTS") or 0)
        first_h = int(row.get("FIRST_HOUR") or 0)
        last_h = int(row.get("LAST_HOUR") or 0)
        avg_hours = round(slots / days, 1) if days else 0.0
        span_hours = max(0, last_h - first_h + 1)
        # كثافة: فواتير لكل ساعة نشطة
        density = round(invoices / slots, 1) if slots else 0.0
        # درجة استمرارية نسبة ليوم عمل ~14 ساعة
        continuity = round(min(100.0, avg_hours / 14.0 * 100.0), 1)
        out.append(
            {
                "branch_code": code,
                "branch_name": names.get(code) or code,
                "invoice_count": invoices,
                "sales_total": sales,
                "active_days": days,
                "active_slots": slots,
                "avg_hours_per_day": avg_hours,
                "span_hours": span_hours,
                "first_hour": first_h,
                "last_hour": last_h,
                "invoices_per_hour": density,
                "continuity_pct": continuity,
            }
        )
    out.sort(
        key=lambda r: (
            -float(r.get("avg_hours_per_day") or 0),
            -float(r.get("continuity_pct") or 0),
            -int(r.get("invoice_count") or 0),
            str(r.get("branch_name") or ""),
        )
    )
    _sales_cache_set(cache_key, out, date_from=date_from, date_to=date_to)
    return out


def fetch_branch_return_totals(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    limit: int = 15,
) -> list[dict]:
    """فروع مرتبة حسب قيمة المرتجع — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    lim = max(1, min(int(limit or 15), 40))
    cache_key = (
        f"sales:ret_branches:v1:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{lim}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    conf = _system_conf(system)
    params: dict = _date_params(date_from, date_to)
    schema = _schema()
    names = _branch_names()
    rows_raw: list[dict] = []

    if conf.get("source") == "pos":
        pos = _pos_owner()
        branch_filter = ""
        group_filter = ""
        if brn:
            params["brn"] = _bind_brn(brn)
            branch_filter = "AND m.BRN_NO = :brn"
        if gcode:
            params["gcode"] = _bind_gcode(gcode)
            group_filter = "AND i.G_CODE = :gcode"
            rows_raw = _fetch_all(
                f"""
                SELECT
                    TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
                    COUNT(DISTINCT m.RT_BILL_NO) AS RETURN_COUNT,
                    ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS RET_NET,
                    ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {pos}.IAS_POS_RT_BILL_DTL d
                JOIN {pos}.IAS_POS_RT_BILL_MST m
                  ON m.RT_BILL_NO = d.RT_BILL_NO
                 AND m.BRN_NO = d.BRN_NO
                LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
                WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                  AND NVL(m.HUNG, 0) = 0
                  {branch_filter}
                  {group_filter}
                GROUP BY TO_CHAR(m.BRN_NO)
                """,
                params,
            )
        else:
            rows_raw = _fetch_all(
                f"""
                SELECT
                    TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
                    COUNT(DISTINCT m.RT_BILL_NO) AS RETURN_COUNT,
                    ROUND(SUM(NVL(m.RT_BILL_AMT, 0)), 2) AS RET_NET,
                    ROUND(SUM(NVL(m.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {pos}.IAS_POS_RT_BILL_MST m
                WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                  AND NVL(m.HUNG, 0) = 0
                  {branch_filter}
                GROUP BY m.BRN_NO
                """,
                params,
            )
    else:
        ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
        ret_cash = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        branch_filter = ""
        if brn:
            params["brn"] = _bind_brn(brn)
            branch_filter = "AND r.BRN_NO = :brn"
        if gcode:
            params["gcode"] = _bind_gcode(gcode)
            rows_raw = _fetch_all(
                f"""
                SELECT
                    TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                    COUNT(DISTINCT r.RT_BILL_SER) AS RETURN_COUNT,
                    ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS RET_NET,
                    ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {schema}.IAS_RT_BILL_DTL d
                JOIN {schema}.IAS_RT_BILL_MST r
                  ON r.RT_BILL_SER = d.RT_BILL_SER
                 AND r.BRN_NO = d.BRN_NO
                LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
                WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                  AND {_rt_bill_mst_ok("r")}
                  {ret_doc}
                  {ret_cash}
                  {branch_filter}
                  AND i.G_CODE = :gcode
                GROUP BY TO_CHAR(r.BRN_NO)
                """,
                params,
            )
        else:
            rows_raw = _fetch_all(
                f"""
                SELECT
                    TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                    COUNT(DISTINCT r.RT_BILL_SER) AS RETURN_COUNT,
                    ROUND(SUM(NVL(r.BILL_AMT, 0)), 2) AS RET_NET,
                    ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {schema}.IAS_RT_BILL_MST r
                WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                  AND {_rt_bill_mst_ok("r")}
                  {ret_doc}
                  {ret_cash}
                  {branch_filter}
                GROUP BY TO_CHAR(r.BRN_NO)
                """,
                params,
            )

    out: list[dict] = []
    for row in rows_raw:
        code = str(row.get("BRANCH_CODE") or "").strip()
        if not code:
            continue
        ret_net = float(row.get("RET_NET") or 0)
        ret_vat = float(row.get("RET_VAT") or 0)
        total = round(ret_net + ret_vat, 2)
        if total <= 0:
            continue
        out.append(
            {
                "branch_code": code,
                "branch_name": names.get(code) or code,
                "return_count": int(row.get("RETURN_COUNT") or 0),
                "return_total": total,
                # توافق مع واجهة الدونات
                "sales_total": total,
                "invoice_count": int(row.get("RETURN_COUNT") or 0),
            }
        )
    out.sort(key=lambda r: (-r["return_total"], r["branch_code"]))
    out = out[:lim]
    _sales_cache_set(cache_key, out, date_from=date_from, date_to=date_to)
    return out


def fetch_group_return_totals(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    limit: int = 40,
) -> list[dict]:
    """مجموعات مرتبة حسب قيمة المرتجع — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    lim = max(1, min(int(limit or 40), 200))
    cache_key = (
        f"sales:ret_groups:v3:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{lim}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    conf = _system_conf(system)
    params: dict = _date_params(date_from, date_to)
    schema = _schema()
    group_names = {
        str(g.get("code") or "").strip(): str(g.get("name") or "").strip()
        for g in fetch_sales_group_options()
        if str(g.get("code") or "").strip()
    }
    branch_filter = ""
    group_filter = ""
    if brn:
        params["brn"] = _bind_brn(brn)
    if gcode:
        params["gcode"] = _bind_gcode(gcode)
        group_filter = "AND i.G_CODE = :gcode"

    if conf.get("source") == "pos":
        pos = _pos_owner()
        if brn:
            branch_filter = "AND m.BRN_NO = :brn"
        hung_m = _hung_ok("m")
        # رأس المرتجع أولاً + مرحلتان (بدون COUNT DISTINCT)
        rows_raw = _fetch_all(
            f"""
            SELECT * FROM (
              SELECT
                  y.GROUP_CODE AS GROUP_CODE,
                  COUNT(*) AS RETURN_COUNT,
                  ROUND(SUM(y.RET_QTY), 2) AS RET_QTY,
                  ROUND(SUM(y.RET_NET), 2) AS RET_NET,
                  ROUND(SUM(y.RET_VAT), 2) AS RET_VAT
              FROM (
                  SELECT
                      NVL(TO_CHAR(i.G_CODE), '(بلا)') AS GROUP_CODE,
                      m.RT_BILL_NO AS RT_BILL_NO,
                      m.BRN_NO AS BRN_NO,
                      SUM(NVL(d.I_QTY, 0)) AS RET_QTY,
                      SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS RET_NET,
                      SUM(NVL(d.VAT_AMT, 0)) AS RET_VAT
                  FROM (
                      SELECT m.RT_BILL_NO, m.BRN_NO
                      FROM {pos}.IAS_POS_RT_BILL_MST m
                      WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                        AND {hung_m}
                        {branch_filter}
                  ) m
                  JOIN {pos}.IAS_POS_RT_BILL_DTL d
                    ON d.RT_BILL_NO = m.RT_BILL_NO
                   AND d.BRN_NO = m.BRN_NO
                  LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
                  WHERE 1 = 1
                    {group_filter}
                  GROUP BY NVL(TO_CHAR(i.G_CODE), '(بلا)'), m.RT_BILL_NO, m.BRN_NO
              ) y
              GROUP BY y.GROUP_CODE
              ORDER BY SUM(y.RET_NET + y.RET_VAT) DESC
            ) WHERE ROWNUM <= :lim
            """,
            {**params, "lim": lim},
        )
    else:
        ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
        ret_cash = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        if brn:
            branch_filter = "AND r.BRN_NO = :brn"
        rows_raw = _fetch_all(
            f"""
            SELECT * FROM (
              SELECT
                  y.GROUP_CODE AS GROUP_CODE,
                  COUNT(*) AS RETURN_COUNT,
                  ROUND(SUM(y.RET_QTY), 2) AS RET_QTY,
                  ROUND(SUM(y.RET_NET), 2) AS RET_NET,
                  ROUND(SUM(y.RET_VAT), 2) AS RET_VAT
              FROM (
                  SELECT
                      NVL(TO_CHAR(i.G_CODE), '(بلا)') AS GROUP_CODE,
                      r.RT_BILL_SER AS RT_BILL_SER,
                      SUM(NVL(d.I_QTY, 0)) AS RET_QTY,
                      SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS RET_NET,
                      SUM(NVL(d.VAT_AMT, 0)) AS RET_VAT
                  FROM (
                      SELECT r.RT_BILL_SER, r.BRN_NO
                      FROM {schema}.IAS_RT_BILL_MST r
                      WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                        AND {_rt_bill_mst_ok("r")}
                        {ret_doc}
                        {ret_cash}
                        {branch_filter}
                  ) r
                  JOIN {schema}.IAS_RT_BILL_DTL d
                    ON d.RT_BILL_SER = r.RT_BILL_SER
                   AND d.BRN_NO = r.BRN_NO
                  LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
                  WHERE 1 = 1
                    {group_filter}
                  GROUP BY NVL(TO_CHAR(i.G_CODE), '(بلا)'), r.RT_BILL_SER
              ) y
              GROUP BY y.GROUP_CODE
              ORDER BY SUM(y.RET_NET + y.RET_VAT) DESC
            ) WHERE ROWNUM <= :lim
            """,
            {**params, "lim": lim},
        )

    out: list[dict] = []
    for row in rows_raw:
        code = str(row.get("GROUP_CODE") or "").strip() or "(بلا)"
        ret_net = float(row.get("RET_NET") or 0)
        ret_vat = float(row.get("RET_VAT") or 0)
        total = round(ret_net + ret_vat, 2)
        if total <= 0:
            continue
        out.append(
            {
                "group_code": code,
                "group_name": group_names.get(code) or code,
                "return_count": int(row.get("RETURN_COUNT") or 0),
                "return_qty": round(float(row.get("RET_QTY") or 0), 2),
                "net_total": round(ret_net, 2),
                "vat_total": round(ret_vat, 2),
                "return_total": total,
                "sales_total": total,
                "invoice_count": int(row.get("RETURN_COUNT") or 0),
            }
        )
    out.sort(key=lambda r: (-r["return_total"], r["group_code"]))
    out = out[:lim]
    _sales_cache_set(cache_key, out, date_from=date_from, date_to=date_to)
    return out



def fetch_sales_mst_bundle(
    date_from,
    date_to,
    system: str = "pos",
    *,
    light: bool = False,
    top_users_limit: int = 8,
) -> dict[str, Any]:
    """
    مسح واحد لرأس الفواتير (GROUPING SETS): فروع + بائعون + أعداد KPI.
    يقلّل 4–6 استعلامات إلى 1–2 لكل نظام.
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    conf = _system_conf(system)
    # light يقلّل تفاصيل البائعين؛ المرتجعات تُخصم دائماً من صافي الفروع
    skip_ret = _skip_mst_returns(date_from, date_to)
    # fast_split كان مرتبطاً بتخطي المرتجعات — أُلغي مع الإبقاء على الدقة
    fast_split = False
    lim = max(1, min(int(top_users_limit or 8), 50))
    cache_key = (
        f"sales:mst_bundle:v8:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:L{int(light)}:u{lim}:r{int(not skip_ret)}:f{int(fast_split)}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    params = _date_params(date_from, date_to)
    if conf.get("source") == "pos":
        pos = _pos_owner()
        hung = _pos_mst_ok("p")
        # رأس الفاتورة صف واحد لكل فاتورة → COUNT(*) أدق وأسرع من COUNT(DISTINCT)
        # بدون GROUPING للمستخدمين — البائعون عبر fetch_top_sales_users (صافي مرتجعات)
        sales_rows = _fetch_all(
            f"""
            SELECT
              CASE WHEN GROUPING(p.BRN_NO) = 0 THEN 'BRN' ELSE 'TOT' END AS KIND,
              TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
              CAST(NULL AS VARCHAR2(40)) AS USER_CODE,
              COUNT(*) AS INVOICE_COUNT,
              ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
              ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL,
              ROUND(SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)), 2) AS GROSS_TOTAL,
              COUNT(DISTINCT NVL(TO_CHAR(p.MACHINE_NO), TO_CHAR(p.CASH_NO))) AS DEVICE_COUNT,
              COUNT(DISTINCT p.AD_U_ID) AS SELLER_COUNT
            FROM {pos}.IAS_POS_BILL_MST p
            WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
              AND {hung}
            GROUP BY GROUPING SETS ((p.BRN_NO), ())
            """,
            params,
        )
        returns_by_brn: dict[str, tuple[int, float, float]] = {}
        if not skip_ret:
            try:
                for row in _fetch_all(
                    f"""
                    SELECT
                        TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                        COUNT(*) AS RET_COUNT,
                        ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                        ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
                    FROM {pos}.IAS_POS_RT_BILL_MST r
                    WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                      AND {_hung_ok("r")}
                      AND {_bill_amt_ok("r", "RT_BILL_AMT")}
                    GROUP BY TO_CHAR(r.BRN_NO)
                    """,
                    params,
                ):
                    code = str(row.get("BRANCH_CODE") or "").strip()
                    returns_by_brn[code] = (
                        int(row.get("RET_COUNT") or 0),
                        float(row.get("RET_NET") or 0),
                        float(row.get("RET_VAT") or 0),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("POS MST bundle returns failed: %s", exc)
                raise
    else:
        schema = _schema()
        doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
        cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        cncl = _bill_mst_ok("b")
        sales_rows = _fetch_all(
            f"""
            SELECT
              CASE WHEN GROUPING(b.BRN_NO) = 0 THEN 'BRN' ELSE 'TOT' END AS KIND,
              TO_CHAR(b.BRN_NO) AS BRANCH_CODE,
              CAST(NULL AS VARCHAR2(40)) AS USER_CODE,
              COUNT(DISTINCT b.BILL_SER) AS INVOICE_COUNT,
              ROUND(SUM(NVL(b.BILL_AMT, 0)), 2) AS NET_TOTAL,
              ROUND(SUM(NVL(b.VAT_AMT, 0)), 2) AS VAT_TOTAL,
              ROUND(SUM(NVL(b.BILL_AMT, 0) + NVL(b.VAT_AMT, 0)), 2) AS GROSS_TOTAL,
              COUNT(DISTINCT TO_CHAR(b.CASH_NO)) AS DEVICE_COUNT,
              COUNT(DISTINCT b.AD_U_ID) AS SELLER_COUNT
            FROM {schema}.IAS_BILL_MST b
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND {cncl}
              {doc_filter}
              {cash_filter}
            GROUP BY GROUPING SETS ((b.BRN_NO), ())
            """,
            params,
        )
        returns_by_brn = {}
        if not skip_ret:
            try:
                ret_params: dict = _date_params(date_from, date_to)
                ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
                ret_cash = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
                for row in _fetch_all(
                    f"""
                    SELECT
                        TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                        COUNT(DISTINCT r.RT_BILL_SER) AS RET_COUNT,
                        ROUND(SUM(NVL(r.BILL_AMT, 0)), 2) AS RET_NET,
                        ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
                    FROM {schema}.IAS_RT_BILL_MST r
                    WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                      AND {_rt_bill_mst_ok("r")}
                      {ret_doc}
                      {ret_cash}
                    GROUP BY TO_CHAR(r.BRN_NO)
                    """,
                    ret_params,
                ):
                    code = str(row.get("BRANCH_CODE") or "").strip()
                    returns_by_brn[code] = (
                        int(row.get("RET_COUNT") or 0),
                        float(row.get("RET_NET") or 0),
                        float(row.get("RET_VAT") or 0),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Bill MST bundle returns failed: %s", exc)
                raise

    branch_sales = [r for r in sales_rows if str(r.get("KIND") or "") == "BRN"]
    tot_rows = [r for r in sales_rows if str(r.get("KIND") or "") == "TOT"]
    branches = _assemble_branch_rows(branch_sales, returns_by_brn)
    # البائعون تُحمَّل لاحقاً عبر API — لا نضاعف مسح المستخدمين هنا
    top_users: list[dict] = []
    seller_count = int((tot_rows[0].get("SELLER_COUNT") if tot_rows else 0) or 0)
    device_count = int((tot_rows[0].get("DEVICE_COUNT") if tot_rows else 0) or 0)
    if not device_count and conf.get("source") != "pos":
        device_count = 0

    # مزامنة كاش الفروع المنفصل لواجهات أخرى
    br_cache = (
        f"sales:branches:v7:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:r{int(not skip_ret)}"
    )
    _sales_cache_set(br_cache, branches, date_from=date_from, date_to=date_to)
    _sales_cache_set(
        f"sales:seller_count:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}",
        seller_count,
        date_from=date_from,
        date_to=date_to,
    )
    _sales_cache_set(
        f"sales:device_count:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}",
        device_count,
        date_from=date_from,
        date_to=date_to,
    )

    out = {
        "branches": branches,
        "top_users": top_users,
        "seller_count": seller_count,
        "device_count": device_count,
        "fast_mode": skip_ret,
    }
    _sales_cache_set(cache_key, out, date_from=date_from, date_to=date_to)
    return out


def _assemble_daily_rows(sales_rows, returns_by_day) -> list[dict]:
    """صف لكل يوم: فواتير + صافي بعد خصم مرتجعات ذلك اليوم."""
    out: list[dict] = []
    for row in sales_rows:
        day = row.get("SALE_DAY")
        if hasattr(day, "date"):
            day = day.date()
        day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
        ret_count, ret_net, ret_vat = returns_by_day.get(day_key, (0, 0.0, 0.0))
        sales_count = int(row.get("INVOICE_COUNT") or 0)
        gross_net = float(row.get("NET_TOTAL") or 0)
        gross_vat = float(row.get("VAT_TOTAL") or 0)
        gross_total = float(row.get("GROSS_TOTAL") or (gross_net + gross_vat))
        net = gross_net - ret_net
        vat = gross_vat - ret_vat
        sales_total = round(net + vat, 2)
        avg_basket = round(sales_total / sales_count, 2) if sales_count else 0.0
        out.append(
            {
                "day": day_key,
                "day_display": day_key,
                "invoice_count": sales_count,
                "return_count": ret_count,
                "gross_total": round(gross_total, 2),
                "net_total": round(net, 2),
                "vat_total": round(vat, 2),
                "sales_total": sales_total,
                "avg_basket": avg_basket,
            }
        )
    out.sort(key=lambda r: r["day"], reverse=True)
    return out


def _fetch_pos_daily_totals(date_from, date_to, branch_code: str = "") -> list[dict]:
    """إجماليات يومية لنقاط البيع (كل الفروع أو فرع واحد)."""
    pos = _pos_owner()
    params = _date_params(date_from, date_to)
    branch_filter = ""
    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = _bind_brn(brn)
        branch_filter = "AND p.BRN_NO = :brn"
    sales_rows = _fetch_all(
        f"""
        SELECT
            TRUNC(p.BILL_DATE) AS SALE_DAY,
            COUNT(*) AS INVOICE_COUNT,
            ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL,
            ROUND(SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)), 2) AS GROSS_TOTAL
        FROM {pos}.IAS_POS_BILL_MST p
        WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
          AND NVL(p.HUNG, 0) = 0
          {branch_filter}
        GROUP BY TRUNC(p.BILL_DATE)
        ORDER BY TRUNC(p.BILL_DATE) DESC
        """,
        params,
    )
    returns_by_day: dict[str, tuple[int, float, float]] = {}
    if _skip_mst_returns(date_from, date_to):
        return _assemble_daily_rows(sales_rows, returns_by_day)
    try:
        ret_branch = "AND r.BRN_NO = :brn" if brn else ""
        for row in _fetch_all(
            f"""
            SELECT
                TRUNC(r.RT_BILL_DATE) AS SALE_DAY,
                COUNT(*) AS RET_COUNT,
                ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {pos}.IAS_POS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND NVL(r.HUNG, 0) = 0
              {ret_branch}
            GROUP BY TRUNC(r.RT_BILL_DATE)
            """,
            params,
        ):
            day = row.get("SALE_DAY")
            if hasattr(day, "date"):
                day = day.date()
            day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
            returns_by_day[day_key] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("POS daily returns failed: %s", exc)
        raise
    return _assemble_daily_rows(sales_rows, returns_by_day)


def _fetch_bill_daily_totals(
    date_from, date_to, branch_code: str, conf: dict
) -> list[dict]:
    """إجماليات يومية من IAS_BILL_MST (كل الفروع أو فرع واحد)."""
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    branch_filter = ""
    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = _bind_brn(brn)
        branch_filter = "AND b.BRN_NO = :brn"
    sales_rows = _fetch_all(
        f"""
        SELECT
            TRUNC(b.BILL_DATE) AS SALE_DAY,
            COUNT(DISTINCT b.BILL_SER) AS INVOICE_COUNT,
            ROUND(SUM(NVL(b.BILL_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(b.VAT_AMT, 0)), 2) AS VAT_TOTAL,
            ROUND(SUM(NVL(b.BILL_AMT, 0) + NVL(b.VAT_AMT, 0)), 2) AS GROSS_TOTAL
        FROM {schema}.IAS_BILL_MST b
        WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          AND {_bill_mst_ok("b")}
          {doc_filter}
          {cash_filter}
          {branch_filter}
        GROUP BY TRUNC(b.BILL_DATE)
        ORDER BY TRUNC(b.BILL_DATE) DESC
        """,
        params,
    )
    returns_by_day: dict[str, tuple[int, float, float]] = {}
    if _skip_mst_returns(date_from, date_to):
        return _assemble_daily_rows(sales_rows, returns_by_day)
    try:
        ret_params: dict = _date_params(date_from, date_to)
        ret_doc_filter = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash_filter = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        ret_branch = ""
        if brn:
            ret_params["brn"] = _bind_brn(brn)
            ret_branch = "AND r.BRN_NO = :brn"
        for row in _fetch_all(
            f"""
            SELECT
                TRUNC(r.RT_BILL_DATE) AS SALE_DAY,
                COUNT(DISTINCT r.RT_BILL_SER) AS RET_COUNT,
                ROUND(SUM(NVL(r.BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {schema}.IAS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND {_rt_bill_mst_ok("r")}
              {ret_doc_filter}
              {ret_cash_filter}
              {ret_branch}
            GROUP BY TRUNC(r.RT_BILL_DATE)
            """,
            ret_params,
        ):
            day = row.get("SALE_DAY")
            if hasattr(day, "date"):
                day = day.date()
            day_key = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
            returns_by_day[day_key] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bill daily returns failed: %s", exc)
        raise
    return _assemble_daily_rows(sales_rows, returns_by_day)


def fetch_branch_daily_sales_totals(
    date_from,
    date_to,
    branch_code: str,
    system: str = "pos",
) -> list[dict]:
    """إجماليات يومية لفرع واحد خلال الفترة — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    code = str(branch_code or "").strip()
    if not code:
        raise OracleStockError("رقم الفرع مطلوب.")
    conf = _system_conf(system)
    if conf.get("source") == "pos":
        return _fetch_pos_daily_totals(date_from, date_to, code)
    return _fetch_bill_daily_totals(date_from, date_to, code, conf)


def fetch_daily_sales_totals(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
) -> list[dict]:
    """إجماليات يومية للنظام — كل الفروع أو فرع محدد."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    conf = _system_conf(system)
    brn = str(branch_code or "").strip()
    fast = _skip_mst_returns(date_from, date_to)
    cache_key = (
        f"sales:daily:v2:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{'fast' if fast else 'net'}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached
    if conf.get("source") == "pos":
        rows = _fetch_pos_daily_totals(date_from, date_to, brn)
    else:
        rows = _fetch_bill_daily_totals(date_from, date_to, brn, conf)
    _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows


def fetch_sales_group_options() -> list[dict]:
    """قائمة المجموعات للفلتر من GROUP_DETAILS."""
    if not oracle_enabled():
        return []
    hit, cached = _django_lookup_get("group_options")
    if hit:
        return cached
    try:
        rows = _fetch_all(
            f"""
            SELECT TO_CHAR(G_CODE) AS G_CODE, G_A_NAME, G_E_NAME
            FROM {_schema()}.GROUP_DETAILS
            WHERE G_CODE IS NOT NULL
            ORDER BY NVL(G_ORDR, 999999), G_A_NAME, G_CODE
            """
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sales group options unavailable: %s", exc)
        return _django_lookup_set("group_options", [])
    out: list[dict] = []
    for row in rows:
        code = str(row.get("G_CODE") or "").strip()
        if not code:
            continue
        name = str(row.get("G_A_NAME") or row.get("G_E_NAME") or "").strip() or code
        out.append({"code": code, "name": name})
    return _django_lookup_set("group_options", out)


def _group_branch_key(group_code: str, branch_code: str) -> str:
    return f"{group_code}|{branch_code}"


def _group_name_lookup() -> dict[str, str]:
    return {
        str(g.get("code") or "").strip(): str(g.get("name") or "").strip()
        for g in fetch_sales_group_options()
        if str(g.get("code") or "").strip()
    }


def _item_group_code_map() -> dict[str, str]:
    """خريطة I_CODE → G_CODE — تُحمَّل مرة وتُخزَّن لتفادي JOIN على تفاصيل الفواتير."""
    hit, cached = _django_lookup_get("item_group_map:v1")
    if hit and isinstance(cached, dict):
        return cached
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(I_CODE) AS ITEM_CODE,
               NVL(TO_CHAR(G_CODE), '(بلا)') AS GROUP_CODE
        FROM {_schema()}.IAS_ITM_MST
        WHERE I_CODE IS NOT NULL
        """,
        {},
    )
    out: dict[str, str] = {}
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        if not code:
            continue
        out[code] = str(row.get("GROUP_CODE") or "").strip() or "(بلا)"
    return _django_lookup_set("item_group_map:v1", out)


def _fold_pos_item_rows_to_group_sales(
    item_rows: list[dict],
    *,
    by_branch: bool,
    group_code: str = "",
    with_bills: bool = False,
) -> list[dict]:
    """طي صفوف مجمّعة حسب الصنف (ودون JOIN أوراكل) → إجماليات مجموعات.

    with_bills=True: الصفوف تحمل BILL_NO/BILL_SRL لعدّ فواتير دقيق لكل مجموعة.
    """
    gmap = _item_group_code_map()
    want = str(group_code or "").strip()
    # مبالغ: (group[, branch]) → totals
    amt: dict[tuple[str, str], dict[str, float]] = {}
    # فواتير فريدة لكل مجموعة
    bills: dict[tuple[str, str], set] = {}

    for row in item_rows or []:
        item = str(row.get("ITEM_CODE") or "").strip()
        if not item:
            continue
        g_code = gmap.get(item) or "(بلا)"
        if want and str(g_code) != str(want):
            continue
        b_code = str(row.get("BRANCH_CODE") or "").strip() if by_branch else ""
        key = (g_code, b_code)
        bucket = amt.get(key)
        if bucket is None:
            bucket = {"qty": 0.0, "net": 0.0, "vat": 0.0}
            amt[key] = bucket
        bucket["qty"] += float(row.get("QTY_TOTAL") or 0)
        bucket["net"] += float(row.get("NET_TOTAL") or 0)
        bucket["vat"] += float(row.get("VAT_TOTAL") or 0)
        if with_bills:
            bill_no = row.get("BILL_NO")
            if bill_no is None or str(bill_no).strip() == "":
                continue
            srl = row.get("BILL_SRL")
            bset = bills.get(key)
            if bset is None:
                bset = set()
                bills[key] = bset
            bset.add((b_code, str(bill_no), str(srl if srl is not None else 0)))

    sales_rows: list[dict] = []
    for (g_code, b_code), bucket in amt.items():
        inv = len(bills.get((g_code, b_code), ())) if with_bills else 0
        sales_rows.append(
            {
                "GROUP_CODE": g_code,
                "BRANCH_CODE": b_code or None,
                "INVOICE_COUNT": inv,
                "QTY_TOTAL": round(bucket["qty"], 2),
                "NET_TOTAL": round(bucket["net"], 2),
                "VAT_TOTAL": round(bucket["vat"], 2),
            }
        )
    return _assemble_group_rows(sales_rows, {}, by_branch=by_branch)


def _month_spans(date_from, date_to) -> list[tuple[date, date]]:
    """شرائح شهرية نصف مفتوحة زمنياً ضمن [from, to]."""
    from calendar import monthrange

    start = _as_date(date_from)
    end = _as_date(date_to)
    if end < start:
        return []
    cur = start.replace(day=1)
    out: list[tuple[date, date]] = []
    while cur <= end:
        last = date(cur.year, cur.month, monthrange(cur.year, cur.month)[1])
        chunk_from = max(cur, start)
        chunk_to = min(last, end)
        if chunk_from <= chunk_to:
            out.append((chunk_from, chunk_to))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out


def sales_group_chunks_newest_first(date_from, date_to) -> list[dict[str, str]]:
    """شهور التحميل التصاعدي — الأحدث أولاً (الأسهل ثم الأصعب)."""
    spans = _month_spans(date_from, date_to)
    spans.reverse()
    return [
        {"date_from": a.isoformat(), "date_to": b.isoformat()}
        for a, b in spans
    ]


def _assemble_group_rows(sales_rows, returns_by_key, *, by_branch: bool = True) -> list[dict]:
    """صف لكل مجموعة، أو لكل مجموعة×فرع عند by_branch."""
    names = _branch_names()
    group_names = _group_name_lookup()
    out: list[dict] = []
    for row in sales_rows:
        g_code = str(row.get("GROUP_CODE") or "").strip() or "(بلا)"
        g_name = (
            str(row.get("GROUP_NAME") or "").strip()
            or group_names.get(g_code)
            or g_code
        )
        b_code = str(row.get("BRANCH_CODE") or "").strip()
        key = _group_branch_key(g_code, b_code if by_branch else "")
        ret_count, ret_qty, ret_net, ret_vat = returns_by_key.get(
            key, (0, 0.0, 0.0, 0.0)
        )
        inv_count = int(row.get("INVOICE_COUNT") or 0)
        qty = float(row.get("QTY_TOTAL") or 0) - ret_qty
        gross_net = float(row.get("NET_TOTAL") or 0)
        gross_vat = float(row.get("VAT_TOTAL") or 0)
        net = gross_net - ret_net
        vat = gross_vat - ret_vat
        sales_total = round(net + vat, 2)
        if by_branch:
            branch_name = names.get(b_code) or b_code
        elif b_code:
            branch_name = names.get(b_code) or b_code
        else:
            branch_name = "كل الفروع"
        out.append(
            {
                "group_code": g_code,
                "group_name": g_name,
                "branch_code": b_code,
                "branch_name": branch_name,
                "invoice_count": inv_count,
                "return_count": int(ret_count),
                "qty_total": round(qty, 2),
                "gross_total": round(gross_net + gross_vat, 2),
                "net_total": round(net, 2),
                "vat_total": round(vat, 2),
                "sales_total": sales_total,
                "avg_basket": round(sales_total / inv_count, 2) if inv_count else 0.0,
            }
        )
    out.sort(
        key=lambda r: (
            -r["sales_total"],
            r["group_name"],
            r["branch_name"],
            r["group_code"],
            r["branch_code"],
        )
    )
    return out


def _fetch_pos_group_totals(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
    by_branch: bool = False,
    skip_returns: bool = False,
    _allow_fanout: bool = True,
) -> list[dict]:
    """مبيعات نقاط البيع مجمّعة حسب المجموعة (أو المجموعة×الفرع).

    تجميع على مرحلتين بدل COUNT(DISTINCT نص طويل) لتقليل TEMP.
    أسماء المجموعات تُحلّ في بايثون من GROUP_DETAILS المخزّن مؤقتاً.
    للفترة الطويلة بدون فرع: تقسيم الفروع بالتوازي (أسرع بكثير من مسح واحد).
    """
    pos = _pos_owner()
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    group_filter = ""
    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = brn
        branch_filter = "AND m.BRN_NO = :brn"
    if group_code:
        params["gcode"] = _bind_gcode(group_code)
        group_filter = "AND i.G_CODE = :gcode"

    if by_branch:
        outer_branch = "TO_CHAR(x.BRN_NO) AS BRANCH_CODE,"
        outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)'), x.BRN_NO"
        ret_outer_branch = "TO_CHAR(x.BRN_NO) AS BRANCH_CODE,"
        ret_outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)'), x.BRN_NO"
    else:
        outer_branch = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
        outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)')"
        ret_outer_branch = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
        ret_outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)')"

    hung_m = _hung_ok("m")
    span = _date_span_days(date_from, date_to)

    # فترات طويلة: شهور متسلسلة (لا تداخل مع توازي الفروع — أقل ضغطاً على أوراكل)
    if (
        skip_returns
        and _allow_fanout
        and not brn
        and span > 45
    ):
        months = _month_spans(date_from, date_to)
        if len(months) >= 2:
            import time as _time

            parts: list = []
            deadline = _time.monotonic() + min(420.0, 70.0 * len(months))
            for df, dt in months:
                if _time.monotonic() > deadline:
                    logger.warning(
                        "POS group month fanout budget exhausted after %s/%s",
                        len(parts),
                        len(months),
                    )
                    break
                # كاش الشهر أولاً — بدون أوراكل إن وُجد
                part, mkey = _groups_cache_lookup(
                    "pos",
                    df,
                    dt,
                    "",
                    str(group_code or ""),
                    by_branch,
                    "gross",
                    allow_stale=True,
                )
                if part is None:
                    try:
                        # شهر واحد: تفرّع فروع داخلي (أسرع من مسح كل الفروع دفعة)
                        part = _fetch_pos_group_totals(
                            df,
                            dt,
                            branch_code="",
                            group_code=group_code,
                            by_branch=by_branch,
                            skip_returns=True,
                            _allow_fanout=True,
                        )
                        if part:
                            _sales_cache_set(
                                mkey, part, date_from=df, date_to=dt
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "POS group month %s→%s failed: %s", df, dt, exc
                        )
                        part = []
                parts.append(part or [])
            if any(parts):
                return _merge_group_total_parts(parts, by_branch=by_branch)

    # تفرّع الفروع من ~أسبوع فأكثر — شهر كامل بمسح واحد بطيء جداً على كل الفروع
    if (
        skip_returns
        and _allow_fanout
        and not brn
        and span >= 8
    ):
        try:
            branches = _pos_branches_in_range(date_from, date_to)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, OracleStockError) or _is_connect_timeout(exc):
                logger.warning(
                    "POS group branches-in-range failed; single scan: %s", exc
                )
                branches = []
            else:
                logger.warning("POS group branches-in-range failed: %s", exc)
                branches = []
        if len(branches) >= 2:

            def _one(b: str) -> list[dict]:
                try:
                    with oracle_session():
                        return _fetch_pos_group_totals(
                            date_from,
                            date_to,
                            branch_code=b,
                            group_code=group_code,
                            by_branch=by_branch,
                            skip_returns=True,
                            _allow_fanout=False,
                        )
                except Exception as exc:  # noqa: BLE001
                    if _is_connect_timeout(exc) or _is_disconnect_error(exc):
                        logger.warning("POS group branch %s failed: %s", b, exc)
                        return []
                    logger.warning("POS group branch %s error: %s", b, exc)
                    return []

            # شهر: عمال أكثر بعد تسريع الاستعلام (بلا JOIN أصناف)
            if span <= 31:
                workers = min(6, len(branches))
                job_timeout = 75.0 if span <= 16 else 110.0
            elif span <= 62:
                workers = min(4, len(branches))
                job_timeout = 140.0
            else:
                workers = min(3, len(branches))
                job_timeout = 180.0
            # حمّل خريطة الأصناف مرة قبل التفرّع
            try:
                _item_group_code_map()
            except Exception:
                pass
            parts = _run_parallel_ex(
                [lambda b=code: _one(b) for code in branches],
                max_workers=workers,
                timeout_sec=job_timeout,
                soft_fail=True,
            )
            if any(parts):
                return _merge_group_total_parts(parts, by_branch=by_branch)
            logger.warning(
                "POS group branch fanout empty (%s branches); falling back to single scan",
                len(branches),
            )

    if skip_returns:
        # مسار سريع: بلا JOIN على IAS_ITM_MST — تجميع حسب الصنف ثم طي المجموعة في بايثون.
        # مع فرع محدد: صف لكل (صنف×فاتورة) لعدّ فواتير دقيق؛ بدون فرع: صف لكل صنف فقط.
        if by_branch:
            branch_sel = "TO_CHAR(m.BRN_NO) AS BRANCH_CODE,"
            branch_grp = ", m.BRN_NO"
        else:
            branch_sel = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
            branch_grp = ""

        # فرع واحد (أو تفرّع): أدرج مفتاح الفاتورة لعدّ صحيح بدون JOIN أصناف
        with_bills = bool(brn) and not by_branch
        if with_bills:
            item_rows = _fetch_all(
                f"""
                SELECT
                    TO_CHAR(d.I_CODE) AS ITEM_CODE,
                    {branch_sel}
                    m.BILL_NO AS BILL_NO,
                    NVL(m.BILL_SRL, 0) AS BILL_SRL,
                    SUM(NVL(d.I_QTY, 0)) AS QTY_TOTAL,
                    SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS NET_TOTAL,
                    SUM(NVL(d.VAT_AMT, 0)) AS VAT_TOTAL
                FROM {pos}.IAS_POS_BILL_MST m
                JOIN {pos}.IAS_POS_BILL_DTL d
                  ON d.BILL_NO = m.BILL_NO
                 AND d.BRN_NO = m.BRN_NO
                 AND NVL(d.BILL_SRL, 0) = NVL(m.BILL_SRL, 0)
                WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                  AND {hung_m}
                  AND d.I_CODE IS NOT NULL
                  {branch_filter}
                GROUP BY TO_CHAR(d.I_CODE), m.BILL_NO, NVL(m.BILL_SRL, 0){branch_grp}
                """,
                params,
            )
        else:
            item_rows = _fetch_all(
                f"""
                SELECT
                    TO_CHAR(d.I_CODE) AS ITEM_CODE,
                    {branch_sel}
                    SUM(NVL(d.I_QTY, 0)) AS QTY_TOTAL,
                    SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS NET_TOTAL,
                    SUM(NVL(d.VAT_AMT, 0)) AS VAT_TOTAL
                FROM {pos}.IAS_POS_BILL_MST m
                JOIN {pos}.IAS_POS_BILL_DTL d
                  ON d.BILL_NO = m.BILL_NO
                 AND d.BRN_NO = m.BRN_NO
                 AND NVL(d.BILL_SRL, 0) = NVL(m.BILL_SRL, 0)
                WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                  AND {hung_m}
                  AND d.I_CODE IS NOT NULL
                  {branch_filter}
                GROUP BY TO_CHAR(d.I_CODE){branch_grp}
                """,
                params,
            )
        return _fold_pos_item_rows_to_group_sales(
            item_rows,
            by_branch=by_branch,
            group_code=str(group_code or ""),
            with_bills=with_bills,
        )

    sales_sql = f"""
        SELECT
            NVL(TO_CHAR(x.G_CODE), '(بلا)') AS GROUP_CODE,
            {outer_branch}
            COUNT(*) AS INVOICE_COUNT,
            ROUND(SUM(x.QTY_TOTAL), 2) AS QTY_TOTAL,
            ROUND(SUM(x.NET_TOTAL), 2) AS NET_TOTAL,
            ROUND(SUM(x.VAT_TOTAL), 2) AS VAT_TOTAL
        FROM (
            SELECT
                i.G_CODE,
                m.BRN_NO,
                m.BILL_NO,
                NVL(m.BILL_SRL, 0) AS BILL_SRL,
                SUM(NVL(d.I_QTY, 0)) AS QTY_TOTAL,
                SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS NET_TOTAL,
                SUM(NVL(d.VAT_AMT, 0)) AS VAT_TOTAL
            FROM {pos}.IAS_POS_BILL_DTL d
            JOIN {pos}.IAS_POS_BILL_MST m
              ON m.BILL_NO = d.BILL_NO
             AND m.BRN_NO = d.BRN_NO
             AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
              AND {hung_m}
              {branch_filter}
              {group_filter}
            GROUP BY i.G_CODE, m.BRN_NO, m.BILL_NO, NVL(m.BILL_SRL, 0)
        ) x
        GROUP BY {outer_group}
        """
    # تجميع على مرحلتين يعطي عدد الفواتير الصحيح + خصم المرتجع دائماً
    sales_rows = _fetch_all(sales_sql, params)

    returns_by_key: dict[str, tuple[int, float, float, float]] = {}
    try:
        ret_params = dict(params)
        ret_branch = "AND m.BRN_NO = :brn" if branch_code else ""
        ret_group = "AND i.G_CODE = :gcode" if group_code else ""
        for row in _fetch_all(
            f"""
            SELECT
                NVL(TO_CHAR(x.G_CODE), '(بلا)') AS GROUP_CODE,
                {ret_outer_branch}
                COUNT(*) AS RET_COUNT,
                ROUND(SUM(x.RET_QTY), 2) AS RET_QTY,
                ROUND(SUM(x.RET_NET), 2) AS RET_NET,
                ROUND(SUM(x.RET_VAT), 2) AS RET_VAT
            FROM (
                SELECT
                    i.G_CODE,
                    m.BRN_NO,
                    m.RT_BILL_NO,
                    SUM(NVL(d.I_QTY, 0)) AS RET_QTY,
                    SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS RET_NET,
                    SUM(NVL(d.VAT_AMT, 0)) AS RET_VAT
                FROM {pos}.IAS_POS_RT_BILL_DTL d
                JOIN {pos}.IAS_POS_RT_BILL_MST m
                  ON m.RT_BILL_NO = d.RT_BILL_NO
                 AND m.BRN_NO = d.BRN_NO
                LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
                WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                  AND {_hung_ok("m")}
                  {ret_branch}
                  {ret_group}
                GROUP BY i.G_CODE, m.BRN_NO, m.RT_BILL_NO
            ) x
            GROUP BY {ret_outer_group}
            """,
            ret_params,
        ):
            g_code = str(row.get("GROUP_CODE") or "").strip() or "(بلا)"
            b_code = str(row.get("BRANCH_CODE") or "").strip()
            returns_by_key[_group_branch_key(g_code, b_code if by_branch else "")] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_QTY") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("POS group returns failed: %s", exc)
        raise
    return _assemble_group_rows(sales_rows, returns_by_key, by_branch=by_branch)


def _fetch_bill_group_totals(
    date_from,
    date_to,
    conf: dict,
    branch_code: str = "",
    group_code: str = "",
    by_branch: bool = False,
    skip_returns: bool = False,
) -> list[dict]:
    """مبيعات الآجل مجمّعة حسب المجموعة (أو المجموعة×الفرع)."""
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    branch_filter = ""
    group_filter = ""
    if branch_code:
        params["brn"] = branch_code
        branch_filter = "AND b.BRN_NO = :brn"
    if group_code:
        params["gcode"] = _bind_gcode(group_code)
        group_filter = "AND i.G_CODE = :gcode"

    if by_branch:
        outer_branch = "TO_CHAR(x.BRN_NO) AS BRANCH_CODE,"
        outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)'), x.BRN_NO"
        ret_outer_branch = "TO_CHAR(x.BRN_NO) AS BRANCH_CODE,"
        ret_outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)'), x.BRN_NO"
    else:
        outer_branch = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
        outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)')"
        ret_outer_branch = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
        ret_outer_group = "NVL(TO_CHAR(x.G_CODE), '(بلا)')"

    sales_sql = f"""
        SELECT
            NVL(TO_CHAR(x.G_CODE), '(بلا)') AS GROUP_CODE,
            {outer_branch}
            COUNT(*) AS INVOICE_COUNT,
            ROUND(SUM(x.QTY_TOTAL), 2) AS QTY_TOTAL,
            ROUND(SUM(x.NET_TOTAL), 2) AS NET_TOTAL,
            ROUND(SUM(x.VAT_TOTAL), 2) AS VAT_TOTAL
        FROM (
            SELECT
                i.G_CODE,
                b.BRN_NO,
                b.BILL_SER,
                SUM(NVL(d.I_QTY, 0)) AS QTY_TOTAL,
                SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS NET_TOTAL,
                SUM(NVL(d.VAT_AMT, 0)) AS VAT_TOTAL
            FROM {schema}.IAS_BILL_DTL d
            JOIN {schema}.IAS_BILL_MST b
              ON b.BILL_NO = d.BILL_NO
             AND b.BILL_SER = d.BILL_SER
             AND b.BILL_DOC_TYPE = d.BILL_DOC_TYPE
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND {_bill_mst_ok("b")}
              AND d.I_CODE IS NOT NULL
              {doc_filter}
              {cash_filter}
              {branch_filter}
              {group_filter}
            GROUP BY i.G_CODE, b.BRN_NO, b.BILL_SER
        ) x
        GROUP BY {outer_group}
        """
    sales_rows = _fetch_all(sales_sql, params)

    returns_by_key: dict[str, tuple[int, float, float, float]] = {}
    if skip_returns:
        return _assemble_group_rows(sales_rows, returns_by_key, by_branch=by_branch)
    try:
        ret_params: dict = _date_params(date_from, date_to)
        ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        ret_branch = ""
        ret_group = ""
        if branch_code:
            ret_params["brn"] = _bind_brn(branch_code)
            ret_branch = "AND r.BRN_NO = :brn"
        if group_code:
            ret_params["gcode"] = _bind_gcode(group_code)
            ret_group = "AND i.G_CODE = :gcode"
        for row in _fetch_all(
            f"""
            SELECT
                NVL(TO_CHAR(x.G_CODE), '(بلا)') AS GROUP_CODE,
                {ret_outer_branch}
                COUNT(*) AS RET_COUNT,
                ROUND(SUM(x.RET_QTY), 2) AS RET_QTY,
                ROUND(SUM(x.RET_NET), 2) AS RET_NET,
                ROUND(SUM(x.RET_VAT), 2) AS RET_VAT
            FROM (
                SELECT
                    i.G_CODE,
                    r.BRN_NO,
                    r.RT_BILL_SER,
                    SUM(NVL(d.I_QTY, 0)) AS RET_QTY,
                    SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS RET_NET,
                    SUM(NVL(d.VAT_AMT, 0)) AS RET_VAT
                FROM {schema}.IAS_RT_BILL_DTL d
                JOIN {schema}.IAS_RT_BILL_MST r
                  ON r.RT_BILL_SER = d.RT_BILL_SER
                 AND r.BRN_NO = d.BRN_NO
                LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
                WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                  AND {_rt_bill_mst_ok("r")}
                  {ret_doc}
                  {ret_cash}
                  {ret_branch}
                  {ret_group}
                GROUP BY i.G_CODE, r.BRN_NO, r.RT_BILL_SER
            ) x
            GROUP BY {ret_outer_group}
            """,
            ret_params,
        ):
            g_code = str(row.get("GROUP_CODE") or "").strip() or "(بلا)"
            b_code = str(row.get("BRANCH_CODE") or "").strip()
            returns_by_key[_group_branch_key(g_code, b_code if by_branch else "")] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_QTY") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bill group returns failed: %s", exc)
        raise
    return _assemble_group_rows(sales_rows, returns_by_key, by_branch=by_branch)


def _groups_cache_key(
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
    *,
    version: str = "v21",
) -> str:
    return (
        f"sales:groups:{version}:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{int(split_by_branch)}:{mode}"
    )


def _groups_cache_lookup(
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
    *,
    allow_stale: bool = True,
):
    """يقرأ v21 ثم v20/v19 للتوافق مع كاش سابق."""
    for ver in ("v21", "v20", "v19"):
        key = _groups_cache_key(
            system,
            date_from,
            date_to,
            brn,
            gcode,
            split_by_branch,
            mode,
            version=ver,
        )
        hit = _sales_cache_get(key)
        if hit is not None:
            return hit, key
        if allow_stale:
            stale = _sales_cache_get_stale(key)
            if stale is not None:
                return stale, key
    return None, _groups_cache_key(
        system, date_from, date_to, brn, gcode, split_by_branch, mode
    )


def _try_merge_monthly_group_cache(
    *,
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
) -> list[dict] | None:
    """يجمع الفترة من كاش الشهور الجاهزة بالكامل — بدون لمس أوراكل."""
    merged, missing = _merge_available_monthly_group_cache(
        system=system,
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=split_by_branch,
        mode=mode,
    )
    if merged is None or missing:
        return None
    return merged


def _groups_month_json_key(
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
) -> str:
    """مفتاح JSON لشهر واحد: نتيجة SQL مخزّنة ثم تُجمَّع لاحقاً."""
    return (
        f"sales:groups:monthjson:v1:{system}:"
        f"{_as_date(date_from).isoformat()}:{_as_date(date_to).isoformat()}:"
        f"{brn}:{gcode}:{int(split_by_branch)}:{mode}"
    )


def _normalize_group_rows_json(rows: list[dict] | None) -> list[dict]:
    """صفوف مجموعات قابلة للتسلسل JSON (بدون كائنات أوراكل)."""
    out: list[dict] = []
    for row in rows or []:
        out.append(
            {
                "group_code": str(row.get("group_code") or "").strip() or "(بلا)",
                "group_name": str(row.get("group_name") or "").strip(),
                "branch_code": str(row.get("branch_code") or "").strip(),
                "branch_name": str(row.get("branch_name") or "").strip(),
                "invoice_count": int(row.get("invoice_count") or 0),
                "return_count": int(row.get("return_count") or 0),
                "qty_total": round(float(row.get("qty_total") or 0), 2),
                "gross_total": round(float(row.get("gross_total") or 0), 2),
                "net_total": round(float(row.get("net_total") or 0), 2),
                "vat_total": round(float(row.get("vat_total") or 0), 2),
                "sales_total": round(float(row.get("sales_total") or 0), 2),
                "avg_basket": round(float(row.get("avg_basket") or 0), 2),
            }
        )
    return out


def _save_groups_month_json(
    *,
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
    rows: list[dict],
) -> list[dict]:
    """SQL → JSON في الكاش."""
    import json as _json

    clean = _normalize_group_rows_json(rows)
    payload = {
        "v": 1,
        "source": "sql",
        "system": system,
        "mode": mode,
        "date_from": _as_date(date_from).isoformat(),
        "date_to": _as_date(date_to).isoformat(),
        "branch_code": brn,
        "group_code": gcode,
        "by_branch": int(split_by_branch),
        "row_count": len(clean),
        "sales_total": _group_rows_sales_sum(clean),
        "rows": clean,
    }
    key = _groups_month_json_key(
        system, date_from, date_to, brn, gcode, split_by_branch, mode
    )
    raw = _json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # TTL طويل للشهور المكتملة — تُجمَّع لاحقاً بدون أوراكل
    try:
        cache.set(key, raw, 60 * 60 * 24 * 14)
    except Exception:
        _sales_cache_set(key, payload, date_from=date_from, date_to=date_to)
    # توافق مع مسار الكاش القديم السابق
    legacy_key = _groups_cache_key(
        system, date_from, date_to, brn, gcode, split_by_branch, mode
    )
    _sales_cache_set(legacy_key, clean, date_from=date_from, date_to=date_to)
    return clean


def _load_groups_month_json(
    *,
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
) -> list[dict] | None:
    """قراءة JSON شهر من الكاش — بلا أوراكل."""
    import json as _json

    key = _groups_month_json_key(
        system, date_from, date_to, brn, gcode, split_by_branch, mode
    )
    hit = _sales_cache_get(key)
    if hit is None:
        hit = _sales_cache_get_stale(key)
    if isinstance(hit, str):
        try:
            hit = _json.loads(hit)
        except Exception:
            hit = None
    if isinstance(hit, dict) and isinstance(hit.get("rows"), list):
        return _normalize_group_rows_json(hit.get("rows"))
    # توافق: كاش قديم كقائمة صفوف
    legacy, _ = _groups_cache_lookup(
        system,
        date_from,
        date_to,
        brn,
        gcode,
        split_by_branch,
        mode,
        allow_stale=True,
    )
    if legacy is not None:
        return _normalize_group_rows_json(legacy)
    return None


def _aggregate_groups_from_month_json(
    *,
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
) -> tuple[list[dict] | None, list[tuple[date, date]], int, int]:
    """تجميع مبيعات المجموعات من JSON الشهور في الكاش فقط.

    يُرجع: (الصفوف المجمّعة أو None، الشهور الناقصة، عدد الجاهز، الإجمالي).
    """
    months = _month_spans(date_from, date_to)
    if not months:
        return None, [], 0, 0
    parts: list[list[dict]] = []
    missing: list[tuple[date, date]] = []
    for a, b in months:
        part = _load_groups_month_json(
            system=system,
            date_from=a,
            date_to=b,
            brn=brn,
            gcode=gcode,
            split_by_branch=split_by_branch,
            mode=mode,
        )
        if part is None:
            missing.append((a, b))
        else:
            parts.append(part)
    ready = len(parts)
    total = len(months)
    try:
        _tls.groups_months_ready = ready
        _tls.groups_months_total = total
    except Exception:
        pass
    if not parts:
        return None, missing, ready, total
    # شهر واحد ضمن فترة قصيرة: لا حاجة لدمج
    if total == 1:
        return parts[0], missing, ready, total
    return (
        _merge_group_total_parts(parts, by_branch=split_by_branch),
        missing,
        ready,
        total,
    )


def pop_groups_months_progress() -> tuple[int, int]:
    """(جاهز، إجمالي) لشريحة JSON الشهرية — يُستهلك مرة واحدة."""
    ready = int(getattr(_tls, "groups_months_ready", 0) or 0)
    total = int(getattr(_tls, "groups_months_total", 0) or 0)
    try:
        _tls.groups_months_ready = 0
        _tls.groups_months_total = 0
    except Exception:
        pass
    return ready, total


def _merge_available_monthly_group_cache(
    *,
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    mode: str,
) -> tuple[list[dict] | None, list[tuple[date, date]]]:
    """دمج JSON الشهور من الكاش + قائمة الشهور الناقصة."""
    months = _month_spans(date_from, date_to)
    if len(months) < 2:
        # فترة قصيرة: جرّب JSON الشهر/الفترة نفسها
        part = _load_groups_month_json(
            system=system,
            date_from=date_from,
            date_to=date_to,
            brn=brn,
            gcode=gcode,
            split_by_branch=split_by_branch,
            mode=mode,
        )
        if part is not None:
            try:
                _tls.groups_months_ready = 1
                _tls.groups_months_total = 1
            except Exception:
                pass
            return part, []
        return None, []
    merged, missing, _ready, _total = _aggregate_groups_from_month_json(
        system=system,
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=split_by_branch,
        mode=mode,
    )
    return merged, missing


def _fetch_one_month_group_totals(
    *,
    system: str,
    date_from,
    date_to,
    brn: str,
    gcode: str,
    split_by_branch: bool,
    fast: bool,
) -> list[dict]:
    """1) اقرأ JSON من الكاش  2) وإلا SQL → JSON  3) أعد الصفوف للتجميع.

    على WAN (الإنتاج): مسح شهر واحد باستعلام واحد أسرع من تفرّع عشرات الفروع
    (كل فرع = اتصال + رحلة شبكة).
    """
    mode = "gross" if fast else "net"
    cached = _load_groups_month_json(
        system=system,
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=split_by_branch,
        mode=mode,
    )
    if cached is not None:
        return cached
    # بلا تفرّع فروع على WAN: استعلام شهر واحد أسرع من عشرات الاتصالات
    if _system_conf(system).get("source") == "pos":
        rows = _fetch_pos_group_totals(
            date_from,
            date_to,
            branch_code=brn,
            group_code=gcode,
            by_branch=split_by_branch,
            skip_returns=fast,
            _allow_fanout=False,
        )
    else:
        with oracle_session():
            rows = _fetch_bill_group_totals(
                date_from,
                date_to,
                _system_conf(system),
                brn,
                gcode,
                by_branch=split_by_branch,
                skip_returns=fast,
            )
    return _save_groups_month_json(
        system=system,
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=split_by_branch,
        mode=mode,
        rows=rows,
    )


def _schedule_groups_monthly_warm(
    date_from,
    date_to,
    *,
    system: str,
    branch_code: str,
    group_code: str,
    by_branch: bool,
    force_fast: bool,
    cache_key: str,
    missing_months: list[tuple[date, date]] | None = None,
) -> None:
    """تدفئة شهور ناقصة ثم دمج كاش الفترة الكاملة — في الخلفية."""
    months = missing_months or _month_spans(date_from, date_to)
    if not months:
        return
    # الأحدث أولاً — يظهر أثره أسرع عند إعادة التحميل
    months = list(reversed(months))
    lock_key = f"{cache_key}:monthly-warm"
    try:
        # قفل أقصر حتى لا تُعلَّق التدفئة 15د بعد تعثّر خيط/إعادة تشغيل
        if not cache.add(lock_key, 1, 300):
            if cache.get(lock_key):
                return
            cache.set(lock_key, 1, 300)
    except Exception:
        return

    def _job():
        try:
            def _warm_one(pair: tuple[date, date]) -> None:
                a, b = pair
                try:
                    _fetch_one_month_group_totals(
                        system=system,
                        date_from=a,
                        date_to=b,
                        brn=branch_code,
                        gcode=group_code,
                        split_by_branch=by_branch,
                        fast=force_fast,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "monthly groups warm %s→%s failed: %s", a, b, exc
                    )

            # حتى 3 شهور بالتوازي — كل شهر استعلام واحد (مناسب لـ WAN)
            _run_parallel_ex(
                [lambda m=pair: _warm_one(m) for pair in months],
                max_workers=min(3, len(months)),
                timeout_sec=min(600.0, 200.0 * len(months)),
                soft_fail=True,
            )
            # بعد كل دفعة: دمج الكاش فوراً حتى يظهر التقدّم عند الاستطلاع
            mode = "gross" if force_fast else "net"
            merged, still_missing = _merge_available_monthly_group_cache(
                system=system,
                date_from=date_from,
                date_to=date_to,
                brn=branch_code,
                gcode=group_code,
                split_by_branch=by_branch,
                mode=mode,
            )
            if merged is not None and not still_missing:
                _sales_cache_set(
                    cache_key, merged, date_from=date_from, date_to=date_to
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("monthly groups warm job failed: %s", exc)
        finally:
            try:
                cache.delete(lock_key)
            except Exception:
                pass

    try:
        threading.Thread(
            target=_job, name="groups-monthly-warm", daemon=True
        ).start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not start monthly groups warm: %s", exc)
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def peek_group_sales_totals(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    by_branch: bool | None = None,
    force_fast: bool | None = None,
) -> list[dict] | None:
    """قراءة كاش فقط (حي / قديم / دمج شهور) — بلا أوراكل."""
    if not oracle_enabled():
        return None
    conf = _system_conf(system)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    split_by_branch = bool(by_branch) if by_branch is not None else bool(gcode)
    if force_fast is None:
        fast = conf.get("source") == "pos"
    else:
        fast = bool(force_fast)
    mode = "gross" if fast else "net"
    cached, cache_key = _groups_cache_lookup(
        system,
        date_from,
        date_to,
        brn,
        gcode,
        split_by_branch,
        mode,
        allow_stale=False,
    )
    if cached is not None:
        return cached
    merged, missing = _merge_available_monthly_group_cache(
        system=system,
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=split_by_branch,
        mode=mode,
    )
    if merged is not None and not missing:
        _sales_cache_set(cache_key, merged, date_from=date_from, date_to=date_to)
        return merged
    if merged is not None:
        # جزئي من الشهور الجاهزة — أفضل من انتظار السنة كاملة
        return merged
    stale, _ = _groups_cache_lookup(
        system,
        date_from,
        date_to,
        brn,
        gcode,
        split_by_branch,
        mode,
        allow_stale=True,
    )
    return stale


def _group_rows_sales_sum(rows: list[dict] | None) -> float:
    return round(sum(float(r.get("sales_total") or 0) for r in (rows or [])), 2)


def _pick_richer_group_rows(
    primary: list[dict] | None,
    secondary: list[dict] | None,
) -> list[dict] | None:
    """اختر أغنى صفوف من الكاش (حسب إجمالي المبيعات)."""
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    if _group_rows_sales_sum(secondary) > _group_rows_sales_sum(primary) * 1.01:
        return secondary
    return primary


def fetch_group_sales_totals(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    by_branch: bool | None = None,
    force_fast: bool | None = None,
) -> list[dict]:
    """إجماليات المبيعات حسب المجموعة.

    أونكس/فواتير: صافي بعد خصم المرتجع (ليطابق تقرير مبيعات الأصناف في أونكس).
    نقاط البيع: إجمالي بدون مرتجع افتراضياً (المرتجع يُعرض منفصلاً) إلا مع force_fast=False.
    مسار سريع: كاش حي → دمج شهور من الكاش → كاش قديم → أوراكل مرة واحدة.
    للسنة/الفترات الطويلة: لا يُمسح العام دفعة واحدة — شهور من الكاش + أحدث شهر ثم تدفئة خلفية.
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    conf = _system_conf(system)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    split_by_branch = bool(by_branch) if by_branch is not None else bool(gcode)
    if force_fast is None:
        # أونكس والآجل: صافي؛ نقاط البيع: إجمالي سريع
        fast = conf.get("source") == "pos"
    else:
        fast = bool(force_fast)
    mode = "gross" if fast else "net"
    months = _month_spans(date_from, date_to)
    long_range = sales_long_range(date_from, date_to) or len(months) >= 2
    cache_key = _groups_cache_key(
        system, date_from, date_to, brn, gcode, split_by_branch, mode
    )

    def _monthly_merge():
        return _merge_available_monthly_group_cache(
            system=system,
            date_from=date_from,
            date_to=date_to,
            brn=brn,
            gcode=gcode,
            split_by_branch=split_by_branch,
            mode=mode,
        )

    def _period_cached(allow_stale: bool = True):
        hit, _ = _groups_cache_lookup(
            system,
            date_from,
            date_to,
            brn,
            gcode,
            split_by_branch,
            mode,
            allow_stale=allow_stale,
        )
        return hit

    def _return_complete(rows: list[dict]) -> list[dict]:
        try:
            _tls.groups_stale = False
            _tls.groups_incomplete = False
            _tls.groups_warning = ""
            _tls.groups_months_ready = len(months)
            _tls.groups_months_total = len(months)
        except Exception:
            pass
        _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
        return rows

    def _return_partial(
        rows: list[dict],
        missing: list[tuple[date, date]],
        *,
        note: str | None = None,
    ) -> list[dict]:
        done = max(0, len(months) - len(missing))
        try:
            _tls.groups_months_ready = done
            _tls.groups_months_total = len(months)
        except Exception:
            pass
        msg = note or (
            f"JSON كاش {done}/{len(months)} شهر — يُجلب بالـ SQL ويُجمَّع تلقائياً"
        )
        _mark_groups_partial(msg)
        _schedule_groups_monthly_warm(
            date_from,
            date_to,
            system=system,
            branch_code=brn,
            group_code=gcode,
            by_branch=split_by_branch,
            force_fast=fast,
            cache_key=cache_key,
            missing_months=missing,
        )
        return rows

    # فترات طويلة: أظهر الكاش فوراً — لا تنتظر SQL الشهر في طلب HTTP
    if long_range:
        merged_chk, missing_chk = _monthly_merge()
        if merged_chk is not None and not missing_chk:
            return _return_complete(merged_chk)

        period_hit = _period_cached(allow_stale=True)
        display = _pick_richer_group_rows(merged_chk, period_hit)

        # يوجد كاش جزئي: أرجعه فوراً + دفّئ الناقص في الخلفية (بلا انتظار 50ث)
        if display is not None:
            still = list(missing_chk or [])
            if not still and merged_chk is not None:
                return _return_complete(merged_chk)
            if not still:
                _, still = _monthly_merge()
            if not still:
                return _return_complete(
                    merged_chk if merged_chk is not None else display
                )
            return _return_partial(display, still)

        # لا كاش أصلاً: اجلب أحدث شهر فقط ثم أرجعه (أول زيارة باردة)
        newest = months[-1] if months else None
        rows: list[dict] = []
        if newest:
            try:
                rows = _fetch_one_month_group_totals(
                    system=system,
                    date_from=newest[0],
                    date_to=newest[1],
                    brn=brn,
                    gcode=gcode,
                    split_by_branch=split_by_branch,
                    fast=fast,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("newest-month groups fetch failed: %s", exc)
                rows = []
            merged_chk, missing_chk = _monthly_merge()
            if merged_chk is not None and not missing_chk:
                return _return_complete(merged_chk)
            if merged_chk is not None:
                return _return_partial(merged_chk, missing_chk)
        missing = [m for m in months if m != newest] if newest else list(months)
        if rows:
            return _return_partial(
                rows,
                missing,
                note="عرض أحدث شهر — بقية الشهور تُجهَّز في الخلفية",
            )
        return _return_partial(
            [],
            missing,
            note="جاري تجهيز مبيعات المجموعات في الخلفية — أعد التحميل بعد قليل",
        )

    cached, cache_key = _groups_cache_lookup(
        system,
        date_from,
        date_to,
        brn,
        gcode,
        split_by_branch,
        mode,
        allow_stale=False,
    )
    if cached is not None:
        try:
            _tls.groups_stale = False
            _tls.groups_incomplete = False
            _tls.groups_warning = ""
        except Exception:
            pass
        return cached

    # دمج شهور كاملة إن وُجدت كلها في الكاش
    merged_all, missing_months = _merge_available_monthly_group_cache(
        system=system,
        date_from=date_from,
        date_to=date_to,
        brn=brn,
        gcode=gcode,
        split_by_branch=split_by_branch,
        mode=mode,
    )
    if merged_all is not None and not missing_months:
        try:
            _tls.groups_stale = False
            _tls.groups_incomplete = False
            _tls.groups_warning = ""
        except Exception:
            pass
        _sales_cache_set(cache_key, merged_all, date_from=date_from, date_to=date_to)
        return merged_all

    # كاش قديم للفترة (مسارات قصيرة فقط)
    stale_fast, stale_key = _groups_cache_lookup(
        system,
        date_from,
        date_to,
        brn,
        gcode,
        split_by_branch,
        mode,
        allow_stale=True,
    )
    if stale_fast is not None:
        try:
            _tls.groups_stale = True
            _tls.groups_incomplete = False
            _tls.groups_warning = "عرض سريع من الكاش — يُحدَّث في الخلفية"
        except Exception:
            pass
        _schedule_groups_refresh(
            date_from,
            date_to,
            system=system,
            branch_code=brn,
            group_code=gcode,
            by_branch=split_by_branch,
            force_fast=fast,
            cache_key=stale_key or cache_key,
        )
        return stale_fast

    try:
        if conf.get("source") == "pos":
            rows = _fetch_pos_group_totals(
                date_from,
                date_to,
                brn,
                gcode,
                by_branch=split_by_branch,
                skip_returns=fast,
            )
        else:
            rows = _fetch_bill_group_totals(
                date_from,
                date_to,
                conf,
                brn,
                gcode,
                by_branch=split_by_branch,
                skip_returns=fast,
            )
    except Exception as exc:  # noqa: BLE001
        stale, _ = _groups_cache_lookup(
            system,
            date_from,
            date_to,
            brn,
            gcode,
            split_by_branch,
            mode,
            allow_stale=True,
        )
        if stale is not None:
            logger.warning("groups serving stale cache after error: %s", exc)
            try:
                _tls.groups_stale = True
                _tls.groups_warning = (
                    "عرض من ذاكرة مؤقتة سابقة — أوراكل متعثّر حالياً"
                )
            except Exception:
                pass
            return stale
        logger.warning("groups cold fetch failed (soft): %s", exc)
        try:
            _tls.groups_stale = True
            if _is_connect_timeout(exc) or _is_disconnect_error(exc):
                _tls.groups_warning = (
                    "انتهت مهلة جلب مبيعات المجموعات من أوراكل. أعد المحاولة بعد لحظات."
                )
            else:
                _tls.groups_warning = (
                    "تعذّر جلب مبيعات المجموعات من أوراكل. أعد المحاولة بعد لحظات."
                )
        except Exception:
            pass
        _schedule_groups_refresh(
            date_from,
            date_to,
            system=system,
            branch_code=brn,
            group_code=gcode,
            by_branch=split_by_branch,
            force_fast=fast,
            cache_key=cache_key,
        )
        return []
    try:
        _tls.groups_stale = False
        _tls.groups_incomplete = False
        _tls.groups_warning = ""
    except Exception:
        pass
    # فترة قصيرة: خزّن JSON أيضاً لتسهيل التجميع لاحقاً
    try:
        _save_groups_month_json(
            system=system,
            date_from=date_from,
            date_to=date_to,
            brn=brn,
            gcode=gcode,
            split_by_branch=split_by_branch,
            mode=mode,
            rows=rows,
        )
    except Exception:
        _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows


def _schedule_groups_refresh(
    date_from,
    date_to,
    *,
    system: str,
    branch_code: str,
    group_code: str,
    by_branch: bool,
    force_fast: bool,
    cache_key: str,
) -> None:
    """تحديث كاش المجموعات في خيط خلفي بعد عرض سريع من الكاش القديم."""
    # فترات طويلة: تدفئة شهرية بدل مسح السنة دفعة واحدة
    if sales_long_range(date_from, date_to) or len(_month_spans(date_from, date_to)) >= 2:
        _schedule_groups_monthly_warm(
            date_from,
            date_to,
            system=system,
            branch_code=branch_code,
            group_code=group_code,
            by_branch=by_branch,
            force_fast=force_fast,
            cache_key=cache_key,
        )
        return

    lock_key = f"{cache_key}:refreshing"
    try:
        if cache.get(lock_key):
            return
        cache.set(lock_key, 1, 300)
    except Exception:
        return

    def _job():
        try:
            with oracle_session():
                if _system_conf(system).get("source") == "pos":
                    rows = _fetch_pos_group_totals(
                        date_from,
                        date_to,
                        branch_code,
                        group_code,
                        by_branch=by_branch,
                        skip_returns=force_fast,
                    )
                else:
                    rows = _fetch_bill_group_totals(
                        date_from,
                        date_to,
                        _system_conf(system),
                        branch_code,
                        group_code,
                        by_branch=by_branch,
                        skip_returns=force_fast,
                    )
            _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
        except Exception as exc:  # noqa: BLE001
            logger.warning("background groups refresh failed: %s", exc)
        finally:
            try:
                cache.delete(lock_key)
            except Exception:
                pass

    try:
        threading.Thread(target=_job, name="groups-refresh", daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not start groups refresh: %s", exc)
        try:
            cache.delete(lock_key)
        except Exception:
            pass


def _item_in_filter(alias_col: str, codes: list[str], params: dict) -> str:
    """شرط IN لأكواد الأصناف مع ربط آمن."""
    keys = []
    for index, code in enumerate(codes[:80]):
        key = f"ic_{index}"
        params[key] = code
        keys.append(f":{key}")
    if not keys:
        return "AND 1=0"
    return f"AND {alias_col} IN ({', '.join(keys)})"


def _item_name_lookup(codes: list[str]) -> dict[str, str]:
    """أسماء أصناف دفعة واحدة — بعد تجميع المبيعات بدون JOIN كتالوج."""
    cleaned = [str(c or "").strip() for c in codes if str(c or "").strip()]
    if not cleaned:
        return {}
    params: dict[str, Any] = {}
    keys = []
    for index, code in enumerate(cleaned[:80]):
        key = f"nm_{index}"
        params[key] = code
        keys.append(f":{key}")
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(I_CODE) AS ITEM_CODE, I_NAME
        FROM {_schema()}.IAS_ITM_MST
        WHERE I_CODE IN ({', '.join(keys)})
        """,
        params,
    )
    out: dict[str, str] = {}
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        name = str(row.get("I_NAME") or "").strip()
        if code and name:
            out[code] = name
    return out


def _assemble_top_item_rows(
    sales_rows,
    returns_by_item,
    limit: int,
    order_by: str = "sales",
) -> list[dict]:
    merged: list[dict] = []
    for row in sales_rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        if not code:
            continue
        ret_qty, ret_net, ret_vat = returns_by_item.get(code, (0.0, 0.0, 0.0))
        qty = float(row.get("QTY_TOTAL") or 0) - ret_qty
        net = float(row.get("NET_TOTAL") or 0) - ret_net
        vat = float(row.get("VAT_TOTAL") or 0) - ret_vat
        sales_total = round(net + vat, 2)
        if sales_total <= 0 and qty <= 0:
            continue
        merged.append(
            {
                "item_code": code,
                "item_name": str(row.get("ITEM_NAME") or "").strip() or code,
                "invoice_count": int(row.get("INVOICE_COUNT") or 0),
                "qty_total": round(qty, 2),
                "net_total": round(net, 2),
                "vat_total": round(vat, 2),
                "sales_total": sales_total,
            }
        )
    by_qty = str(order_by or "sales").strip().lower() == "qty"
    if by_qty:
        merged.sort(key=lambda r: (-r["qty_total"], -r["sales_total"], r["item_code"]))
    else:
        merged.sort(key=lambda r: (-r["sales_total"], -r["qty_total"], r["item_code"]))
    top = merged[: max(1, int(limit or 8))]
    if by_qty:
        peak = top[0]["qty_total"] if top else 0.0
        for row in top:
            share = (row["qty_total"] / peak * 100.0) if peak else 0.0
            row["share_pct"] = round(share, 1)
    else:
        peak = top[0]["sales_total"] if top else 0.0
        for row in top:
            share = (row["sales_total"] / peak * 100.0) if peak else 0.0
            row["share_pct"] = round(share, 1)
    return top


def _item_order_sql(order_by: str = "sales") -> str:
    if str(order_by or "sales").strip().lower() == "qty":
        return "SUM(NVL(d.I_QTY, 0)) DESC"
    return (
        "SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0) + NVL(d.VAT_AMT, 0)) DESC"
    )


def _pos_branches_in_range(date_from, date_to) -> list[str]:
    """فروع لها مبيعات POS في الفترة — استعلام خفيف قبل تقسيم الأصناف."""
    pos = _pos_owner()
    hung_m = _hung_ok("m")
    rows = _fetch_all(
        f"""
        SELECT DISTINCT TO_CHAR(m.BRN_NO) AS BRANCH_CODE
        FROM {pos}.IAS_POS_BILL_MST m
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND {hung_m}
          AND m.BRN_NO IS NOT NULL
        """,
        _date_params(date_from, date_to),
    )
    out: list[str] = []
    for row in rows:
        code = str(row.get("BRANCH_CODE") or "").strip()
        if code:
            out.append(code)
    return out


def _fetch_pos_item_sales_agg(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
    limit: int | None = None,
    order_by: str = "sales",
    recent_bills: int | None = None,
    max_bills: int | None = None,
    sample_mod: int = 1,
) -> list[dict]:
    """تجميع مبيعات أصناف POS — Top-N أو عيّنة فواتير لتسريع اللوحة."""
    pos = _pos_owner()
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    group_filter = ""
    if branch_code:
        params["brn"] = branch_code
        branch_filter = "AND m.BRN_NO = :brn"
    if group_code:
        params["gcode"] = _bind_gcode(group_code)
        group_filter = "AND i.G_CODE = :gcode"
    hung_m = _hung_ok("m")
    need_item_join = bool(group_code)
    if need_item_join:
        item_join = f"LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE"
        name_expr = "MAX(NVL(i.I_NAME, TO_CHAR(d.I_CODE)))"
    else:
        item_join = ""
        name_expr = "TO_CHAR(d.I_CODE)"

    if recent_bills and int(recent_bills) > 0:
        params["bill_cap"] = int(recent_bills)
        mst_from = f"""
              SELECT BILL_NO, BRN_NO, BILL_SRL FROM (
                  SELECT m.BILL_NO, m.BRN_NO, m.BILL_SRL
                  FROM {pos}.IAS_POS_BILL_MST m
                  WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                    AND {hung_m}
                    {branch_filter}
                  ORDER BY m.BILL_DATE DESC, m.BILL_NO DESC
              ) WHERE ROWNUM <= :bill_cap
        """
    elif max_bills and int(max_bills) > 0:
        # عيّنة موزّعة من رأس الفاتورة فقط (خفيف) ثم تجميع بنودها —
        # أسرع بكثير من GROUP BY على كل IAS_POS_BILL_DTL للفترة.
        params["bill_cap"] = int(max_bills)
        mod = max(1, int(sample_mod or 1))
        hash_filter = ""
        if mod > 1:
            params["sample_mod"] = mod
            hash_filter = (
                "AND MOD(ORA_HASH(TO_CHAR(m.BRN_NO) || ':' || TO_CHAR(m.BILL_NO)), "
                ":sample_mod) = 0"
            )
        mst_from = f"""
              SELECT BILL_NO, BRN_NO, BILL_SRL FROM (
                  SELECT m.BILL_NO, m.BRN_NO, m.BILL_SRL
                  FROM {pos}.IAS_POS_BILL_MST m
                  WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                    AND {hung_m}
                    {branch_filter}
                    {hash_filter}
              ) WHERE ROWNUM <= :bill_cap
        """
    else:
        mst_from = f"""
              SELECT m.BILL_NO, m.BRN_NO, m.BILL_SRL
              FROM {pos}.IAS_POS_BILL_MST m
              WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                AND {hung_m}
                {branch_filter}
        """

    core = f"""
          SELECT
              TO_CHAR(d.I_CODE) AS ITEM_CODE,
              {name_expr} AS ITEM_NAME,
              COUNT(*) AS INVOICE_COUNT,
              ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
              ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
              ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
          FROM (
              {mst_from}
          ) m
          JOIN {pos}.IAS_POS_BILL_DTL d
            ON d.BILL_NO = m.BILL_NO
           AND d.BRN_NO = m.BRN_NO
           AND NVL(d.BILL_SRL, 0) = NVL(m.BILL_SRL, 0)
          {item_join}
          WHERE d.I_CODE IS NOT NULL
            {group_filter}
          GROUP BY d.I_CODE
    """
    if limit is not None:
        order_sql = _item_order_sql(order_by)
        sql = f"""
        SELECT * FROM (
          {core}
          ORDER BY {order_sql}
        ) WHERE ROWNUM <= :lim
        """
        bind = {**params, "lim": max(int(limit), 1)}
    else:
        sql = core
        bind = params
    return _fetch_all(sql, bind)


def _merge_pos_item_sales_rows(parts: list[list[dict]]) -> list[dict]:
    """دمج تجميعات أصناف من عدة فروع."""
    acc: dict[str, dict] = {}
    for rows in parts:
        for row in rows or []:
            code = str(row.get("ITEM_CODE") or "").strip()
            if not code:
                continue
            cur = acc.get(code)
            if cur is None:
                acc[code] = {
                    "ITEM_CODE": code,
                    "ITEM_NAME": str(row.get("ITEM_NAME") or "").strip() or code,
                    "INVOICE_COUNT": int(row.get("INVOICE_COUNT") or 0),
                    "QTY_TOTAL": float(row.get("QTY_TOTAL") or 0),
                    "NET_TOTAL": float(row.get("NET_TOTAL") or 0),
                    "VAT_TOTAL": float(row.get("VAT_TOTAL") or 0),
                }
                continue
            cur["INVOICE_COUNT"] += int(row.get("INVOICE_COUNT") or 0)
            cur["QTY_TOTAL"] += float(row.get("QTY_TOTAL") or 0)
            cur["NET_TOTAL"] += float(row.get("NET_TOTAL") or 0)
            cur["VAT_TOTAL"] += float(row.get("VAT_TOTAL") or 0)
            name = str(row.get("ITEM_NAME") or "").strip()
            if name and name != code:
                cur["ITEM_NAME"] = name
    return list(acc.values())


def _fetch_pos_top_items(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
    limit: int = 8,
    skip_returns: bool = False,
    order_by: str = "sales",
    quick: bool = False,
    fast_sample: bool = False,
) -> list[dict]:
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    fetch_lim = max(int(limit or 8), 8)
    if not skip_returns:
        fetch_lim = max(fetch_lim * 2, 16)

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    span = _date_span_days(date_from, date_to)

    # معاينة سريعة: آخر أيام الفترة + سقف فواتير صغير
    # (ORDER BY على الشهر كامل كان ~4ث؛ نافذة 3–5 أيام + 3000 ≈ 0.2ث)
    if quick:
        d_to = _as_date(date_to)
        d_from = _as_date(date_from)
        quick_from = max(d_from, d_to - timedelta(days=4))
        sales_rows = _fetch_pos_item_sales_agg(
            quick_from,
            d_to,
            branch_code=brn,
            group_code=gcode,
            limit=fetch_lim,
            order_by=order_by,
            recent_bills=3000,
        )
    elif (not brn) and span > 10:
        # رقم دقيق: تجميع كامل لكل فرع ثم دمج (بدون عيّنة وبدون قصّ Top مبكر)
        try:
            branches = _pos_branches_in_range(date_from, date_to)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POS branches-in-range failed, single scan: %s", exc)
            branches = []
        if len(branches) >= 2:

            def _one(b: str) -> list[dict]:
                with oracle_session():
                    return _fetch_pos_item_sales_agg(
                        date_from,
                        date_to,
                        branch_code=b,
                        group_code=gcode,
                        order_by=order_by,
                    )

            parts = _run_parallel(
                [lambda b=code: _one(b) for code in branches],
                max_workers=min(2, len(branches)),
                timeout_sec=240.0,
            )
            sales_rows = _merge_pos_item_sales_rows(parts)
            by_qty = str(order_by or "sales").strip().lower() == "qty"
            if by_qty:
                sales_rows.sort(
                    key=lambda r: (
                        -float(r.get("QTY_TOTAL") or 0),
                        -float(r.get("NET_TOTAL") or 0),
                    )
                )
            else:
                sales_rows.sort(
                    key=lambda r: (
                        -(
                            float(r.get("NET_TOTAL") or 0)
                            + float(r.get("VAT_TOTAL") or 0)
                        ),
                        -float(r.get("QTY_TOTAL") or 0),
                    )
                )
            sales_rows = sales_rows[:fetch_lim]
        else:
            sales_rows = _fetch_pos_item_sales_agg(
                date_from,
                date_to,
                branch_code=brn,
                group_code=gcode,
                limit=fetch_lim,
                order_by=order_by,
            )
    else:
        # تجميع كامل للفترة (أو فرع محدد) ثم Top-N
        sales_rows = _fetch_pos_item_sales_agg(
            date_from,
            date_to,
            branch_code=brn,
            group_code=gcode,
            limit=fetch_lim,
            order_by=order_by,
        )

    if not gcode and sales_rows:
        codes = [
            str(r.get("ITEM_CODE") or "").strip()
            for r in sales_rows
            if str(r.get("ITEM_CODE") or "").strip()
        ]
        names = _item_name_lookup(codes)
        for row in sales_rows:
            code = str(row.get("ITEM_CODE") or "").strip()
            if code and names.get(code):
                row["ITEM_NAME"] = names[code]

    returns_by_item: dict[str, tuple[float, float, float]] = {}
    item_codes = [
        str(row.get("ITEM_CODE") or "").strip()
        for row in sales_rows
        if str(row.get("ITEM_CODE") or "").strip()
    ]
    if not item_codes or skip_returns:
        return _assemble_top_item_rows(
            sales_rows, returns_by_item, limit, order_by=order_by
        )
    try:
        ret_params = dict(params)
        if brn:
            ret_params["brn"] = brn
        branch_filter = "AND m.BRN_NO = :brn" if brn else ""
        group_filter = ""
        if gcode:
            ret_params["gcode"] = _bind_gcode(gcode)
            group_filter = "AND i.G_CODE = :gcode"
        item_filter = _item_in_filter("d.I_CODE", item_codes, ret_params)
        pos = _pos_owner()
        for row in _fetch_all(
            f"""
            SELECT
                TO_CHAR(d.I_CODE) AS ITEM_CODE,
                ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS RET_QTY,
                ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {pos}.IAS_POS_RT_BILL_DTL d
            JOIN {pos}.IAS_POS_RT_BILL_MST m
              ON m.RT_BILL_NO = d.RT_BILL_NO
             AND m.BRN_NO = d.BRN_NO
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
              AND NVL(m.HUNG, 0) = 0
              AND d.I_CODE IS NOT NULL
              {branch_filter}
              {group_filter}
              {item_filter}
            GROUP BY d.I_CODE
            """,
            ret_params,
        ):
            code = str(row.get("ITEM_CODE") or "").strip()
            returns_by_item[code] = (
                float(row.get("RET_QTY") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("POS top items returns failed: %s", exc)
        raise
    return _assemble_top_item_rows(
        sales_rows, returns_by_item, limit, order_by=order_by
    )


def _fetch_bill_top_items(
    date_from,
    date_to,
    conf: dict,
    branch_code: str = "",
    group_code: str = "",
    limit: int = 8,
    skip_returns: bool = False,
    order_by: str = "sales",
) -> list[dict]:
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    branch_filter = ""
    group_filter = ""
    if branch_code:
        params["brn"] = branch_code
        branch_filter = "AND b.BRN_NO = :brn"
    if group_code:
        params["gcode"] = _bind_gcode(group_code)
        group_filter = "AND i.G_CODE = :gcode"
    order_sql = _item_order_sql(order_by)

    sales_rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT
              TO_CHAR(d.I_CODE) AS ITEM_CODE,
              MAX(NVL(i.I_NAME, TO_CHAR(d.I_CODE))) AS ITEM_NAME,
              COUNT(*) AS INVOICE_COUNT,
              ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
              ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
              ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
          FROM {schema}.IAS_BILL_DTL d
          JOIN {schema}.IAS_BILL_MST b
            ON b.BILL_SER = d.BILL_SER
           AND b.BRN_NO = d.BRN_NO
          LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
          WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
            AND {_bill_mst_ok("b")}
            AND d.I_CODE IS NOT NULL
            {doc_filter}
            {cash_filter}
            {branch_filter}
            {group_filter}
          GROUP BY d.I_CODE
          ORDER BY {order_sql}
        ) WHERE ROWNUM <= :lim
        """,
        {
            **params,
            "lim": max(int(limit or 8), 8)
            if skip_returns
            else max(int(limit or 8) * 2, 16),
        },
    )

    returns_by_item: dict[str, tuple[float, float, float]] = {}
    item_codes = [
        str(row.get("ITEM_CODE") or "").strip()
        for row in sales_rows
        if str(row.get("ITEM_CODE") or "").strip()
    ]
    if not item_codes or skip_returns:
        return _assemble_top_item_rows(
            sales_rows, returns_by_item, limit, order_by=order_by
        )
    try:
        ret_params: dict = _date_params(date_from, date_to)
        ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        ret_branch = ""
        ret_group = ""
        if branch_code:
            ret_params["brn"] = _bind_brn(branch_code)
            ret_branch = "AND r.BRN_NO = :brn"
        if group_code:
            ret_params["gcode"] = _bind_gcode(group_code)
            ret_group = "AND i.G_CODE = :gcode"
        item_filter = _item_in_filter("d.I_CODE", item_codes, ret_params)
        for row in _fetch_all(
            f"""
            SELECT
                TO_CHAR(d.I_CODE) AS ITEM_CODE,
                ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS RET_QTY,
                ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {schema}.IAS_RT_BILL_DTL d
            JOIN {schema}.IAS_RT_BILL_MST r
              ON r.RT_BILL_SER = d.RT_BILL_SER
             AND r.BRN_NO = d.BRN_NO
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND {_rt_bill_mst_ok("r")}
              AND d.I_CODE IS NOT NULL
              {ret_doc}
              {ret_cash}
              {ret_branch}
              {ret_group}
              {item_filter}
            GROUP BY TO_CHAR(d.I_CODE)
            """,
            ret_params,
        ):
            code = str(row.get("ITEM_CODE") or "").strip()
            returns_by_item[code] = (
                float(row.get("RET_QTY") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bill top items returns failed: %s", exc)
        raise
    return _assemble_top_item_rows(
        sales_rows, returns_by_item, limit, order_by=order_by
    )


def fetch_top_sales_items(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    limit: int = 8,
    order_by: str = "sales",
    force_fast: bool | None = None,
    quick: bool = False,
    fast_sample: bool = False,
) -> list[dict]:
    """أكثر الأصناف مبيعاً خلال الفترة — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    order = "qty" if str(order_by or "sales").strip().lower() == "qty" else "sales"
    # الافتراضي: إجمالي قبل المرتجع (مثل المجموعات) — أسرع؛ fast=0 للصافي
    fast = True if force_fast is None else bool(force_fast)
    mode = "gross" if fast else "net"
    qtag = "q1" if quick else "q0"
    stag = "s1" if fast_sample else "s0"
    cache_key = (
        f"sales:items:v15:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{int(limit or 8)}:"
        f"{mode}:{order}:{qtag}:{stag}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached
    conf = _system_conf(system)
    if conf.get("source") == "pos":
        rows = _fetch_pos_top_items(
            date_from,
            date_to,
            brn,
            gcode,
            limit,
            skip_returns=fast,
            order_by=order,
            quick=bool(quick),
            fast_sample=bool(fast_sample),
        )
    else:
        rows = _fetch_bill_top_items(
            date_from,
            date_to,
            conf,
            brn,
            gcode,
            limit,
            skip_returns=fast,
            order_by=order,
        )
    _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows


def fetch_sales_item_highlights(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
    system: str = "",
) -> dict:
    """أعلى صنف مبيع/سحب/إرجاع — بنفس نطاق تبويب الداشبورد (نظام واحد)."""
    empty = {
        "top_amount_name": "—",
        "top_amount_code": "",
        "top_amount_value": "0.00",
        "top_qty_name": "—",
        "top_qty_code": "",
        "top_qty_value": "0.00",
        "top_return_name": "—",
        "top_return_code": "",
        "top_return_value": "0.00",
    }
    if not oracle_enabled():
        return empty

    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    sys = str(system or "pos").strip().lower()
    if sys not in ("pos", "wholesale"):
        sys = "pos"

    top_amt = None
    top_qty = None
    top_ret = None
    try:
        def _amt():
            with oracle_session():
                rows = fetch_top_sales_items(
                    date_from,
                    date_to,
                    system=sys,
                    branch_code=brn,
                    group_code=gcode,
                    limit=1,
                    order_by="sales",
                )
                return rows[0] if rows else None

        def _qty():
            with oracle_session():
                rows = fetch_top_sales_items(
                    date_from,
                    date_to,
                    system=sys,
                    branch_code=brn,
                    group_code=gcode,
                    limit=1,
                    order_by="qty",
                )
                return rows[0] if rows else None

        def _ret():
            with oracle_session():
                rows = fetch_top_returned_items(
                    date_from,
                    date_to,
                    system=sys,
                    branch_code=brn,
                    group_code=gcode,
                    limit=1,
                )
                return rows[0] if rows else None

        top_amt, top_qty, top_ret = _run_parallel([_amt, _qty, _ret], max_workers=3)
    except Exception as exc:  # noqa: BLE001
        logger.warning("item highlights (%s) failed: %s", sys, exc)
        return empty

    if not top_amt and not top_qty and not top_ret:
        return empty
    return {
        "top_amount_name": (top_amt or {}).get("item_name") or "—",
        "top_amount_code": (top_amt or {}).get("item_code") or "",
        "top_amount_value": f"{float((top_amt or {}).get('sales_total') or 0):,.2f}",
        "top_qty_name": (top_qty or {}).get("item_name") or "—",
        "top_qty_code": (top_qty or {}).get("item_code") or "",
        "top_qty_value": f"{float((top_qty or {}).get('qty_total') or 0):,.2f}",
        "top_return_name": (top_ret or {}).get("item_name") or "—",
        "top_return_code": (top_ret or {}).get("item_code") or "",
        "top_return_value": (
            f"{float((top_ret or {}).get('return_total') or (top_ret or {}).get('sales_total') or 0):,.2f}"
        ),
    }


def _assemble_top_return_rows(rows, limit: int) -> list[dict]:
    merged: list[dict] = []
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        if not code:
            continue
        qty = float(row.get("QTY_TOTAL") or 0)
        net = float(row.get("NET_TOTAL") or 0)
        vat = float(row.get("VAT_TOTAL") or 0)
        total = round(net + vat, 2)
        merged.append(
            {
                "item_code": code,
                "item_name": str(row.get("ITEM_NAME") or "").strip() or code,
                "return_count": int(row.get("RETURN_COUNT") or 0),
                "qty_total": round(qty, 2),
                "net_total": round(net, 2),
                "vat_total": round(vat, 2),
                "return_total": total,
                # توافق مع واجهة الرسم (نفس حقول المبيعات)
                "sales_total": total,
                "invoice_count": int(row.get("RETURN_COUNT") or 0),
            }
        )
    merged.sort(key=lambda r: (-r["return_total"], -r["qty_total"], r["item_code"]))
    top = merged[: max(1, int(limit or 8))]
    peak = top[0]["return_total"] if top else 0.0
    for row in top:
        share = (row["return_total"] / peak * 100.0) if peak else 0.0
        row["share_pct"] = round(share, 1)
    return top


def fetch_top_returned_items(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    limit: int = 20,
    quick: bool = False,
) -> list[dict]:
    """أكثر الأصناف إرجاعاً خلال الفترة (قيمة المرتجع) — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    lim = max(1, min(int(limit or 20), 50))
    qtag = "q1" if quick else "q0"
    cache_key = (
        f"sales:ret_items:v2:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{lim}:{qtag}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    conf = _system_conf(system)
    params: dict = _date_params(date_from, date_to)
    schema = _schema()
    if conf.get("source") == "pos":
        pos = _pos_owner()
        branch_filter = ""
        group_filter = ""
        if brn:
            params["brn"] = _bind_brn(brn)
            branch_filter = "AND m.BRN_NO = :brn"
        if gcode:
            params["gcode"] = _bind_gcode(gcode)
            group_filter = "AND i.G_CODE = :gcode"
        hung_m = _hung_ok("m")
        need_join = bool(gcode)
        item_join = (
            f"LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE"
            if need_join
            else ""
        )
        name_expr = (
            "MAX(NVL(i.I_NAME, TO_CHAR(d.I_CODE)))"
            if need_join
            else "TO_CHAR(d.I_CODE)"
        )
        if quick:
            params["bill_cap"] = 5000
            mst_from = f"""
                  SELECT RT_BILL_NO, BRN_NO FROM (
                      SELECT m.RT_BILL_NO, m.BRN_NO
                      FROM {pos}.IAS_POS_RT_BILL_MST m
                      WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                        AND {hung_m}
                        {branch_filter}
                      ORDER BY m.RT_BILL_DATE DESC, m.RT_BILL_NO DESC
                  ) WHERE ROWNUM <= :bill_cap
            """
        else:
            mst_from = f"""
                  SELECT m.RT_BILL_NO, m.BRN_NO
                  FROM {pos}.IAS_POS_RT_BILL_MST m
                  WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
                    AND {hung_m}
                    {branch_filter}
            """
        rows = _fetch_all(
            f"""
            SELECT * FROM (
              SELECT
                  TO_CHAR(d.I_CODE) AS ITEM_CODE,
                  {name_expr} AS ITEM_NAME,
                  COUNT(*) AS RETURN_COUNT,
                  ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                  ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
                  ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
              FROM (
                  {mst_from}
              ) m
              JOIN {pos}.IAS_POS_RT_BILL_DTL d
                ON d.RT_BILL_NO = m.RT_BILL_NO
               AND d.BRN_NO = m.BRN_NO
              {item_join}
              WHERE d.I_CODE IS NOT NULL
                {group_filter}
              GROUP BY d.I_CODE
              ORDER BY
                  SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0) + NVL(d.VAT_AMT, 0)) DESC
            ) WHERE ROWNUM <= :lim
            """,
            {**params, "lim": lim},
        )
        if not need_join and rows:
            codes = [
                str(r.get("ITEM_CODE") or "").strip()
                for r in rows
                if str(r.get("ITEM_CODE") or "").strip()
            ]
            names = _item_name_lookup(codes)
            for row in rows:
                code = str(row.get("ITEM_CODE") or "").strip()
                if code and names.get(code):
                    row["ITEM_NAME"] = names[code]
    else:
        doc_filter = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
        cash_filter = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        branch_filter = ""
        group_filter = ""
        if brn:
            params["brn"] = _bind_brn(brn)
            branch_filter = "AND r.BRN_NO = :brn"
        if gcode:
            params["gcode"] = _bind_gcode(gcode)
            group_filter = "AND i.G_CODE = :gcode"
        rows = _fetch_all(
            f"""
            SELECT * FROM (
              SELECT
                  TO_CHAR(d.I_CODE) AS ITEM_CODE,
                  MAX(NVL(i.I_NAME, TO_CHAR(d.I_CODE))) AS ITEM_NAME,
                  COUNT(*) AS RETURN_COUNT,
                  ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                  ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
                  ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
              FROM {schema}.IAS_RT_BILL_DTL d
              JOIN {schema}.IAS_RT_BILL_MST r
                ON r.RT_BILL_SER = d.RT_BILL_SER
               AND r.BRN_NO = d.BRN_NO
              LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
              WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                AND {_rt_bill_mst_ok("r")}
                AND d.I_CODE IS NOT NULL
                {doc_filter}
                {cash_filter}
                {branch_filter}
                {group_filter}
              GROUP BY d.I_CODE
              ORDER BY
                  SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0) + NVL(d.VAT_AMT, 0)) DESC
            ) WHERE ROWNUM <= :lim
            """,
            {**params, "lim": lim},
        )

    out = _assemble_top_return_rows(rows, lim)
    _sales_cache_set(cache_key, out, date_from=date_from, date_to=date_to)
    return out



def _user_names() -> dict[str, str]:
    """أسماء المستخدمين من USER_R مفهرسة برقم المستخدم."""
    hit, cached = _django_lookup_get("user_names")
    if hit:
        return cached
    try:
        rows = _fetch_all(
            f"SELECT U_ID, U_A_NAME, U_E_NAME FROM {_schema()}.USER_R"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("User names unavailable: %s", exc)
        return _django_lookup_set("user_names", {})
    names: dict[str, str] = {}
    for row in rows:
        code = str(row.get("U_ID") or "").strip()
        if not code:
            continue
        label = str(row.get("U_A_NAME") or row.get("U_E_NAME") or "").strip() or code
        names[code] = label
    return _django_lookup_set("user_names", names)


def _assemble_user_rows(sales_rows, returns_by_user, limit: int) -> list[dict]:
    names = _user_names()
    out: list[dict] = []
    for row in sales_rows:
        code = str(row.get("USER_CODE") or "").strip()
        ret_count, ret_net, ret_vat = returns_by_user.get(code, (0, 0.0, 0.0))
        net = float(row.get("NET_TOTAL") or 0) - ret_net
        vat = float(row.get("VAT_TOTAL") or 0) - ret_vat
        total = round(net + vat, 2)
        out.append(
            {
                "user_code": code,
                "user_name": names.get(code) or code,
                "invoice_count": int(row.get("INVOICE_COUNT") or 0),
                "return_count": ret_count,
                "net_total": round(net, 2),
                "vat_total": round(vat, 2),
                "sales_total": total,
            }
        )
    out.sort(key=lambda r: (-r["sales_total"], r["user_code"]))
    top = out[: max(1, min(int(limit or 10), 50))]
    peak = top[0]["sales_total"] if top else 0.0
    for row in top:
        pct = (row["sales_total"] / peak * 100.0) if peak > 0 else 0.0
        row["share_pct"] = round(pct, 1)
    return top


def fetch_active_sales_user_count(
    date_from,
    date_to,
    system: str = "pos",
) -> int:
    """عدد المستخدمين (البائعين) الذين لديهم فواتير في الفترة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    cache_key = (
        f"sales:seller_count:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return int(cached)
    conf = _system_conf(system)
    params = _date_params(date_from, date_to)
    if conf.get("source") == "pos":
        pos = _pos_owner()
        rows = _fetch_all(
            f"""
            SELECT COUNT(DISTINCT TO_CHAR(p.AD_U_ID)) AS CNT
            FROM {pos}.IAS_POS_BILL_MST p
            WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
              AND NVL(p.HUNG, 0) = 0
              AND p.AD_U_ID IS NOT NULL
            """,
            params,
        )
    else:
        schema = _schema()
        doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
        cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        rows = _fetch_all(
            f"""
            SELECT COUNT(DISTINCT TO_CHAR(b.AD_U_ID)) AS CNT
            FROM {schema}.IAS_BILL_MST b
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND {_bill_mst_ok("b")}
              AND b.AD_U_ID IS NOT NULL
              {doc_filter}
              {cash_filter}
            """,
            params,
        )
    count = int((rows[0].get("CNT") if rows else 0) or 0)
    _sales_cache_set(cache_key, count, date_from=date_from, date_to=date_to)
    return count


def fetch_active_sales_device_count(
    date_from,
    date_to,
    system: str = "pos",
) -> int:
    """عدد الأجهزة/الآلات النشطة التي أصدرت فواتير في الفترة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    cache_key = (
        f"sales:device_count:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return int(cached)
    conf = _system_conf(system)
    params = _date_params(date_from, date_to)
    if conf.get("source") == "pos":
        pos = _pos_owner()
        rows = _fetch_all(
            f"""
            SELECT COUNT(DISTINCT NVL(TO_CHAR(p.MACHINE_NO), TO_CHAR(p.CASH_NO))) AS CNT
            FROM {pos}.IAS_POS_BILL_MST p
            WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
              AND NVL(p.HUNG, 0) = 0
              AND NVL(TO_CHAR(p.MACHINE_NO), TO_CHAR(p.CASH_NO)) IS NOT NULL
            """,
            params,
        )
    else:
        schema = _schema()
        doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
        cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        rows = _fetch_all(
            f"""
            SELECT COUNT(DISTINCT TO_CHAR(b.CASH_NO)) AS CNT
            FROM {schema}.IAS_BILL_MST b
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND {_bill_mst_ok("b")}
              AND b.CASH_NO IS NOT NULL
              {doc_filter}
              {cash_filter}
            """,
            params,
        )
    count = int((rows[0].get("CNT") if rows else 0) or 0)
    _sales_cache_set(cache_key, count, date_from=date_from, date_to=date_to)
    return count


def fetch_top_sales_users(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    limit: int = 10,
) -> list[dict]:
    """أكثر المستخدمين مبيعاً — رأس الفاتورة فقط (Top-N سريع، بدون مرتجعات)."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    brn = str(branch_code or "").strip()
    lim = max(1, min(int(limit or 10), 50))
    cache_key = (
        f"sales:users:v4:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{lim}:gross"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    conf = _system_conf(system)
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    if brn:
        params["brn"] = _bind_brn(brn)
        branch_filter = "AND p.BRN_NO = :brn"
    params["lim"] = lim

    if conf.get("source") == "pos":
        pos = _pos_owner()
        sales_rows = _fetch_all(
            f"""
            SELECT * FROM (
              SELECT
                  TO_CHAR(p.AD_U_ID) AS USER_CODE,
                  COUNT(*) AS INVOICE_COUNT,
                  ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
                  ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL
              FROM {pos}.IAS_POS_BILL_MST p
              WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
                AND {_pos_mst_ok("p")}
                AND p.AD_U_ID IS NOT NULL
                {branch_filter}
              GROUP BY p.AD_U_ID
              ORDER BY SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)) DESC
            ) WHERE ROWNUM <= :lim
            """,
            params,
        )
        rows = _assemble_user_rows(sales_rows, {}, lim)
        _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
        return rows

    schema = _schema()
    bill_branch = ""
    if brn:
        params["brn"] = _bind_brn(brn)
        bill_branch = "AND b.BRN_NO = :brn"
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    sales_rows = _fetch_all(
        f"""
        SELECT * FROM (
          SELECT
              TO_CHAR(b.AD_U_ID) AS USER_CODE,
              COUNT(*) AS INVOICE_COUNT,
              ROUND(SUM(NVL(b.BILL_AMT, 0)), 2) AS NET_TOTAL,
              ROUND(SUM(NVL(b.VAT_AMT, 0)), 2) AS VAT_TOTAL
          FROM {schema}.IAS_BILL_MST b
          WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
            {doc_filter}
            {cash_filter}
            {bill_branch}
            AND {_bill_mst_ok("b")}
            AND b.AD_U_ID IS NOT NULL
          GROUP BY b.AD_U_ID
          ORDER BY SUM(NVL(b.BILL_AMT, 0) + NVL(b.VAT_AMT, 0)) DESC
        ) WHERE ROWNUM <= :lim
        """,
        params,
    )
    rows = _assemble_user_rows(sales_rows, {}, lim)
    _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows



def fetch_user_invoice_details(
    date_from,
    date_to,
    user_code,
    system: str = "pos",
) -> list[dict]:
    """فواتير مستخدم مجمّعة حسب الفرع ورقم الآلة/الصندوق والجهاز."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    try:
        user_id = int(str(user_code).strip())
    except (TypeError, ValueError) as exc:
        raise OracleStockError("رقم المستخدم غير صحيح.") from exc

    conf = _system_conf(system)
    params: dict = {**_date_params(date_from, date_to), "user_id": user_id}

    if conf.get("source") == "pos":
        pos = _pos_owner()
        rows = _fetch_all(
            f"""
            SELECT
                TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
                NVL(TO_CHAR(p.MACHINE_NO), NVL(TO_CHAR(p.CASH_NO), '-')) AS MACHINE_NO,
                NVL(NULLIF(TRIM(p.AD_TRMNL_NM), ''), '-') AS TERMINAL_NAME,
                TO_CHAR(p.BILL_NO) AS BILL_NO,
                TO_CHAR(p.BILL_NO) AS BILL_SER,
                p.BILL_DATE,
                ROUND(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0), 2) AS BILL_TOTAL
            FROM {pos}.IAS_POS_BILL_MST p
            WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
              AND p.AD_U_ID = :user_id
              AND NVL(p.HUNG, 0) = 0
            ORDER BY p.BRN_NO, p.MACHINE_NO, p.AD_TRMNL_NM, p.BILL_DATE, p.BILL_NO
            """,
            params,
        )
    else:
        schema = _schema()
        doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
        cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        rows = _fetch_all(
            f"""
            SELECT
                TO_CHAR(b.BRN_NO) AS BRANCH_CODE,
                NVL(TO_CHAR(b.CASH_NO), '-') AS MACHINE_NO,
                NVL(NULLIF(TRIM(b.AD_TRMNL_NM), ''), '-') AS TERMINAL_NAME,
                TO_CHAR(b.BILL_NO) AS BILL_NO,
                TO_CHAR(b.BILL_SER) AS BILL_SER,
                b.BILL_DATE,
                ROUND(NVL(b.BILL_AMT, 0) + NVL(b.VAT_AMT, 0), 2) AS BILL_TOTAL
            FROM {schema}.IAS_BILL_MST b
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND b.AD_U_ID = :user_id
              {doc_filter}
              {cash_filter}
              AND {_bill_mst_ok("b")}
            ORDER BY b.BRN_NO, b.CASH_NO, b.AD_TRMNL_NM, b.BILL_DATE, b.BILL_NO
            """,
            params,
        )

    branch_names = _branch_names()
    groups: dict[tuple[str, str, str], dict] = {}
    seen: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        branch_code = str(row.get("BRANCH_CODE") or "").strip()
        machine_no = str(row.get("MACHINE_NO") or "").strip()
        terminal_name = str(row.get("TERMINAL_NAME") or "-").strip() or "-"
        key = (branch_code, machine_no, terminal_name)
        group = groups.setdefault(
            key,
            {
                "branch_code": branch_code,
                "branch_name": branch_names.get(branch_code) or branch_code,
                "machine_no": machine_no,
                "terminal_name": terminal_name,
                "invoice_count": 0,
                "sales_total": 0.0,
                "invoices": [],
            },
        )
        bill_ser = str(row.get("BILL_SER") or row.get("BILL_NO") or "").strip()
        group_seen = seen.setdefault(key, set())
        if bill_ser in group_seen:
            continue
        group_seen.add(bill_ser)
        bill_date = row.get("BILL_DATE")
        group["invoices"].append(
            {
                "bill_no": str(row.get("BILL_NO") or "").strip(),
                "bill_date": bill_date.strftime("%Y-%m-%d") if bill_date else "",
                "bill_total": round(float(row.get("BILL_TOTAL") or 0), 2),
            }
        )
        group["invoice_count"] += 1
        group["sales_total"] += float(row.get("BILL_TOTAL") or 0)

    out = list(groups.values())
    for group in out:
        group["sales_total"] = round(group["sales_total"], 2)
    out.sort(
        key=lambda group: (
            group["branch_code"],
            group["machine_no"],
            group["terminal_name"],
        )
    )
    return out


def _item_unit_cost_map() -> dict[str, float]:
    """تكلفة وحدة تقريبية لكل صنف من IAS_ITM_WCODE — للكاش وتفادي JOIN ثقيل."""
    hit, cached = _django_lookup_get("item_unit_cost_map:v1")
    if hit and isinstance(cached, dict):
        return cached
    rows = _fetch_all(
        f"""
        SELECT TO_CHAR(w.I_CODE) AS ITEM_CODE,
               ROUND(AVG(NVL(w.I_CWTAVG, w.PRIMARY_COST)), 6) AS UNIT_COST
        FROM {_schema()}.IAS_ITM_WCODE w
        WHERE NVL(w.I_CWTAVG, w.PRIMARY_COST) IS NOT NULL
          AND NVL(w.I_CWTAVG, w.PRIMARY_COST) > 0
          AND w.I_CODE IS NOT NULL
        GROUP BY TO_CHAR(w.I_CODE)
        """,
        {},
    )
    out: dict[str, float] = {}
    for row in rows:
        code = str(row.get("ITEM_CODE") or "").strip()
        if not code:
            continue
        out[code] = float(row.get("UNIT_COST") or 0)
    return _django_lookup_set("item_unit_cost_map:v1", out)


def _item_wh_unit_cost_map() -> dict[str, dict[str, float]]:
    """تكلفة وحدة لكل صنف+مخزن — كاش طويل لتفادي إعادة مسح IAS_ITM_WCODE."""
    hit, cached = _django_lookup_get("item_wh_unit_cost_map:v2")
    if hit and isinstance(cached, dict):
        out: dict[str, dict[str, float]] = {}
        for item, wh_map in cached.items():
            if isinstance(wh_map, dict):
                out[str(item)] = {
                    str(wh): float(cost or 0) for wh, cost in wh_map.items()
                }
        return out
    rows = _fetch_all(
        f"""
        SELECT w.I_CODE AS ITEM_CODE,
               w.W_CODE AS W_CODE,
               ROUND(AVG(NVL(w.I_CWTAVG, w.PRIMARY_COST)), 6) AS UNIT_COST
        FROM {_schema()}.IAS_ITM_WCODE w
        WHERE NVL(w.I_CWTAVG, w.PRIMARY_COST) IS NOT NULL
          AND NVL(w.I_CWTAVG, w.PRIMARY_COST) > 0
          AND w.I_CODE IS NOT NULL
          AND w.W_CODE IS NOT NULL
        GROUP BY w.I_CODE, w.W_CODE
        """,
        {},
    )
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        item = str(row.get("ITEM_CODE") or "").strip()
        wh = str(row.get("W_CODE") or "").strip()
        if not item or not wh:
            continue
        out.setdefault(item, {})[wh] = float(row.get("UNIT_COST") or 0)
    return _django_lookup_set("item_wh_unit_cost_map:v2", out)


def _simple_unit_cost_join_sql(schema: str) -> str:
    """JOIN تكلفة خفيف: متوسط I_CWTAVG/PRIMARY_COST بدون TO_CHAR."""
    return f"""
        LEFT JOIN (
          SELECT
              w.I_CODE AS I_CODE,
              ROUND(AVG(NVL(w.I_CWTAVG, w.PRIMARY_COST)), 6) AS UNIT_COST
          FROM {schema}.IAS_ITM_WCODE w
          WHERE NVL(w.I_CWTAVG, w.PRIMARY_COST) IS NOT NULL
            AND NVL(w.I_CWTAVG, w.PRIMARY_COST) > 0
          GROUP BY w.I_CODE
        ) uc ON uc.I_CODE = d.I_CODE
    """


def _margin_rank_rows(
    rows: list[dict],
    *,
    code_key: str,
    name_key: str,
    names: dict[str, str],
    limit: int | None = None,
) -> list[dict]:
    """يرتب صفوف الهامش: نسبة الربح = (صافي − تكلفة) / تكلفة × 100."""
    out: list[dict] = []
    for row in rows:
        code = str(row.get("CODE") or "").strip() or "(بلا)"
        net = round(float(row.get("NET_TOTAL") or 0), 2)
        if net <= 0:
            continue
        cost_total = round(float(row.get("COST_TOTAL") or 0), 2)
        if cost_total < 0:
            cost_total = 0.0
        qty = round(float(row.get("QTY_TOTAL") or 0), 2)
        profit = round(net - cost_total, 2)
        margin_pct = (
            round((profit / cost_total) * 100.0, 2) if cost_total > 0 else None
        )
        name = names.get(code) or str(row.get("NAME") or "").strip() or code
        out.append(
            {
                code_key: code,
                name_key: name,
                "qty_total": qty,
                "sales_net": net,
                "cost_total": cost_total,
                "profit": profit,
                "margin_pct": margin_pct,
            }
        )
    out.sort(
        key=lambda r: (
            r["margin_pct"] is None,
            -(r["margin_pct"] if r["margin_pct"] is not None else 0),
            -r["profit"],
            r[name_key],
        )
    )
    if limit is None:
        return out
    return out[: max(1, int(limit or 15))]


def _fetch_bill_margin_ranks(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
) -> tuple[list[dict], list[dict]]:
    """هامش الآجل للفروع فقط (لوحة مجموعات الهامش أُلغيت)."""
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    group_filter = ""
    brn = str(branch_code or "").strip()
    if brn:
        params["brn"] = _bind_brn(brn)
        branch_filter = "AND m.BRN_NO = :brn"
    if group_code:
        params["gcode"] = _bind_gcode(group_code)
        group_filter = "AND i.G_CODE = :gcode"

    rows = _fetch_all(
        f"""
        SELECT
            TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
            ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
            ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(d.STK_COST, 0) * NVL(d.I_QTY, 0)), 2) AS COST_TOTAL
        FROM {schema}.IAS_BILL_DTL d
        JOIN {schema}.IAS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO
         AND m.BRN_NO = d.BRN_NO
         AND NVL(m.BILL_SER, 0) = NVL(d.BILL_SER, 0)
        LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND {_bill_mst_ok("m")}
          AND d.I_CODE IS NOT NULL
          {branch_filter}
          {group_filter}
        GROUP BY m.BRN_NO
        """,
        params,
    )
    branch_names = _branch_names()
    branch_rows = [
        {
            "CODE": row.get("BRANCH_CODE"),
            "NAME": branch_names.get(str(row.get("BRANCH_CODE") or "").strip()),
            "QTY_TOTAL": row.get("QTY_TOTAL"),
            "NET_TOTAL": row.get("NET_TOTAL"),
            "COST_TOTAL": row.get("COST_TOTAL"),
        }
        for row in rows
    ]

    # خصم مرتجعات الآجل من صافي/تكلفة الهامش
    try:
        ret_params = dict(params)
        ret_rows = _fetch_all(
            f"""
            SELECT
                TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
                ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
                ROUND(SUM(NVL(d.STK_COST, 0) * NVL(d.I_QTY, 0)), 2) AS COST_TOTAL
            FROM {schema}.IAS_RT_BILL_DTL d
            JOIN {schema}.IAS_RT_BILL_MST r
              ON r.RT_BILL_SER = d.RT_BILL_SER
             AND r.BRN_NO = d.BRN_NO
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND {_rt_bill_mst_ok("r")}
              AND d.I_CODE IS NOT NULL
              {branch_filter.replace("m.BRN_NO", "r.BRN_NO") if branch_filter else ""}
              {group_filter}
            GROUP BY r.BRN_NO
            """,
            ret_params,
        )
        brn_map = {str(r.get("CODE") or "").strip(): r for r in branch_rows}
        for row in ret_rows:
            code = str(row.get("BRANCH_CODE") or "").strip() or "(بلا)"
            bucket = brn_map.get(code)
            if bucket is None:
                bucket = {
                    "CODE": code,
                    "NAME": branch_names.get(code) or code,
                    "QTY_TOTAL": 0.0,
                    "NET_TOTAL": 0.0,
                    "COST_TOTAL": 0.0,
                }
                branch_rows.append(bucket)
                brn_map[code] = bucket
            bucket["QTY_TOTAL"] = float(bucket.get("QTY_TOTAL") or 0) - float(
                row.get("QTY_TOTAL") or 0
            )
            bucket["NET_TOTAL"] = float(bucket.get("NET_TOTAL") or 0) - float(
                row.get("NET_TOTAL") or 0
            )
            bucket["COST_TOTAL"] = float(bucket.get("COST_TOTAL") or 0) - float(
                row.get("COST_TOTAL") or 0
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Bill margin returns failed: %s", exc)
        raise

    return (
        _margin_rank_rows(
            branch_rows, code_key="branch_code", name_key="branch_name", names=branch_names
        ),
        [],
    )


def _merge_margin_branch_rank_rows(parts: list[list[dict]]) -> list[dict]:
    """دمج صفوف هامش فروع من مسارات متوازية ثم إعادة الترتيب."""
    acc: dict[str, dict[str, float | str]] = {}
    for rows in parts:
        for row in rows or []:
            code = str(row.get("branch_code") or "").strip()
            if not code:
                continue
            cur = acc.get(code)
            if cur is None:
                acc[code] = {
                    "branch_code": code,
                    "branch_name": str(row.get("branch_name") or code),
                    "qty_total": float(row.get("qty_total") or 0),
                    "sales_net": float(row.get("sales_net") or 0),
                    "cost_total": float(row.get("cost_total") or 0),
                }
            else:
                cur["qty_total"] = float(cur["qty_total"]) + float(
                    row.get("qty_total") or 0
                )
                cur["sales_net"] = float(cur["sales_net"]) + float(
                    row.get("sales_net") or 0
                )
                cur["cost_total"] = float(cur["cost_total"]) + float(
                    row.get("cost_total") or 0
                )
                if row.get("branch_name"):
                    cur["branch_name"] = str(row["branch_name"])
    out: list[dict] = []
    for cur in acc.values():
        net = round(float(cur["sales_net"]), 2)
        if net <= 0:
            continue
        cost = round(max(float(cur["cost_total"]), 0.0), 2)
        qty = round(float(cur["qty_total"]), 2)
        profit = round(net - cost, 2)
        margin_pct = round((profit / cost) * 100.0, 2) if cost > 0 else None
        out.append(
            {
                "branch_code": cur["branch_code"],
                "branch_name": cur["branch_name"],
                "qty_total": qty,
                "sales_net": net,
                "cost_total": cost,
                "profit": profit,
                "margin_pct": margin_pct,
            }
        )
    out.sort(
        key=lambda r: (
            r["margin_pct"] is None,
            -(r["margin_pct"] if r["margin_pct"] is not None else 0),
            -r["profit"],
            r["branch_name"],
        )
    )
    return out


def _fetch_pos_margin_ranks(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
    skip_returns: bool = False,
    recent_bills: int | None = None,
    _allow_fanout: bool = True,
) -> tuple[list[dict], list[dict]]:
    """هامش POS — تجميع صنف×مخزن في SQL ثم تكلفة من كاش (بدون مسح WCODE في كل طلب)."""
    pos = _pos_owner()
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    if brn:
        params["brn"] = _bind_brn(brn)
        branch_filter = "AND m.BRN_NO = :brn"
    hung_m = _hung_ok("m")
    stock_qty = "NVL(d.P_QTY, NVL(d.I_QTY, 0) * NVL(d.P_SIZE, 1))"
    span = _date_span_days(date_from, date_to)

    # فترات طويلة: فرع لكل عامل — أسرع من مسح واحد لكل الفروع
    if (
        _allow_fanout
        and not recent_bills
        and not brn
        and span > 10
    ):
        try:
            branches = _pos_branches_in_range(date_from, date_to)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POS margin branches-in-range failed: %s", exc)
            branches = []
        if len(branches) >= 2:

            def _one(b: str) -> list[dict]:
                with oracle_session():
                    br_rows, _ = _fetch_pos_margin_ranks(
                        date_from,
                        date_to,
                        branch_code=b,
                        group_code=gcode,
                        skip_returns=skip_returns,
                        recent_bills=None,
                        _allow_fanout=False,
                    )
                    return br_rows

            parts = _run_parallel(
                [lambda b=code: _one(b) for code in branches],
                max_workers=min(2, len(branches)),
            )
            return _merge_margin_branch_rank_rows(parts), []

    if recent_bills and int(recent_bills) > 0:
        params["bill_cap"] = int(recent_bills)
        mst_from = f"""
              SELECT BILL_NO, BRN_NO, BILL_SRL, W_CODE FROM (
                  SELECT m.BILL_NO, m.BRN_NO, m.BILL_SRL, m.W_CODE
                  FROM {pos}.IAS_POS_BILL_MST m
                  WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                    AND {hung_m}
                    {branch_filter}
                  ORDER BY m.BILL_DATE DESC, m.BILL_NO DESC
              ) WHERE ROWNUM <= :bill_cap
        """
    else:
        mst_from = f"""
              SELECT m.BILL_NO, m.BRN_NO, m.BILL_SRL, m.W_CODE
              FROM {pos}.IAS_POS_BILL_MST m
              WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
                AND {hung_m}
                {branch_filter}
        """

    sales_sql = f"""
        SELECT
            TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
            TO_CHAR(d.I_CODE) AS ITEM_CODE,
            TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
            SUM(NVL(d.I_QTY, 0)) AS QTY_TOTAL,
            SUM({stock_qty}) AS STOCK_QTY,
            SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS NET_TOTAL
        FROM (
            {mst_from}
        ) m
        JOIN {pos}.IAS_POS_BILL_DTL d
          ON d.BILL_NO = m.BILL_NO
         AND d.BRN_NO = m.BRN_NO
         AND NVL(d.BILL_SRL, 0) = NVL(m.BILL_SRL, 0)
        WHERE d.I_CODE IS NOT NULL
        GROUP BY m.BRN_NO, d.I_CODE, NVL(d.W_CODE, m.W_CODE)
    """
    returns_sql = f"""
        SELECT
            TO_CHAR(m.BRN_NO) AS BRANCH_CODE,
            TO_CHAR(d.I_CODE) AS ITEM_CODE,
            TO_CHAR(NVL(d.W_CODE, m.W_CODE)) AS W_CODE,
            SUM(NVL(d.I_QTY, 0)) AS QTY_TOTAL,
            SUM({stock_qty}) AS STOCK_QTY,
            SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)) AS NET_TOTAL
        FROM {pos}.IAS_POS_RT_BILL_MST m
        JOIN {pos}.IAS_POS_RT_BILL_DTL d
          ON d.RT_BILL_NO = m.RT_BILL_NO
         AND d.BRN_NO = m.BRN_NO
        WHERE m.RT_BILL_DATE >= :d_from AND m.RT_BILL_DATE < :d_to_excl
          AND {_hung_ok("m")}
          AND d.I_CODE IS NOT NULL
          {branch_filter}
        GROUP BY m.BRN_NO, d.I_CODE, NVL(d.W_CODE, m.W_CODE)
    """

    gmap = _item_group_code_map() if gcode else {}
    cmap = _item_unit_cost_map()
    wh_cmap = _item_wh_unit_cost_map()

    def _unit_cost(item: str, wh: str) -> float:
        by_wh = wh_cmap.get(item)
        if by_wh:
            if wh and wh in by_wh:
                return float(by_wh[wh] or 0)
            for val in by_wh.values():
                if val:
                    return float(val)
        return float(cmap.get(item) or 0)

    def _run_sql(sql: str, bind: dict) -> list[dict]:
        with oracle_session():
            return _fetch_all(sql, bind)

    if skip_returns:
        item_rows = _fetch_all(sales_sql, params)
        returns_rows: list[dict] = []
    else:
        item_rows, returns_rows = _run_parallel(
            [
                (lambda sql=sales_sql, bind=dict(params): _run_sql(sql, bind)),
                (lambda sql=returns_sql, bind=dict(params): _run_sql(sql, bind)),
            ],
            max_workers=2,
        )

    item_acc: dict[tuple[str, str, str], dict[str, float]] = {}

    def _touch(
        branch: str, item: str, wh: str, qty: float, stock_qty_v: float, net: float
    ) -> None:
        key = (branch, item, wh)
        row = item_acc.get(key)
        if row is None:
            row = {"qty": 0.0, "stock_qty": 0.0, "net": 0.0}
            item_acc[key] = row
        row["qty"] += qty
        row["stock_qty"] += stock_qty_v
        row["net"] += net

    for row in item_rows:
        item = str(row.get("ITEM_CODE") or "").strip()
        if not item:
            continue
        b_code = str(row.get("BRANCH_CODE") or "").strip() or "(بلا)"
        wh = str(row.get("W_CODE") or "").strip()
        _touch(
            b_code,
            item,
            wh,
            float(row.get("QTY_TOTAL") or 0),
            float(row.get("STOCK_QTY") or 0),
            float(row.get("NET_TOTAL") or 0),
        )

    for row in returns_rows:
        item = str(row.get("ITEM_CODE") or "").strip()
        if not item:
            continue
        b_code = str(row.get("BRANCH_CODE") or "").strip() or "(بلا)"
        wh = str(row.get("W_CODE") or "").strip()
        _touch(
            b_code,
            item,
            wh,
            -float(row.get("QTY_TOTAL") or 0),
            -float(row.get("STOCK_QTY") or 0),
            -float(row.get("NET_TOTAL") or 0),
        )

    brn_acc: dict[str, dict[str, float]] = {}

    def _add(
        bucket: dict[str, dict[str, float]], key: str, qty: float, net: float, cost: float
    ) -> None:
        if not key:
            return
        row = bucket.get(key)
        if row is None:
            row = {"qty": 0.0, "net": 0.0, "cost": 0.0}
            bucket[key] = row
        row["qty"] += qty
        row["net"] += net
        row["cost"] += cost

    for (b_code, item, wh), vals in item_acc.items():
        if gcode:
            if gmap.get(item, "(بلا)") != gcode:
                continue
        qty = float(vals["qty"])
        net = float(vals["net"])
        cost = float(vals["stock_qty"]) * _unit_cost(item, wh)
        _add(brn_acc, b_code, qty, net, cost)

    branch_names = _branch_names()
    branch_rows = [
        {
            "CODE": code,
            "NAME": branch_names.get(code) or code,
            "QTY_TOTAL": vals["qty"],
            "NET_TOTAL": vals["net"],
            "COST_TOTAL": vals["cost"],
        }
        for code, vals in brn_acc.items()
    ]
    return (
        _margin_rank_rows(
            branch_rows, code_key="branch_code", name_key="branch_name", names=branch_names
        ),
        [],
    )


def fetch_margin_ranks(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    limit: int = 15,
    force_fast: bool | None = None,
    quick: bool = False,
) -> dict[str, list[dict]]:
    """هامش الربح كما تقرير النظام — تجميع الفروع فقط.

    الآجل: STK_COST × الكمية من IAS_BILL_DTL.
    نقاط البيع: I_CWTAVG لمخزن السطر × كمية المخزون (P_QTY / I_QTY×P_SIZE).
    quick=True: أحدث ~8 آلاف فاتورة فقط (معاينة سريعة).
    """
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    lim = max(1, int(limit or 15))
    # الافتراضي: بدون خصم مرتجع على الهامش (أسرع؛ مثل المجموعات) — fast=0 للصافي
    fast = True if force_fast is None else bool(force_fast)
    mode = "gross" if fast else "net"
    qtag = "q1" if quick else "q0"
    cache_key = (
        f"sales:margin:v18:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{lim}:{mode}:{qtag}:brn"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    conf = _system_conf(system)
    if conf.get("source") == "pos":
        branches, groups = _fetch_pos_margin_ranks(
            date_from,
            date_to,
            brn,
            gcode,
            skip_returns=fast,
            recent_bills=8000 if quick else None,
        )
    else:
        branches, groups = _fetch_bill_margin_ranks(date_from, date_to, brn, gcode)

    del lim
    payload = {"branches": branches, "groups": groups}
    _sales_cache_set(cache_key, payload, date_from=date_from, date_to=date_to)
    return payload
