"""واجهة تطبيق الموبايل (Flutter web) على /app/."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.http import require_GET

_ROOT = Path(settings.BASE_DIR) / 'mobile_web'


def _safe_file(rel: str) -> Path | None:
    root = _ROOT.resolve()
    if not root.is_dir():
        return None
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


@require_GET
def mobile_web_app(request, asset: str = ''):
    """يقدّم بناء Flutter web من المجلد mobile_web."""
    rel = (asset or '').replace('\\', '/').lstrip('/')
    if not rel or rel.endswith('/'):
        rel = 'index.html'
    path = _safe_file(rel)
    if path is None and '.' not in Path(rel).name:
        path = _safe_file('index.html')
    if path is None:
        if not _ROOT.is_dir():
            return HttpResponse(
                'تطبيق الموبايل غير مضمّن في هذا النشر.',
                status=503,
                content_type='text/plain; charset=utf-8',
            )
        raise Http404()
    content_type, _ = mimetypes.guess_type(str(path))
    if path.suffix == '.js':
        content_type = 'application/javascript'
    elif path.suffix == '.wasm':
        content_type = 'application/wasm'
    elif path.suffix == '.json':
        content_type = 'application/json'
    elif path.name == 'index.html':
        content_type = 'text/html; charset=utf-8'
    resp = FileResponse(path.open('rb'), content_type=content_type or 'application/octet-stream')
    if path.name == 'index.html':
        resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    else:
        resp['Cache-Control'] = 'public, max-age=86400'
    return resp
