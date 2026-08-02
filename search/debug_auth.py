"""مساعدات تشخيص دخول خفيفة — بدون كتابة ملفات على القرص."""

from __future__ import annotations

import hashlib


def fingerprint(value: str) -> str:
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()[:10]


def auth_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    """محفوظ للتوافق؛ لا يسجّل شيئاً في الإنتاج."""
    return
