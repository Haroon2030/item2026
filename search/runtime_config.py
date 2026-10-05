"""إعدادات الربط وقت التشغيل: تُقرأ من قاعدة البيانات وتتجاوز قيم .env.

تعدّل قواميس settings.ORACLE / settings.ERP_CONFIG في مكانها، فيلتقطها كل
الكود الحالي دون أي تعديل. كل عامل (worker) يتحقق من آخر تحديث كل بضع ثوانٍ.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import logging
import re
import threading
import time

from django.conf import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_defaults: dict | None = None  # لقطة قيم .env الأصلية
_applied_stamp = None
_last_check = 0.0
CHECK_EVERY_SECONDS = 5

_SCHEMA_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_$#]*$')


# ——— التشفير ———
def _fernet():
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(('conn-setting:' + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode() if plain else ''


def decrypt_secret(token: str) -> str:
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:  # noqa: BLE001 — مفتاح تغيّر أو بيانات تالفة
        logger.warning('تعذّر فك تشفير كلمة سر أوراكل المحفوظة')
        return ''


# ——— قراءة/تطبيق ———
def get_row():
    from .models import ConnectionSetting

    return ConnectionSetting.objects.filter(pk=1).first()


def parse_warehouses(text: str) -> list[dict]:
    """سطر لكل مخزن: «60=مخزن الرياض» (يقبل أيضاً : أو فاصلة أو مسافة)."""
    out, seen = [], set()
    for line in (text or '').splitlines():
        m = re.match(r'^\s*([^=:،,\s]+)\s*[=:،,]?\s*(.*?)\s*$', line)
        if not m:
            continue
        code, name = m.group(1), m.group(2)
        if code in seen:
            continue
        seen.add(code)
        out.append({'code': code, 'name': name or f'مخزن {code}'})
    return out


def _snapshot_defaults() -> dict:
    global _defaults
    if _defaults is None:
        _defaults = {
            'ORACLE': copy.deepcopy(dict(settings.ORACLE)),
            'ERP_CONFIG': copy.deepcopy(dict(settings.ERP_CONFIG)),
        }
    return _defaults


def apply_row(row) -> None:
    """يطبّق صف الإعدادات (أو الافتراضيات الفارغة إن لم يوجد) على settings."""
    d = _snapshot_defaults()
    oracle = settings.ORACLE
    erp = settings.ERP_CONFIG
    oracle.clear()
    oracle.update(copy.deepcopy(d['ORACLE']))
    erp.clear()
    erp.update(copy.deepcopy(d['ERP_CONFIG']))
    if row is None:
        return

    oracle.update(
        {
            'HOST': (row.oracle_host or '').strip(),
            'PORT': int(row.oracle_port or 1521),
            'SERVICE_NAME': (row.oracle_service_name or '').strip(),
            'USER': (row.oracle_user or '').strip(),
            'PASSWORD': decrypt_secret(row.oracle_password_enc),
            'SCHEMA': (row.oracle_schema or '').strip(),
        }
    )
    # أوراكل مفعّل متى اكتملت بياناته
    oracle['ENABLED'] = bool(
        oracle['HOST'] and oracle['USER'] and oracle['PASSWORD']
        and oracle['SERVICE_NAME'] and oracle['SCHEMA']
    )

    erp['DEFAULT_WAREHOUSE'] = (row.default_warehouse or '').strip()
    erp['COMPARE_WAREHOUSES'] = [
        c.strip() for c in re.split(r'[,\s،]+', row.compare_warehouses or '') if c.strip()
    ]
    erp['WAREHOUSES'] = parse_warehouses(row.warehouses_text)


def refresh(force: bool = False) -> None:
    """يعيد تطبيق إعدادات DB إن تغيّرت (استعلام خفيف كل 5 ثوانٍ لكل عامل)."""
    global _applied_stamp, _last_check
    now = time.monotonic()
    if not force and now - _last_check < CHECK_EVERY_SECONDS:
        return
    with _lock:
        if not force and time.monotonic() - _last_check < CHECK_EVERY_SECONDS:
            return
        _last_check = time.monotonic()
        try:
            row = get_row()
        except Exception:  # noqa: BLE001 — الجدول غير موجود قبل migrate
            return
        stamp = row.updated_at if row is not None else None
        if not force and stamp == _applied_stamp and _defaults is not None:
            return
        apply_row(row)
        _applied_stamp = stamp
        logger.info('Connection settings applied (stamp=%s)', stamp)


# ——— اختبار الاتصال ———
def test_oracle(cfg: dict) -> dict:
    """يجرّب اتصالاً مباشراً بالقيم المرسلة (دون المساس بالمجمّع الحالي)."""
    from . import oracle_stock as os_

    host = (cfg.get('HOST') or '').strip()
    user = (cfg.get('USER') or '').strip()
    pwd = cfg.get('PASSWORD') or ''
    service = (cfg.get('SERVICE_NAME') or '').strip()
    schema = (cfg.get('SCHEMA') or '').strip()
    port = int(cfg.get('PORT') or 1521)
    if not (host and user and pwd and service):
        return {'ok': False, 'error': 'أكمل الخادم والمستخدم وكلمة السر وService Name.'}
    if schema and not _SCHEMA_RE.match(schema):
        return {'ok': False, 'error': 'اسم المخطط (Schema) غير صالح.'}
    try:
        import oracledb

        os_._init_thick_client()
        connect_data = f'(SERVICE_NAME={service})'
        dsn = (
            '(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)'
            f'(HOST={os_._resolve_host_ipv4(host)})(PORT={port}))'
            f'(CONNECT_TIMEOUT=15)(CONNECT_DATA={connect_data}))'
        )
        conn = oracledb.connect(user=user, password=pwd, dsn=dsn, tcp_connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': os_._friendly_connect_error(exc)}
    result = {'ok': True, 'schemas': [], 'schema_ok': None}
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1 FROM DUAL')
        cur.fetchone()
        try:
            cur.execute(
                "SELECT DISTINCT owner FROM all_tables WHERE table_name = 'IAS_ITM_MST' ORDER BY owner"
            )
            result['schemas'] = [r[0] for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            pass
        if schema:
            try:
                cur.execute(f'SELECT COUNT(*) FROM {schema}.IAS_ITM_MST WHERE ROWNUM <= 100')
                result['items_sample'] = int(cur.fetchone()[0])
                result['schema_ok'] = True
            except Exception as exc:  # noqa: BLE001
                result['schema_ok'] = False
                result['schema_error'] = str(exc).splitlines()[0][:200]
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return result
