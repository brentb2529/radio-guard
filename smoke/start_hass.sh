#!/usr/bin/env bash
# (Re)start the smoke-test HA instance and wait until the API answers.
cd "$(dirname "$0")"
pkill -f "hass -c config" 2>/dev/null; sleep 2
nohup ../.venv/bin/hass -c config >> hass.log 2>&1 &
for i in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $(cat token.txt 2>/dev/null)" http://127.0.0.1:8123/api/ || true)
  [ "$code" = "200" ] && { sleep 6; echo "HA up"; exit 0; }
  sleep 2
done
echo "HA failed to start"; tail -20 hass.log; exit 1
