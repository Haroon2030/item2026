"""تسجيل مؤقت لتشخيص الدخول — بدون أسماء أو كلمات سر."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse

SESSION_ID = '5b001b'
LOG_PATH = Path(settings.BASE_DIR) / 'debug-5b001b.log'


def fingerprint(value: str) -> str:
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()[:10]


def auth_log(hypothesis_id: str, location: str, message: str, data: dict) -> None:
    payload = {
        'sessionId': SESSION_ID,
        'runId': 'pre-fix',
        'hypothesisId': hypothesis_id,
        'location': location,
        'message': message,
        'data': data,
        'timestamp': int(time.time() * 1000),
    }
    try:
        with LOG_PATH.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
    except OSError:
        pass


def auth_debug_dump(request):
    """مخرج مؤقت؛ السجل لا يحتوي بيانات اعتماد أو معلومات شخصية."""
    try:
        lines = LOG_PATH.read_text(encoding='utf-8').splitlines()[-100:]
        logs = [json.loads(line) for line in lines if line.strip()]
    except (OSError, ValueError):
        logs = []
    return JsonResponse({'sessionId': SESSION_ID, 'logs': logs})
