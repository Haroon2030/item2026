#!/bin/sh
set -e

mkdir -p /app/data /app/staticfiles

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# فهرس العبوة/الباركود: إن كان فارغاً نزامنه قبل فتح الموقع
# يحتاج VOLUME على /app/data حتى لا يُمسح مع كل Redeploy
python manage.py ensure_barcode_index || echo "WARN: auto sync skipped/failed"

# تشغيل مباشر ومستقر (HTTPS عبر Dokploy/Traefik على الدومين)
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-180}" \
  --access-logfile - \
  --error-logfile -
