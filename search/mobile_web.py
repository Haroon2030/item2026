"""واجهة تطبيق الموبايل (Flutter web) على /app/."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_http_methods

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


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == '.js':
        return 'text/javascript; charset=utf-8'
    if suffix == '.mjs':
        return 'text/javascript; charset=utf-8'
    if suffix == '.wasm':
        return 'application/wasm'
    if suffix == '.json':
        return 'application/json; charset=utf-8'
    if suffix in {'.html', '.htm'} or path.name == 'index.html':
        return 'text/html; charset=utf-8'
    if suffix == '.css':
        return 'text/css; charset=utf-8'
    if suffix == '.svg':
        return 'image/svg+xml'
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or 'application/octet-stream'


@require_http_methods(['GET', 'HEAD'])
def mobile_web_app(request, asset: str = ''):
    """يقدّم بناء Flutter web بلا Content-Disposition حتى يعمل Safari."""
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

    content_type = _content_type(path)
    if request.method == 'HEAD':
        resp = HttpResponse(b'', content_type=content_type)
        resp['Content-Length'] = str(path.stat().st_size)
    else:
        resp = HttpResponse(path.read_bytes(), content_type=content_type)
    if 'Content-Disposition' in resp:
        del resp['Content-Disposition']
    if path.name == 'index.html':
        resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    else:
        resp['Cache-Control'] = 'public, max-age=3600'
    return resp
