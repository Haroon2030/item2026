#!/bin/bash
set -e

SERVICE=$(docker service ls --format '{{.Name}}' | grep -i itm2026 | head -n1)
if [ -z "$SERVICE" ]; then
  echo "ERROR: itm2026 service not found"
  docker service ls
  exit 1
fi

echo "Updating service: $SERVICE"

docker service update \
  --env-rm DJANGO_ALLOWED_HOSTS \
  --env-add DJANGO_ALLOWED_HOSTS=* \
  --env-rm DJANGO_DEBUG \
  --env-add DJANGO_DEBUG=False \
  --env-rm CSRF_TRUSTED_ORIGINS \
  --env-add 'CSRF_TRUSTED_ORIGINS=http://item.alrsheed.net,https://item.alrsheed.net,http://72.61.107.230:8084,https://72.61.107.230:8443' \
  --force \
  "$SERVICE"

echo "Waiting for new task..."
sleep 15

CID=$(docker ps -q --filter name=itm2026 | head -n1)
echo "Container: $CID"
echo -n "ENV DJANGO_ALLOWED_HOSTS="
docker exec "$CID" printenv DJANGO_ALLOWED_HOSTS
echo -n "Django ALLOWED_HOSTS="
docker exec -e DJANGO_SETTINGS_MODULE=config.settings "$CID" \
  python -c "import django; django.setup(); from django.conf import settings; print(settings.ALLOWED_HOSTS)"

echo "Done. Open https://item.alrsheed.net"
