"""
قراءة موجود المخزون والموردين ومبيعات نقاط البيع من أوراكل أونكس — SELECT فقط، بلا أي كتابة.

الجداول المستخدمة (قراءة):
- IAS_ITM_WCODE   : الكمية/التكلفة حسب المخزن
- IAS_ITM_MST     : اسم الصنف والمجموعة وحالة Inactive
- IAS_PI_BILL_*   : فواتير الشراء (الموردون الذين نُزّل منهم الصنف)
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
_SALES_CACHE_TTL = 300  # 5 دقائق للفترة القصيرة
_LOOKUP_CACHE_TTL = 3600  # ساعة لأسماء الفروع/المستخدمين/مالك POS
_pool = None
_pool_lock = threading.Lock()
_pool_dsn = None
_pool_user = None


class OracleStockError(Exception):
    """فشل قراءة المخزون من أوراكل."""


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
    global _client_ready
    if _client_ready:
        return
    with _client_lock:
        if _client_ready:
            return
        import oracledb

        lib_dir = str(_cfg().get("CLIENT_LIB_DIR") or "").strip()
        # على Docker/Linux غالباً thin mode يكفي؛ مسار Windows المحلي يُتجاهل بأمان.
        if lib_dir:
            try:
                oracledb.init_oracle_client(lib_dir=lib_dir)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Oracle thick client lib_dir failed (%s); using thin mode",
                    exc,
                )
        else:
            try:
                oracledb.init_oracle_client()
            except Exception:
                logger.warning("Oracle thick client init skipped; using thin mode")
        _client_ready = True


def _oracle_dsn() -> tuple[str, str, str]:
    """يعيد (user, password, dsn)."""
    import oracledb

    cfg = _cfg()
    user = str(cfg.get("USER") or "").strip()
    password = str(cfg.get("PASSWORD") or "")
    host = str(cfg.get("HOST") or "").strip()
    port = int(cfg.get("PORT") or 1521)
    service = str(cfg.get("SERVICE_NAME") or "").strip()
    sid = str(cfg.get("SID") or "").strip()
    if not (user and password and host and (service or sid)):
        raise OracleStockError("إعدادات أوراكل غير مكتملة.")
    _init_thick_client()
    if service:
        dsn = oracledb.makedsn(host, port, service_name=service)
    else:
        dsn = oracledb.makedsn(host, port, sid=sid)
    return user, password, dsn


def _get_pool():
    """مجمع اتصالات أوراكل مشترك — يقلّل زمن فتح الاتصال."""
    global _pool, _pool_dsn, _pool_user
    user, password, dsn = _oracle_dsn()
    with _pool_lock:
        if _pool is not None and _pool_dsn == dsn and _pool_user == user:
            return _pool
        import oracledb

        if _pool is not None:
            try:
                _pool.close(force=True)
            except Exception:
                pass
            _pool = None
        try:
            _pool = oracledb.create_pool(
                user=user,
                password=password,
                dsn=dsn,
                min=1,
                max=8,
                increment=1,
            )
            _pool_dsn = dsn
            _pool_user = user
            logger.info("Oracle connection pool ready (max=8)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Oracle pool create failed: %s", exc)
            _pool = None
        return _pool


def _connect():
    import oracledb

    pool = _get_pool()
    if pool is not None:
        try:
            conn = pool.acquire()
            setattr(conn, "_from_pool", True)
            return conn
        except Exception as exc:  # noqa: BLE001
            logger.warning("Oracle pool acquire failed: %s", exc)
    user, password, dsn = _oracle_dsn()
    conn = oracledb.connect(user=user, password=password, dsn=dsn)
    setattr(conn, "_from_pool", False)
    return conn


def _release_conn(conn) -> None:
    if conn is None:
        return
    from_pool = bool(getattr(conn, "_from_pool", False))
    if from_pool:
        try:
            pool = _get_pool()
            if pool is not None:
                pool.release(conn)
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


def _use_fast_sales(date_from, date_to) -> bool:
    """لوحات التفاصيل (مجموعات/أصناف): بلا مرتجعات دائماً — أسرع وأقل TEMP."""
    return True


def sales_fast_mode(date_from, date_to) -> bool:
    return _use_fast_sales(date_from, date_to)


def _skip_mst_returns(date_from, date_to) -> bool:
    """تخطي مرتجعات رأس الفاتورة للفترات ≥14 يوم أو للوضع الخفيف."""
    return _date_span_days(date_from, date_to) >= 14


def _date_params(date_from, date_to) -> dict[str, date]:
    """حدود تاريخ نصف مفتوحة [from, to+1) لاستخدام فهارس BILL_DATE."""
    d_from = _as_date(date_from)
    d_to_excl = _as_date(date_to) + timedelta(days=1)
    return {"d_from": d_from, "d_to_excl": d_to_excl}


def _sales_cache_ttl(date_from=None, date_to=None) -> int:
    if date_from is None or date_to is None:
        return _SALES_CACHE_TTL
    days = _date_span_days(date_from, date_to)
    if days >= 180:
        return 1800
    if days >= 90:
        return 1200
    if days >= 31:
        return 600
    return _SALES_CACHE_TTL


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
        owned = True
    try:
        cur = conn.cursor()
        cur.execute(safe_sql, params or {})
        cols = [d[0].upper() for d in (cur.description or [])]
        rows = []
        for tup in cur:
            rows.append({cols[i]: tup[i] for i in range(len(cols))})
        return rows
    finally:
        if owned:
            _release_conn(conn)


def _sales_cache_get(key: str):
    try:
        return cache.get(key)
    except Exception:
        return None


def _sales_cache_set(
    key: str,
    value: Any,
    ttl: int | None = None,
    *,
    date_from=None,
    date_to=None,
) -> None:
    try:
        if ttl is None:
            ttl = _sales_cache_ttl(date_from, date_to)
        cache.set(key, value, ttl)
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
    catalog = int(rows[0].get("CATALOG_COUNT") or 0)
    zero = int(rows[0].get("ZERO_COUNT") or 0)
    return catalog, zero


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
    """إجماليات نقاط البيع من IAS_POS_BILL_MST / IAS_POS_RT_BILL_MST."""
    pos = _pos_owner()
    params = _date_params(date_from, date_to)
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
          AND NVL(p.HUNG, 0) = 0
        GROUP BY TO_CHAR(p.BRN_NO)
        """,
        params,
    )
    returns_by_brn: dict[str, tuple[int, float, float]] = {}
    if _skip_mst_returns(date_from, date_to):
        return _assemble_branch_rows(sales_rows, returns_by_brn)
    try:
        for row in _fetch_all(
            f"""
            SELECT
                TO_CHAR(r.BRN_NO) AS BRANCH_CODE,
                COUNT(DISTINCT r.RT_BILL_NO) AS RET_COUNT,
                ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {pos}.IAS_POS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND NVL(r.HUNG, 0) = 0
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
        logger.warning("POS returns totals skipped: %s", exc)
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
          AND NVL(b.CNCL_FLG, 0) = 0
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
              AND NVL(r.CNCL_FLG, 0) = 0
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
        logger.warning("Bill returns totals skipped: %s", exc)
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
        f"sales:branches:v3:{system}:{_as_date(date_from).isoformat()}:"
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
    skip_ret = light or _skip_mst_returns(date_from, date_to)
    lim = max(1, min(int(top_users_limit or 8), 50))
    cache_key = (
        f"sales:mst_bundle:v1:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:L{int(light)}:u{lim}:r{int(not skip_ret)}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    params = _date_params(date_from, date_to)
    if conf.get("source") == "pos":
        pos = _pos_owner()
        if light:
            sales_rows = _fetch_all(
                f"""
                SELECT
                  CASE WHEN GROUPING(p.BRN_NO) = 0 THEN 'BRN' ELSE 'TOT' END AS KIND,
                  TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
                  CAST(NULL AS VARCHAR2(40)) AS USER_CODE,
                  COUNT(DISTINCT p.BILL_NO) AS INVOICE_COUNT,
                  ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
                  ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL,
                  ROUND(SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)), 2) AS GROSS_TOTAL,
                  COUNT(DISTINCT NVL(TO_CHAR(p.MACHINE_NO), TO_CHAR(p.CASH_NO))) AS DEVICE_COUNT,
                  COUNT(DISTINCT p.AD_U_ID) AS SELLER_COUNT
                FROM {pos}.IAS_POS_BILL_MST p
                WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
                  AND NVL(p.HUNG, 0) = 0
                GROUP BY GROUPING SETS ((p.BRN_NO), ())
                """,
                params,
            )
        else:
            sales_rows = _fetch_all(
                f"""
                SELECT
                  CASE
                    WHEN GROUPING(p.BRN_NO) = 0 THEN 'BRN'
                    WHEN GROUPING(p.AD_U_ID) = 0 THEN 'USR'
                    ELSE 'TOT'
                  END AS KIND,
                  TO_CHAR(p.BRN_NO) AS BRANCH_CODE,
                  TO_CHAR(p.AD_U_ID) AS USER_CODE,
                  COUNT(DISTINCT p.BILL_NO) AS INVOICE_COUNT,
                  ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
                  ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL,
                  ROUND(SUM(NVL(p.BILL_AMT, 0) + NVL(p.VAT_AMT, 0)), 2) AS GROSS_TOTAL,
                  COUNT(DISTINCT NVL(TO_CHAR(p.MACHINE_NO), TO_CHAR(p.CASH_NO))) AS DEVICE_COUNT,
                  COUNT(DISTINCT p.AD_U_ID) AS SELLER_COUNT
                FROM {pos}.IAS_POS_BILL_MST p
                WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
                  AND NVL(p.HUNG, 0) = 0
                GROUP BY GROUPING SETS ((p.BRN_NO), (p.AD_U_ID), ())
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
                        COUNT(DISTINCT r.RT_BILL_NO) AS RET_COUNT,
                        ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                        ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
                    FROM {pos}.IAS_POS_RT_BILL_MST r
                    WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                      AND NVL(r.HUNG, 0) = 0
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
                logger.warning("POS MST bundle returns skipped: %s", exc)
    else:
        schema = _schema()
        doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
        cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        if light:
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
                  AND NVL(b.CNCL_FLG, 0) = 0
                  {doc_filter}
                  {cash_filter}
                GROUP BY GROUPING SETS ((b.BRN_NO), ())
                """,
                params,
            )
        else:
            sales_rows = _fetch_all(
                f"""
                SELECT
                  CASE
                    WHEN GROUPING(b.BRN_NO) = 0 THEN 'BRN'
                    WHEN GROUPING(b.AD_U_ID) = 0 THEN 'USR'
                    ELSE 'TOT'
                  END AS KIND,
                  TO_CHAR(b.BRN_NO) AS BRANCH_CODE,
                  TO_CHAR(b.AD_U_ID) AS USER_CODE,
                  COUNT(DISTINCT b.BILL_SER) AS INVOICE_COUNT,
                  ROUND(SUM(NVL(b.BILL_AMT, 0)), 2) AS NET_TOTAL,
                  ROUND(SUM(NVL(b.VAT_AMT, 0)), 2) AS VAT_TOTAL,
                  ROUND(SUM(NVL(b.BILL_AMT, 0) + NVL(b.VAT_AMT, 0)), 2) AS GROSS_TOTAL,
                  COUNT(DISTINCT TO_CHAR(b.CASH_NO)) AS DEVICE_COUNT,
                  COUNT(DISTINCT b.AD_U_ID) AS SELLER_COUNT
                FROM {schema}.IAS_BILL_MST b
                WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
                  AND NVL(b.CNCL_FLG, 0) = 0
                  {doc_filter}
                  {cash_filter}
                GROUP BY GROUPING SETS ((b.BRN_NO), (b.AD_U_ID), ())
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
                      AND NVL(r.CNCL_FLG, 0) = 0
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
                logger.warning("Bill MST bundle returns skipped: %s", exc)

    branch_sales = [r for r in sales_rows if str(r.get("KIND") or "") == "BRN"]
    user_sales = [r for r in sales_rows if str(r.get("KIND") or "") == "USR"]
    tot_rows = [r for r in sales_rows if str(r.get("KIND") or "") == "TOT"]
    branches = _assemble_branch_rows(branch_sales, returns_by_brn)
    # بائعون بدون خصم مرتجعات على مستوى المستخدم (توفير استعلام) — المبالغ من المبيعات
    top_users: list[dict] = []
    if not light:
        top_users = _assemble_user_rows(user_sales, {}, lim)
    seller_count = int((tot_rows[0].get("SELLER_COUNT") if tot_rows else 0) or 0)
    device_count = int((tot_rows[0].get("DEVICE_COUNT") if tot_rows else 0) or 0)
    if not seller_count and top_users:
        seller_count = len(user_sales)
    if not device_count and conf.get("source") != "pos":
        device_count = 0

    # مزامنة كاش الفروع المنفصل لواجهات أخرى
    br_cache = (
        f"sales:branches:v3:{system}:{_as_date(date_from).isoformat()}:"
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


def _fetch_pos_daily_totals(date_from, date_to, branch_code: str) -> list[dict]:
    """إجماليات يومية لنقاط البيع لفرع واحد."""
    pos = _pos_owner()
    params = {**_date_params(date_from, date_to), "brn": branch_code}
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
          AND TO_CHAR(p.BRN_NO) = :brn
          AND NVL(p.HUNG, 0) = 0
        GROUP BY TRUNC(p.BILL_DATE)
        ORDER BY TRUNC(p.BILL_DATE) DESC
        """,
        params,
    )
    returns_by_day: dict[str, tuple[int, float, float]] = {}
    try:
        for row in _fetch_all(
            f"""
            SELECT
                TRUNC(r.RT_BILL_DATE) AS SALE_DAY,
                COUNT(*) AS RET_COUNT,
                ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {pos}.IAS_POS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND TO_CHAR(r.BRN_NO) = :brn
              AND NVL(r.HUNG, 0) = 0
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
        logger.warning("POS daily returns skipped: %s", exc)
    return _assemble_daily_rows(sales_rows, returns_by_day)


def _fetch_bill_daily_totals(date_from, date_to, branch_code: str, conf: dict) -> list[dict]:
    """إجماليات يومية من IAS_BILL_MST لفرع واحد."""
    schema = _schema()
    params: dict = {**_date_params(date_from, date_to), "brn": branch_code}
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
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
          AND TO_CHAR(b.BRN_NO) = :brn
          {doc_filter}
          {cash_filter}
          AND NVL(b.CNCL_FLG, 0) = 0
        GROUP BY TRUNC(b.BILL_DATE)
        ORDER BY TRUNC(b.BILL_DATE) DESC
        """,
        params,
    )
    returns_by_day: dict[str, tuple[int, float, float]] = {}
    try:
        ret_params: dict = {**_date_params(date_from, date_to), "brn": branch_code}
        ret_doc_filter = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash_filter = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        for row in _fetch_all(
            f"""
            SELECT
                TRUNC(r.RT_BILL_DATE) AS SALE_DAY,
                COUNT(DISTINCT r.RT_BILL_SER) AS RET_COUNT,
                ROUND(SUM(NVL(r.BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {schema}.IAS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              AND TO_CHAR(r.BRN_NO) = :brn
              {ret_doc_filter}
              {ret_cash_filter}
              AND NVL(r.CNCL_FLG, 0) = 0
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
        logger.warning("Bill daily returns skipped: %s", exc)
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


def _is_temp_space_error(exc: BaseException) -> bool:
    text = str(exc)
    return "ORA-01652" in text or "unable to extend temp segment" in text.lower()


def _group_name_lookup() -> dict[str, str]:
    return {
        str(g.get("code") or "").strip(): str(g.get("name") or "").strip()
        for g in fetch_sales_group_options()
        if str(g.get("code") or "").strip()
    }


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
) -> list[dict]:
    """مبيعات نقاط البيع مجمّعة حسب المجموعة (أو المجموعة×الفرع).

    تجميع على مرحلتين بدل COUNT(DISTINCT نص طويل) لتقليل TEMP.
    أسماء المجموعات تُحلّ في بايثون من GROUP_DETAILS المخزّن مؤقتاً.
    """
    pos = _pos_owner()
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    group_filter = ""
    if branch_code:
        params["brn"] = branch_code
        branch_filter = "AND TO_CHAR(m.BRN_NO) = :brn"
    if group_code:
        params["gcode"] = group_code
        group_filter = "AND TO_CHAR(i.G_CODE) = :gcode"

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

    if by_branch:
        light_branch = "TO_CHAR(m.BRN_NO) AS BRANCH_CODE,"
        light_group = "NVL(TO_CHAR(i.G_CODE), '(بلا)'), TO_CHAR(m.BRN_NO)"
    else:
        light_branch = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
        light_group = "NVL(TO_CHAR(i.G_CODE), '(بلا)')"

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
              AND NVL(m.HUNG, 0) = 0
              {branch_filter}
              {group_filter}
            GROUP BY i.G_CODE, m.BRN_NO, m.BILL_NO, NVL(m.BILL_SRL, 0)
        ) x
        GROUP BY {outer_group}
        """
    # احتياطي أخف: مبالغ فقط بدون تجميع على مستوى الفاتورة
    light_sql = f"""
        SELECT
            NVL(TO_CHAR(i.G_CODE), '(بلا)') AS GROUP_CODE,
            {light_branch}
            0 AS INVOICE_COUNT,
            ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
            ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
        FROM {pos}.IAS_POS_BILL_DTL d
        JOIN {pos}.IAS_POS_BILL_MST m
          ON m.BILL_NO = d.BILL_NO
         AND m.BRN_NO = d.BRN_NO
         AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
        LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
          AND NVL(m.HUNG, 0) = 0
          {branch_filter}
          {group_filter}
        GROUP BY {light_group}
        """

    try:
        # نظرة عامة: تجميع خفيف (مبالغ) — أسرع بكثير من تجميع كل فاتورة
        # تفصيل مجموعة×فرع: تجميع على مرحلتين لعدد الفواتير بدقة
        sales_rows = _fetch_all(sales_sql if by_branch else light_sql, params)
    except Exception as exc:  # noqa: BLE001
        if by_branch and _is_temp_space_error(exc):
            logger.warning("POS group sales TEMP; light fallback: %s", exc)
            skip_returns = True
            sales_rows = _fetch_all(light_sql, params)
        else:
            raise

    returns_by_key: dict[str, tuple[int, float, float, float]] = {}
    if skip_returns:
        return _assemble_group_rows(sales_rows, returns_by_key, by_branch=by_branch)
    try:
        ret_params = dict(params)
        ret_branch = "AND TO_CHAR(m.BRN_NO) = :brn" if branch_code else ""
        ret_group = "AND TO_CHAR(i.G_CODE) = :gcode" if group_code else ""
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
                  AND NVL(m.HUNG, 0) = 0
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
        logger.warning("POS group returns skipped: %s", exc)
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
        branch_filter = "AND TO_CHAR(b.BRN_NO) = :brn"
    if group_code:
        params["gcode"] = group_code
        group_filter = "AND TO_CHAR(i.G_CODE) = :gcode"

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

    if by_branch:
        light_branch = "TO_CHAR(b.BRN_NO) AS BRANCH_CODE,"
        light_group = "NVL(TO_CHAR(i.G_CODE), '(بلا)'), TO_CHAR(b.BRN_NO)"
    else:
        light_branch = "CAST(NULL AS VARCHAR2(20)) AS BRANCH_CODE,"
        light_group = "NVL(TO_CHAR(i.G_CODE), '(بلا)')"

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
              ON b.BILL_SER = d.BILL_SER
             AND b.BRN_NO = d.BRN_NO
            LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
            WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
              AND NVL(b.CNCL_FLG, 0) = 0
              {doc_filter}
              {cash_filter}
              {branch_filter}
              {group_filter}
            GROUP BY i.G_CODE, b.BRN_NO, b.BILL_SER
        ) x
        GROUP BY {outer_group}
        """
    light_sql = f"""
        SELECT
            NVL(TO_CHAR(i.G_CODE), '(بلا)') AS GROUP_CODE,
            {light_branch}
            0 AS INVOICE_COUNT,
            ROUND(SUM(NVL(d.I_QTY, 0)), 2) AS QTY_TOTAL,
            ROUND(SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(d.VAT_AMT, 0)), 2) AS VAT_TOTAL
        FROM {schema}.IAS_BILL_DTL d
        JOIN {schema}.IAS_BILL_MST b
          ON b.BILL_SER = d.BILL_SER
         AND b.BRN_NO = d.BRN_NO
        LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
        WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          AND NVL(b.CNCL_FLG, 0) = 0
          {doc_filter}
          {cash_filter}
          {branch_filter}
          {group_filter}
        GROUP BY {light_group}
        """

    try:
        sales_rows = _fetch_all(sales_sql if by_branch else light_sql, params)
    except Exception as exc:  # noqa: BLE001
        if by_branch and _is_temp_space_error(exc):
            logger.warning("Bill group sales TEMP; light fallback: %s", exc)
            skip_returns = True
            sales_rows = _fetch_all(light_sql, params)
        else:
            raise

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
            ret_params["brn"] = branch_code
            ret_branch = "AND TO_CHAR(r.BRN_NO) = :brn"
        if group_code:
            ret_params["gcode"] = group_code
            ret_group = "AND TO_CHAR(i.G_CODE) = :gcode"
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
                  AND NVL(r.CNCL_FLG, 0) = 0
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
        logger.warning("Bill group returns skipped: %s", exc)
    return _assemble_group_rows(sales_rows, returns_by_key, by_branch=by_branch)


def fetch_group_sales_totals(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    by_branch: bool | None = None,
) -> list[dict]:
    """إجماليات المبيعات حسب المجموعة — صف لكل مجموعة (كل الفروع)، أو حسب الفروع عند اختيار مجموعة."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    conf = _system_conf(system)
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    split_by_branch = bool(by_branch) if by_branch is not None else bool(gcode)
    fast = _use_fast_sales(date_from, date_to)
    cache_key = (
        f"sales:groups:v3:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{int(split_by_branch)}:f{int(fast)}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached
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
    _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows


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


def _assemble_top_item_rows(sales_rows, returns_by_item, limit: int) -> list[dict]:
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
    merged.sort(key=lambda r: (-r["sales_total"], -r["qty_total"], r["item_code"]))
    top = merged[: max(1, int(limit or 8))]
    peak = top[0]["sales_total"] if top else 0.0
    for row in top:
        share = (row["sales_total"] / peak * 100.0) if peak else 0.0
        row["share_pct"] = round(share, 1)
    return top


def _fetch_pos_top_items(
    date_from,
    date_to,
    branch_code: str = "",
    group_code: str = "",
    limit: int = 8,
    skip_returns: bool = False,
) -> list[dict]:
    pos = _pos_owner()
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    branch_filter = ""
    group_filter = ""
    if branch_code:
        params["brn"] = branch_code
        branch_filter = "AND TO_CHAR(m.BRN_NO) = :brn"
    if group_code:
        params["gcode"] = group_code
        group_filter = "AND TO_CHAR(i.G_CODE) = :gcode"

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
          FROM {pos}.IAS_POS_BILL_DTL d
          JOIN {pos}.IAS_POS_BILL_MST m
            ON m.BILL_NO = d.BILL_NO
           AND m.BRN_NO = d.BRN_NO
           AND NVL(m.BILL_SRL, 0) = NVL(d.BILL_SRL, 0)
          LEFT JOIN {schema}.IAS_ITM_MST i ON i.I_CODE = d.I_CODE
          WHERE m.BILL_DATE >= :d_from AND m.BILL_DATE < :d_to_excl
            AND NVL(m.HUNG, 0) = 0
            AND d.I_CODE IS NOT NULL
            {branch_filter}
            {group_filter}
          GROUP BY TO_CHAR(d.I_CODE)
          ORDER BY
              SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0) + NVL(d.VAT_AMT, 0)) DESC
        ) WHERE ROWNUM <= :lim
        """,
        {**params, "lim": max(int(limit or 8) * (2 if skip_returns else 3), 24)},
    )

    returns_by_item: dict[str, tuple[float, float, float]] = {}
    item_codes = [
        str(row.get("ITEM_CODE") or "").strip()
        for row in sales_rows
        if str(row.get("ITEM_CODE") or "").strip()
    ]
    if not item_codes or skip_returns:
        return _assemble_top_item_rows(sales_rows, returns_by_item, limit)
    try:
        ret_params = dict(params)
        item_filter = _item_in_filter("d.I_CODE", item_codes, ret_params)
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
        logger.warning("POS top items returns skipped: %s", exc)
    return _assemble_top_item_rows(sales_rows, returns_by_item, limit)


def _fetch_bill_top_items(
    date_from,
    date_to,
    conf: dict,
    branch_code: str = "",
    group_code: str = "",
    limit: int = 8,
    skip_returns: bool = False,
) -> list[dict]:
    schema = _schema()
    params: dict = _date_params(date_from, date_to)
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    branch_filter = ""
    group_filter = ""
    if branch_code:
        params["brn"] = branch_code
        branch_filter = "AND TO_CHAR(b.BRN_NO) = :brn"
    if group_code:
        params["gcode"] = group_code
        group_filter = "AND TO_CHAR(i.G_CODE) = :gcode"

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
            AND NVL(b.CNCL_FLG, 0) = 0
            AND d.I_CODE IS NOT NULL
            {doc_filter}
            {cash_filter}
            {branch_filter}
            {group_filter}
          GROUP BY TO_CHAR(d.I_CODE)
          ORDER BY
              SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0) + NVL(d.VAT_AMT, 0)) DESC
        ) WHERE ROWNUM <= :lim
        """,
        {**params, "lim": max(int(limit or 8) * (2 if skip_returns else 3), 24)},
    )

    returns_by_item: dict[str, tuple[float, float, float]] = {}
    item_codes = [
        str(row.get("ITEM_CODE") or "").strip()
        for row in sales_rows
        if str(row.get("ITEM_CODE") or "").strip()
    ]
    if not item_codes or skip_returns:
        return _assemble_top_item_rows(sales_rows, returns_by_item, limit)
    try:
        ret_params: dict = _date_params(date_from, date_to)
        ret_doc = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        ret_branch = ""
        ret_group = ""
        if branch_code:
            ret_params["brn"] = branch_code
            ret_branch = "AND TO_CHAR(r.BRN_NO) = :brn"
        if group_code:
            ret_params["gcode"] = group_code
            ret_group = "AND TO_CHAR(i.G_CODE) = :gcode"
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
              AND NVL(r.CNCL_FLG, 0) = 0
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
        logger.warning("Bill top items returns skipped: %s", exc)
    return _assemble_top_item_rows(sales_rows, returns_by_item, limit)


def fetch_top_sales_items(
    date_from,
    date_to,
    system: str = "pos",
    branch_code: str = "",
    group_code: str = "",
    limit: int = 8,
) -> list[dict]:
    """أكثر الأصناف مبيعاً خلال الفترة — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    fast = _use_fast_sales(date_from, date_to)
    cache_key = (
        f"sales:items:v2:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{int(limit or 8)}:f{int(fast)}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached
    conf = _system_conf(system)
    if conf.get("source") == "pos":
        rows = _fetch_pos_top_items(
            date_from, date_to, brn, gcode, limit, skip_returns=fast
        )
    else:
        rows = _fetch_bill_top_items(
            date_from, date_to, conf, brn, gcode, limit, skip_returns=fast
        )
    _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
    return rows


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
) -> list[dict]:
    """أكثر الأصناف إرجاعاً خلال الفترة (قيمة المرتجع) — SELECT فقط."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")
    brn = str(branch_code or "").strip()
    gcode = str(group_code or "").strip()
    lim = max(1, min(int(limit or 20), 50))
    cache_key = (
        f"sales:ret_items:v1:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{brn}:{gcode}:{lim}"
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
            params["brn"] = brn
            branch_filter = "AND TO_CHAR(m.BRN_NO) = :brn"
        if gcode:
            params["gcode"] = gcode
            group_filter = "AND TO_CHAR(i.G_CODE) = :gcode"
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
              GROUP BY TO_CHAR(d.I_CODE)
              ORDER BY
                  SUM(NVL(d.I_PRICE, 0) * NVL(d.I_QTY, 0) - NVL(d.DIS_AMT, 0) + NVL(d.VAT_AMT, 0)) DESC
            ) WHERE ROWNUM <= :lim
            """,
            {**params, "lim": lim},
        )
    else:
        doc_filter = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", params)
        cash_filter = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        branch_filter = ""
        group_filter = ""
        if brn:
            params["brn"] = brn
            branch_filter = "AND TO_CHAR(r.BRN_NO) = :brn"
        if gcode:
            params["gcode"] = gcode
            group_filter = "AND TO_CHAR(i.G_CODE) = :gcode"
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
                AND NVL(r.CNCL_FLG, 0) = 0
                AND d.I_CODE IS NOT NULL
                {doc_filter}
                {cash_filter}
                {branch_filter}
                {group_filter}
              GROUP BY TO_CHAR(d.I_CODE)
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
              AND NVL(b.CNCL_FLG, 0) = 0
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
              AND NVL(b.CNCL_FLG, 0) = 0
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
    limit: int = 10,
) -> list[dict]:
    """أكثر المستخدمين مبيعاً — من جدول POS أو فواتير الآجل."""
    if not oracle_enabled():
        raise OracleStockError("أوراكل غير مفعّل.")

    cache_key = (
        f"sales:users:{system}:{_as_date(date_from).isoformat()}:"
        f"{_as_date(date_to).isoformat()}:{int(limit or 10)}"
    )
    cached = _sales_cache_get(cache_key)
    if cached is not None:
        return cached

    conf = _system_conf(system)
    params: dict = _date_params(date_from, date_to)
    returns_by_user: dict[str, tuple[int, float, float]] = {}

    if conf.get("source") == "pos":
        pos = _pos_owner()
        sales_rows = _fetch_all(
            f"""
            SELECT
                TO_CHAR(p.AD_U_ID) AS USER_CODE,
                COUNT(DISTINCT p.BILL_NO) AS INVOICE_COUNT,
                ROUND(SUM(NVL(p.BILL_AMT, 0)), 2) AS NET_TOTAL,
                ROUND(SUM(NVL(p.VAT_AMT, 0)), 2) AS VAT_TOTAL
            FROM {pos}.IAS_POS_BILL_MST p
            WHERE p.BILL_DATE >= :d_from AND p.BILL_DATE < :d_to_excl
              AND NVL(p.HUNG, 0) = 0
              AND p.AD_U_ID IS NOT NULL
            GROUP BY TO_CHAR(p.AD_U_ID)
            """,
            params,
        )
        try:
            for row in _fetch_all(
                f"""
                SELECT
                    TO_CHAR(r.AD_U_ID) AS USER_CODE,
                    COUNT(DISTINCT r.RT_BILL_NO) AS RET_COUNT,
                    ROUND(SUM(NVL(r.RT_BILL_AMT, 0)), 2) AS RET_NET,
                    ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
                FROM {pos}.IAS_POS_RT_BILL_MST r
                WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
                  AND NVL(r.HUNG, 0) = 0
                  AND r.AD_U_ID IS NOT NULL
                GROUP BY TO_CHAR(r.AD_U_ID)
                """,
                params,
            ):
                code = str(row.get("USER_CODE") or "").strip()
                returns_by_user[code] = (
                    int(row.get("RET_COUNT") or 0),
                    float(row.get("RET_NET") or 0),
                    float(row.get("RET_VAT") or 0),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("POS user returns skipped: %s", exc)
        rows = _assemble_user_rows(sales_rows, returns_by_user, limit)
        _sales_cache_set(cache_key, rows, date_from=date_from, date_to=date_to)
        return rows

    schema = _schema()
    doc_filter = _doc_type_filter(conf, "b", "BILL_DOC_TYPE", params)
    cash_filter = "AND b.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
    sales_rows = _fetch_all(
        f"""
        SELECT
            TO_CHAR(b.AD_U_ID) AS USER_CODE,
            COUNT(DISTINCT b.BILL_SER) AS INVOICE_COUNT,
            ROUND(SUM(NVL(b.BILL_AMT, 0)), 2) AS NET_TOTAL,
            ROUND(SUM(NVL(b.VAT_AMT, 0)), 2) AS VAT_TOTAL
        FROM {schema}.IAS_BILL_MST b
        WHERE b.BILL_DATE >= :d_from AND b.BILL_DATE < :d_to_excl
          {doc_filter}
          {cash_filter}
          AND NVL(b.CNCL_FLG, 0) = 0
          AND b.AD_U_ID IS NOT NULL
        GROUP BY TO_CHAR(b.AD_U_ID)
        """,
        params,
    )
    try:
        ret_params: dict = _date_params(date_from, date_to)
        ret_doc_filter = _doc_type_filter(conf, "r", "RT_BILL_DOC_TYPE", ret_params)
        ret_cash_filter = "AND r.CASH_NO IS NOT NULL" if conf.get("require_cash") else ""
        for row in _fetch_all(
            f"""
            SELECT
                TO_CHAR(r.AD_U_ID) AS USER_CODE,
                COUNT(DISTINCT r.RT_BILL_SER) AS RET_COUNT,
                ROUND(SUM(NVL(r.BILL_AMT, 0)), 2) AS RET_NET,
                ROUND(SUM(NVL(r.VAT_AMT, 0)), 2) AS RET_VAT
            FROM {schema}.IAS_RT_BILL_MST r
            WHERE r.RT_BILL_DATE >= :d_from AND r.RT_BILL_DATE < :d_to_excl
              {ret_doc_filter}
              {ret_cash_filter}
              AND NVL(r.CNCL_FLG, 0) = 0
              AND r.AD_U_ID IS NOT NULL
            GROUP BY TO_CHAR(r.AD_U_ID)
            """,
            ret_params,
        ):
            code = str(row.get("USER_CODE") or "").strip()
            returns_by_user[code] = (
                int(row.get("RET_COUNT") or 0),
                float(row.get("RET_NET") or 0),
                float(row.get("RET_VAT") or 0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bill user returns skipped: %s", exc)
    rows = _assemble_user_rows(sales_rows, returns_by_user, limit)
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
              AND NVL(b.CNCL_FLG, 0) = 0
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
