FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# Instant Client مطلوب لـ Thick mode (DPY-3015 مع كلمة سر أوراكل القديمة)
ARG ORACLE_IC_ZIP_URL=https://download.oracle.com/otn_software/linux/instantclient/2370000/instantclient-basiclite-linux.x64-23.7.0.25.01.zip

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        ca-certificates \
    && (apt-get install -y --no-install-recommends libaio1t64 \
        || apt-get install -y --no-install-recommends libaio1) \
    && (ln -sf /usr/lib/x86_64-linux-gnu/libaio.so.1t64 /usr/lib/x86_64-linux-gnu/libaio.so.1 || true) \
    && mkdir -p /opt/oracle \
    && curl -fsSL -o /tmp/instantclient.zip "${ORACLE_IC_ZIP_URL}" \
    && unzip -q /tmp/instantclient.zip -d /opt/oracle \
    && rm -f /tmp/instantclient.zip \
    && IC_DIR="$(find /opt/oracle -maxdepth 1 -type d -name 'instantclient_*' | head -n 1)" \
    && test -n "${IC_DIR}" \
    && echo "${IC_DIR}" > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/staticfiles

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/" >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
