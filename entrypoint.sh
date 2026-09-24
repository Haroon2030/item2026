#!/bin/sh
set -e

mkdir -p /app/data /app/data/django_cache /app/staticfiles

# انتظار جاهزية MySQL إن وُجد DB_HOST (Dokploy)
if [ -n "${DB_HOST:-}" ] || [ -n "${MYSQL_HOST:-}" ]; then
  echo "Waiting for MySQL at ${DB_HOST:-$MYSQL_HOST}:${DB_PORT:-${MYSQL_PORT:-3306}}…"
  python - <<'PY'
import os, sys, time
host = os.environ.get("DB_HOST") or os.environ.get("MYSQL_HOST") or ""
port = int(os.environ.get("DB_PORT") or os.environ.get("MYSQL_PORT") or "3306")
user = os.environ.get("DB_USER") or os.environ.get("MYSQL_USER") or ""
password = os.environ.get("DB_PASSWORD") or os.environ.get("MYSQL_PASSWORD") or ""
name = os.environ.get("DB_NAME") or os.environ.get("MYSQL_DATABASE") or "item"
if not host:
    sys.exit(0)
try:
    import pymysql
except ImportError:
    print("WARN: PyMySQL missing; skip DB wait")
    sys.exit(0)
for i in range(40):
    try:
        conn = pymysql.connect(
            host=host, port=port, user=user, password=password, database=name,
            connect_timeout=3,
        )
        conn.close()
        print("MySQL ready.")
        sys.exit(0)
    except Exception as exc:
        print(f"  attempt {i+1}/40: {exc}")
        time.sleep(2)
print("ERROR: MySQL not reachable", file=sys.stderr)
sys.exit(1)
PY
fi

python manage.py migrate --noinput
python manage.py ensure_app_user
python manage.py collectstatic --noinput

# صور شرائح الترحيب في جذر static — تُنسخ صراحة بعد collectstatic لضمان ظهورها بعد كل رفع
mkdir -p /app/staticfiles
for f in slide-decision.jpg slide-sales.jpg slide-ops.jpg; do
  if [ -f "/app/static/${f}" ]; then
    cp -f "/app/static/${f}" "/app/staticfiles/${f}"
    # نسخة WhiteNoise المضغوطة اختيارية؛ الملف الخام كافٍ للعرض
    echo "welcome-slide: installed ${f}"
  else
    echo "WARN: missing welcome slide /app/static/${f}" >&2
  fi
done
# إبقاء مسار img/welcome متوافقاً إن وُجدت نسخ قديمة في القوالب/الكاش
mkdir -p /app/staticfiles/img/welcome
for f in slide-decision.jpg slide-sales.jpg slide-ops.jpg login-hero.jpg; do
  if [ -f "/app/static/img/welcome/${f}" ]; then
    cp -f "/app/static/img/welcome/${f}" "/app/staticfiles/img/welcome/${f}"
  elif [ -f "/app/static/${f}" ]; then
    cp -f "/app/static/${f}" "/app/staticfiles/img/welcome/${f}"
  fi
done

# فهرس العبوة/الباركود: إن كان فارغاً نزامنه قبل فتح الموقع
python manage.py ensure_barcode_index || echo "WARN: auto sync skipped/failed"

# إصدار الواجهة: كل تشغيل حاوية بعد الرفع = رقم جديد → أول دخول يحدّث الكاش
if [ "${APP_CLIENT_VERSION_LOCK:-}" != "1" ]; then
  APP_CLIENT_VERSION="$(date -u +%Y%m%d%H%M%S)"
  export APP_CLIENT_VERSION
  printf '%s\n' "$APP_CLIENT_VERSION" > /tmp/app-client-version
  echo "APP_CLIENT_VERSION=${APP_CLIENT_VERSION}"
fi

# تشغيل مباشر ومستقر (HTTPS عبر Dokploy/Traefik على الدومين)
exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-1800}" \
  --access-logfile - \
  --error-logfile -
