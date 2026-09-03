"""
Django settings — مع إعدادات أمان قابلة للضبط عبر البيئة.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """تحميل بسيط لملف .env بدون مكتبة خارجية."""
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / '.env')


def _env(key: str, default: str = '') -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_list(key: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(key)
    if not raw:
        return list(default or [])
    # بعض لوحات النشر تلف القيمة بعلامات اقتباس
    raw = raw.strip().strip('"').strip("'")
    return [
        part.strip().strip('"').strip("'")
        for part in raw.replace(';', ',').split(',')
        if part.strip()
    ]


def _hosts_from_origins(origins: list[str]) -> list[str]:
    from urllib.parse import urlparse

    hosts: list[str] = []
    for origin in origins:
        host = urlparse(origin).hostname
        if host and host not in hosts:
            hosts.append(host)
    return hosts


# مفتاح سري: من البيئة أو ملف محلي غير مضمّن في المستودع
_SECRET_FILE = BASE_DIR / '.secret_key'
SECRET_KEY = _env('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if _SECRET_FILE.exists():
        SECRET_KEY = _SECRET_FILE.read_text(encoding='utf-8').strip()
    else:
        SECRET_KEY = get_random_secret_key()
        _SECRET_FILE.write_text(SECRET_KEY, encoding='utf-8')

DEBUG = _env_bool('DJANGO_DEBUG', default=True)

ALLOWED_HOSTS = _env_list(
    'DJANGO_ALLOWED_HOSTS',
    default=['127.0.0.1', 'localhost', 'item.alrsheed.net', '72.61.107.230'],
)
CSRF_TRUSTED_ORIGINS = _env_list(
    'CSRF_TRUSTED_ORIGINS',
    default=[
        'http://item.alrsheed.net',
        'https://item.alrsheed.net',
        'http://72.61.107.230:8084',
        'https://72.61.107.230:8443',
        'http://127.0.0.1:8000',
        'http://localhost:8000',
    ],
)
# محلي دائماً حتى لا يفشل الدخول بعد إعادة تشغيل السيرفر
for _origin in (
    'http://127.0.0.1:8000',
    'http://localhost:8000',
    'http://127.0.0.1:8080',
    'http://localhost:8080',
):
    if _origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(_origin)
# ادمج مضيفات CSRF تلقائياً
for host in _hosts_from_origins(CSRF_TRUSTED_ORIGINS):
    if host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(host)
for host in ('item.alrsheed.net', '72.61.107.230', 'www.item.alrsheed.net'):
    if host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(host)

# في الإنتاج اسمح بكل المضيفات لتفادي 400 خلف Dokploy/Proxy
if not DEBUG:
    ALLOWED_HOSTS = ['*']
elif '*' in ALLOWED_HOSTS:
    ALLOWED_HOSTS = ['*']

if DEBUG and '*' not in ALLOWED_HOSTS:
    for host in ('127.0.0.1', 'localhost', '[::1]'):
        if host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(host)

# خلف وكيل عكسي (Dokploy / Nginx)
USE_X_FORWARDED_HOST = _env_bool('USE_X_FORWARDED_HOST', default=not DEBUG)
SECURE_PROXY_SSL_HEADER = None
if _env_bool('USE_SECURE_PROXY_SSL_HEADER', default=False):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'search',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'search.middleware.SecurityHeadersMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'search.middleware.NavPermissionMiddleware',
    # بعد الجلسة/CSRF حتى لا تُقرأ جسم POST قبل التحقق
    'search.middleware.SqlInjectionGuardMiddleware',
    'search.middleware.RateLimitMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'search.context_processors.app_client',
                'search.context_processors.nav_access',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# كاش ملفات دائم للمبيعات/الفترات الطويلة
# على الإنتاج: /app/data مجلد Docker دائم — لا يُمسَح مع كل نشر
_DATA_DIR = Path(_env('DATA_DIR', str(BASE_DIR / 'data')))
_CACHE_DIR = _DATA_DIR / 'django_cache'
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': str(_CACHE_DIR),
        # 7 أيام للشهور/الخرائط — الإنتاج يعتمد عليها بعد أول تدفئة
        'TIMEOUT': int(_env('DJANGO_CACHE_TIMEOUT', str(60 * 60 * 24 * 7)) or str(60 * 60 * 24 * 7)),
        'OPTIONS': {'MAX_ENTRIES': 50000},
    }
}

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': Path(_env('DATABASE_PATH', str(BASE_DIR / 'db.sqlite3'))),
    }
}

# MySQL/MariaDB (Dokploy) — يُفعَّل عند وجود DB_HOST
_DB_HOST = _env('DB_HOST') or _env('MYSQL_HOST')
if _DB_HOST:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': _env('DB_NAME') or _env('MYSQL_DATABASE') or 'item',
            'USER': _env('DB_USER') or _env('MYSQL_USER') or 'item_2026',
            'PASSWORD': _env('DB_PASSWORD') or _env('MYSQL_PASSWORD') or '',
            'HOST': _DB_HOST,
            'PORT': _env('DB_PORT') or _env('MYSQL_PORT') or '3306',
            'OPTIONS': {
                'charset': 'utf8mb4',
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            'CONN_MAX_AGE': int(_env('DB_CONN_MAX_AGE', '60') or '60'),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 6},
    },
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'


def _ui_fingerprint() -> str:
    """بصمة سريعة لملفات الواجهة — تتغيّر بعد أي تعديل CSS/JS/HTML."""
    digest = hashlib.sha1()
    for folder in (BASE_DIR / 'static', BASE_DIR / 'templates'):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob('*')):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {'.css', '.js', '.html', '.svg', '.ico', '.png'}:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            digest.update(path.relative_to(BASE_DIR).as_posix().encode())
            digest.update(b'\0')
            digest.update(str(int(stat.st_mtime_ns)).encode())
            digest.update(b'\0')
            digest.update(str(stat.st_size).encode())
            digest.update(b'\n')
    return digest.hexdigest()[:12]


def _app_client_version() -> str:
    explicit = os.environ.get('APP_CLIENT_VERSION', '').strip()
    if explicit:
        return explicit
    for stamp in (Path('/tmp/app-client-version'), BASE_DIR / '.client-version'):
        try:
            text = stamp.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return text
    return _ui_fingerprint() or '1'


# يُولَّد تلقائياً عند تشغيل حاوية الإنتاج حتى يُحدَّث المتصفح من أول دخول
APP_CLIENT_VERSION = _app_client_version()

AUTHENTICATION_BACKENDS = [
    'search.auth_backend.UsernameOrPhoneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

LANGUAGE_CODE = 'ar'
TIME_ZONE = 'Asia/Riyadh'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = Path(_env('STATIC_ROOT', str(BASE_DIR / 'staticfiles')))

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ---------------------------------------------------------------------------
# أمان الجلسات والكوكيز والهيدرز
# ---------------------------------------------------------------------------
# جلسات في قاعدة البيانات (لا LocMem) حتى لا تُفقد عند إعادة تشغيل العمال
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', default=not DEBUG)
# 8 ساعات افتراضياً — جدّد العمرو عند كل طلب حتى لا تنقطع أثناء التصفح الطويل
SESSION_COOKIE_AGE = int(_env('SESSION_COOKIE_AGE', '28800') or '28800')
SESSION_SAVE_EVERY_REQUEST = _env_bool('SESSION_SAVE_EVERY_REQUEST', default=True)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SECURE = _env_bool('CSRF_COOKIE_SECURE', default=not DEBUG)
CSRF_FAILURE_VIEW = 'search.csrf.csrf_failure'

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
SECURE_BROWSER_XSS_FILTER = True

if not DEBUG:
    SECURE_SSL_REDIRECT = _env_bool('SECURE_SSL_REDIRECT', default=False)
    SECURE_HSTS_SECONDS = int(_env('SECURE_HSTS_SECONDS', '0') or '0')
    SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
    SECURE_HSTS_PRELOAD = _env_bool('SECURE_HSTS_PRELOAD', False)

# حدود الطلبات
RATE_LIMIT_SEARCH_PER_MINUTE = int(_env('RATE_LIMIT_SEARCH_PER_MINUTE', '60') or '60')
RATE_LIMIT_SYNC_PER_HOUR = int(_env('RATE_LIMIT_SYNC_PER_HOUR', '10') or '10')
RATE_LIMIT_LOGIN_PER_10_MINUTES = int(_env('RATE_LIMIT_LOGIN_PER_10_MINUTES', '20') or '20')
RATE_LIMIT_LOGIN_WINDOW_SECONDS = int(_env('RATE_LIMIT_LOGIN_WINDOW_SECONDS', '120') or '120')
SEARCH_QUERY_MAX_LEN = int(_env('SEARCH_QUERY_MAX_LEN', '128') or '128')

# رمز حماية عملية المزامنة الثقيلة (اتركه فارغًا فقط للتطوير المحلي)
SYNC_SECRET = _env('SYNC_SECRET', default='')

# ---------------------------------------------------------------------------
# إعدادات الربط مع نظام الأصناف عبر API
# ---------------------------------------------------------------------------
EXTERNAL_API = {
    'BASE_URL': _env(
        'ONYX_BASE_URL',
        'http://alrhead.dyndns.ws:8090/Service/OnyxService.svc',
    ),
    'SEARCH_PATH': '/GetAllPrice',
    'METHOD': 'GET',
    'QUERY_PARAM': 'i_code',
    'TIMEOUT': int(_env('ONYX_TIMEOUT', '60') or '60'),
    'QTY_TIMEOUT': int(_env('ONYX_QTY_TIMEOUT', '45') or '45'),
    'COMPARE_TIMEOUT': int(_env('ONYX_COMPARE_TIMEOUT', '8') or '8'),
    'COMPARE_CACHE_TTL': int(_env('ONYX_COMPARE_CACHE_TTL', '1800') or '1800'),
    'RETRIES': int(_env('ONYX_RETRIES', '1') or '1'),
    'ITEMS_TIMEOUT': int(_env('ONYX_ITEMS_TIMEOUT', '180') or '180'),
    'ITEMS_PARAMS': {
        'year': int(_env('ONYX_YEAR', '2026') or '2026'),
        'active': 1,
    },
    'EXTRA_PARAMS': {
        'year': int(_env('ONYX_YEAR', '2026') or '2026'),
        'active': 1,
        'lev_no': _env('ONYX_LEV_NO', '1'),
        'price_w_code': _env('ONYX_DEFAULT_WAREHOUSE', '60'),
    },
    'WAREHOUSES': [
        {'code': '60', 'name': 'مخزن 60'},
        {'code': '1201', 'name': 'مخزن 1201'},
        {'code': '800', 'name': 'مخزن 800'},
        {'code': '1801', 'name': 'مخزن 1801'},
        {'code': '1901', 'name': 'مخزن 1901'},
        {'code': '2001', 'name': 'مخزن 2001'},
        {'code': '30', 'name': 'مخزن 30'},
        {'code': '1', 'name': 'مخزن 1'},
    ],
    'DEFAULT_WAREHOUSE': _env('ONYX_DEFAULT_WAREHOUSE', '60'),
    # مخازن مقارنة السعر/التكلفة في بحث الأصناف
    'COMPARE_WAREHOUSES': [
        c.strip()
        for c in (_env('ONYX_COMPARE_WAREHOUSES', '1201,1,30,1901,2001,1801,60,701') or '').split(',')
        if c.strip()
    ],
    'API_KEY': _env('ONYX_API_KEY', ''),
    'API_KEY_HEADER': _env('ONYX_API_KEY_HEADER', 'Authorization'),
    'API_KEY_PREFIX': _env('ONYX_API_KEY_PREFIX', 'Bearer'),
    'RESULTS_PATH': '',
    'ALLOWED_HOSTS': _env_list(
        'ONYX_ALLOWED_HOSTS',
        default=['alrhead.dyndns.ws'],
    ),
    'FIELD_MAP': {
        'code': 'I_CODE',
        'name': 'I_NAME',
        'barcode': 'BARCODE',
        'price': 'I_PRICE',
        'unit': 'ITM_UNT',
        'quantity': 'AVL_QTY',
        'avg_cost': 'I_CWTAVG',
    },
}

# ---------------------------------------------------------------------------
# أوراكل أونكس — قراءة فقط (SELECT) للموجود الحقيقي
# ---------------------------------------------------------------------------
ORACLE = {
    'ENABLED': _env_bool('ORACLE_ENABLED', default=False),
    'HOST': _env('ORACLE_HOST', ''),
    'PORT': int(_env('ORACLE_PORT', '1521') or '1521'),
    'SERVICE_NAME': _env('ORACLE_SERVICE_NAME', ''),
    'SID': _env('ORACLE_SID', ''),
    'USER': _env('ORACLE_USER', ''),
    'PASSWORD': _env('ORACLE_PASSWORD', ''),
    'SCHEMA': _env('ORACLE_SCHEMA', ''),
    'CLIENT_LIB_DIR': _env('ORACLE_CLIENT_LIB_DIR', ''),
    # مهلة فتح TCP بالثواني — قصيرة حتى لا يعلّق الواجهة عند انقطاع VPN
    'TCP_CONNECT_TIMEOUT': int(_env('ORACLE_TCP_CONNECT_TIMEOUT', '30') or '30'),
    'RETRY_COUNT': int(_env('ORACLE_RETRY_COUNT', '3') or '3'),
    'RETRY_DELAY': int(_env('ORACLE_RETRY_DELAY', '2') or '2'),
    # دقائق: فحص الاتصالات الخاملة في المجمع
    'POOL_EXPIRE_TIME': int(_env('ORACLE_POOL_EXPIRE_TIME', '4') or '4'),
    # سقف اتصالات المجمّع (تحليل المخزون يحتاج عدة جلسات متوازية)
    'POOL_MAX': int(_env('ORACLE_POOL_MAX', '12') or '12'),
    # ثوانٍ: فحص حياة الاتصال الخامل قبل إعادة استخدامه
    'POOL_PING_INTERVAL': int(_env('ORACLE_POOL_PING_INTERVAL', '30') or '30'),
    # مللي ثانية: أقصى مدة لاستعلام أوراكل واحد (call timeout)
    'CALL_TIMEOUT_MS': int(_env('ORACLE_CALL_TIMEOUT_MS', '120000') or '120000'),
}
# oracle = موجود من IAS_ITM_WCODE | api = Avl_Qty من الويب سيرفس
STOCK_QTY_SOURCE = (_env('STOCK_QTY_SOURCE', 'api') or 'api').strip().lower()
# مبيعات المجموعات: light = أسرع عبر WAN | full = مسح DTL دقيق (بطيء وقد يعلّق السنة)
GROUPS_SQL_MODE = (_env('GROUPS_SQL_MODE', 'light') or 'light').strip().lower()

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'loggers': {
        'search': {
            'handlers': ['console'],
            'level': 'INFO' if not DEBUG else 'DEBUG',
        },
        'django.request': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
