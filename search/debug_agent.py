"""Temporary debug-mode logging helpers (session 5b001b)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from django.conf import settings

CACHE_LIMIT = 80


def _log_path() -> Path:
    # بجانب قاعدة البيانات حتى يبقى مشتركاً بين عمّال Gunicorn
    try:
        db_name = settings.DATABASES['default']['NAME']
        parent = Path(db_name).parent
        if parent.exists():
            return parent / 'debug-5b001b.log'
    except Exception:
        pass
    return Path(settings.BASE_DIR) / 'debug-5b001b.log'


def agent_log(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    payload = {
        'sessionId': '5b001b',
        'hypothesisId': hypothesis_id,
        'location': location,
        'message': message,
        'data': data or {},
        'timestamp': int(time.time() * 1000),
        'runId': 'post-fix',
    }
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except OSError:
        pass
    # نسخة محلية لمساحة العمل عند التطوير
    try:
        local = Path(settings.BASE_DIR) / 'debug-5b001b.log'
        if local.resolve() != path.resolve():
            with local.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except OSError:
        pass


def agent_logs_dump() -> list:
    path = _log_path()
    rows: list = []
    for candidate in (path, Path(settings.BASE_DIR) / 'debug-5b001b.log'):
        try:
            if not candidate.exists():
                continue
            lines = candidate.read_text(encoding='utf-8').splitlines()[-CACHE_LIMIT:]
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            continue
    # الأحدث أولاً مع إزالة التكرار البسيط
    rows = rows[-CACHE_LIMIT:]
    return rows
