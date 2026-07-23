FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl nginx openssl \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/staticfiles /app/certs

EXPOSE 8000 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=50s --retries=3 \
  CMD curl -fk https://127.0.0.1:8443/ >/dev/null || curl -fsS "http://127.0.0.1:${PORT:-8000}/" >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
