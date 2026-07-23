"""التحقق من مدخلات البحث والمخزن."""

from __future__ import annotations

import re

from django.conf import settings

# أرقام/حروف/شرطة/شرطة سفلية/slash شائع في أكواد أونكس
_QUERY_RE = re.compile(r'^[\w\-/\\.]+$', re.UNICODE)
_WAREHOUSE_RE = re.compile(r'^[0-9A-Za-z_-]{1,16}$')


class ValidationError(ValueError):
    pass


def sanitize_search_query(raw: str | None) -> str:
    query = (raw or '').strip()
    max_len = int(getattr(settings, 'SEARCH_QUERY_MAX_LEN', 64))
    if not query:
        return ''
    if len(query) > max_len:
        raise ValidationError(f'نص البحث طويل جدًا (الحد {max_len} حرفًا).')
    # منع رموز حقن/تحكم شائعة مع السماح بالعربية عبر \w
    if not _QUERY_RE.match(query):
        raise ValidationError('نص البحث يحتوي رموزًا غير مسموحة.')
    return query


def resolve_warehouse(raw: str | None, warehouses: list[dict], default: str) -> str:
    allowed = {str(w.get('code')) for w in warehouses}
    selected = (raw or '').strip()
    if selected:
        if not _WAREHOUSE_RE.match(selected) or selected not in allowed:
            raise ValidationError('المخزن المحدد غير صالح.')
        return selected
    if default in allowed:
        return default
    return next(iter(allowed), '60')
