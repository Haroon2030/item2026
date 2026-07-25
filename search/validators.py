"""التحقق من مدخلات البحث والمخزن + كشف أنماط حقن SQL."""

from __future__ import annotations

import re
import unicodedata

from django.conf import settings


# أرقام/حروف/شرطة/شرطة سفلية/slash شائع في أكواد أونكس
# + رموز Codabar/GS1 الخاصة: $ + : %
_QUERY_RE = re.compile(r'^[\w\-/\\.$+:%]+$', re.UNICODE)
_WAREHOUSE_RE = re.compile(r'^[0-9A-Za-z_-]{1,16}$')

# أنماط شائعة لمحاولات SQL Injection (دفاع إضافي فوق ORM)
_SQLI_PATTERNS = (
    re.compile(r"('\s*or\s+'?\d|'?\s*or\s+\d+\s*=\s*\d)", re.I),
    re.compile(r'(union(\s+all)?\s+select)', re.I),
    re.compile(r'(insert\s+into|drop\s+table|alter\s+table|create\s+table|truncate\s+table|delete\s+from)', re.I),
    re.compile(r'(information_schema|sysobjects|sys\.tables|pg_catalog)', re.I),
    re.compile(r'(sleep\s*\(|benchmark\s*\(|waitfor\s+delay|pg_sleep\s*\()', re.I),
    re.compile(r'(xp_cmdshell|load_file\s*\(|into\s+(out|dump)file)', re.I),
    re.compile(r'(;\s*(select|drop|delete|update|insert|exec|execute)\b)', re.I),
    re.compile(r'(--|#|/\*|\*/)', re.I),
)

# حقول لا تُفحص (كلمات السر قد تحتوي رموزاً خاصة عمداً)
_SKIP_FIELDS = frozenset({
    'password',
    'password1',
    'password2',
    'old_password',
    'new_password1',
    'new_password2',
    'csrfmiddlewaretoken',
    'sync_secret',
})


class ValidationError(ValueError):
    pass


def _normalize_query(value: str) -> str:
    text = unicodedata.normalize('NFKC', value or '')
    cleaned = []
    for ch in text:
        if unicodedata.category(ch) in {'Mn', 'Me', 'Cf'}:
            continue
        if ch in '\u200e\u200f\u202a\u202b\u202c\u202d\u202e':
            continue
        cleaned.append(ch)
    return ''.join(cleaned).strip()


def contains_sql_injection(value: str | None) -> bool:
    """هل النص يشبه محاولة حقن SQL؟"""
    text = str(value or '')
    if not text or len(text) > 4000:
        return bool(text) and len(text) > 4000
    for pattern in _SQLI_PATTERNS:
        if pattern.search(text):
            return True
    return False


def reject_sql_injection(value: str | None, field_name: str = 'input') -> str:
    """يرفع ValidationError إن وُجدت أنماط حقن."""
    text = str(value or '')
    if contains_sql_injection(text):
        raise ValidationError(f'قيمة غير مسموحة في الحقل «{field_name}».')
    return text


def sanitize_search_query(raw: str | None) -> str:
    query = _normalize_query((raw or '').strip())
    max_len = int(getattr(settings, 'SEARCH_QUERY_MAX_LEN', 128))
    if not query:
        return ''
    if len(query) > max_len:
        raise ValidationError(f'نص البحث طويل جدًا (الحد {max_len} حرفًا).')
    if contains_sql_injection(query):
        raise ValidationError('نص البحث يحتوي أنماطاً غير مسموحة.')
    # منع رموز حقن/تحكم شائعة مع السماح بالعربية عبر \w
    if not _QUERY_RE.match(query):
        raise ValidationError('نص البحث يحتوي رموزًا غير مسموحة.')
    return query


def resolve_warehouse(raw: str | None, warehouses: list[dict], default: str) -> str:
    allowed = {str(w.get('code')) for w in warehouses}
    selected = (raw or '').strip()
    if selected:
        if contains_sql_injection(selected):
            raise ValidationError('المخزن المحدد غير صالح.')
        if not _WAREHOUSE_RE.match(selected) or selected not in allowed:
            raise ValidationError('المخزن المحدد غير صالح.')
        return selected
    if default in allowed:
        return default
    return next(iter(allowed), '60')


def scan_request_for_sql_injection(request) -> str | None:
    """
    يفحص GET/POST بحثاً عن محاولات حقن.
    يرجع اسم الحقل المخالف أو None إن كان الطلب آمناً.
    """
    sources = []
    if hasattr(request, 'GET'):
        sources.append(request.GET)
    if request.method in {'POST', 'PUT', 'PATCH'} and hasattr(request, 'POST'):
        sources.append(request.POST)

    for params in sources:
        for key in params.keys():
            name = str(key or '').lower()
            if name in _SKIP_FIELDS or name.endswith('password'):
                continue
            for value in params.getlist(key):
                if contains_sql_injection(value):
                    return key
    return None
