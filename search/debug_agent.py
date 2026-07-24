"""Temporary debug-mode logging helpers (session 5b001b)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

LOG_PATH = Path(settings.BASE_DIR) / 'debug-5b001b.log'
CACHE_KEY = 'agent_dbg_5b001b'
CACHE_LIMIT = 80


def agent_log(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    payload = {
        'sessionId': '5b001b',
        'hypothesisId': hypothesis_id,
        'location': location,
        'message': message,
        'data': data or {},
        'timestamp': int(time.time() * 1000),
    }
    try:
        with LOG_PATH.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except OSError:
        pass
    try:
        rows = cache.get(CACHE_KEY) or []
        rows.append(payload)
        cache.set(CACHE_KEY, rows[-CACHE_LIMIT:], timeout=3600)
    except Exception:
        pass


def agent_logs_dump() -> list:
    try:
        return list(cache.get(CACHE_KEY) or [])
    except Exception:
        return []
