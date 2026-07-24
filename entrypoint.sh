#!/bin/sh
set -e

mkdir -p /app/data /app/staticfiles /app/certs

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# شهادة self-signed احتياطية (الأفضل Let's Encrypt عبر Dokploy للدومين)
if [ ! -f /app/certs/cert.pem ] || [ ! -f /app/certs/key.pem ]; then
  echo "Generating self-signed TLS certificate..."
  OPENSSL_HOST="${TLS_HOST:-item.alrsheed.net}"
  OPENSSL_IP="${TLS_IP:-72.61.107.230}"
  cat > /tmp/openssl.cnf <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
CN = ${OPENSSL_HOST}

[v3_req]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
DNS.1 = ${OPENSSL_HOST}
DNS.2 = localhost
IP.1 = ${OPENSSL_IP}
EOF
  openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout /app/certs/key.pem \
    -out /app/certs/cert.pem \
    -days 825 \
    -config /tmp/openssl.cnf
fi

# Gunicorn يستمع على كل الواجهات ليبقى HTTP :8084 يعمل
gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-180}" \
  --access-logfile - \
  --error-logfile - &
GUNICORN_PID=$!

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS "http://127.0.0.1:${PORT:-8000}/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

nginx -c /app/deploy/nginx-ssl.conf -g 'daemon off;' &
NGINX_PID=$!

term() {
  kill -TERM "$NGINX_PID" "$GUNICORN_PID" 2>/dev/null || true
  wait "$NGINX_PID" "$GUNICORN_PID" 2>/dev/null || true
}
trap term INT TERM

wait -n "$NGINX_PID" "$GUNICORN_PID"
EXIT_CODE=$?
term
exit "$EXIT_CODE"
