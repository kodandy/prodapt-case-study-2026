#!/bin/sh
# Regression check: web (nginx) keeps proxying after gateway-api / ops-ai-api are recreated
# with NEW container IPs, without web being restarted. Run via `make web-reresolve-check`.
# Host curl/wget are not used: the HTTP requests run inside the web container (busybox wget).
#
# Why the IP squatting: docker hands a recreated container its old IP back on a quiet network,
# so a plain --force-recreate would pass even with a stale nginx upstream cache. Throwaway
# containers hold the old IPs while the upstreams come back, which forces new addresses.
set -eu
DC=${DC:-docker compose}
SQUATTERS=
trap 'for s in $SQUATTERS; do docker stop -t 0 "$s" >/dev/null 2>&1 || true; done' EXIT

ip_of() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1"; }

# Arrange: stack + web are already up (make web-up); remember web's container and the upstream IPs
web_id=$($DC ps -q web)
if [ -z "$web_id" ]; then
  echo "FAIL: web is not running"
  exit 1
fi
network=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' "$web_id")
old_gw=$(ip_of "$($DC ps -q gateway-api)")
old_ops=$(ip_of "$($DC ps -q ops-ai-api)")

# Act: recreate the upstreams only (web is NOT restarted) while the old IPs are taken
$DC rm -sf gateway-api ops-ai-api
for addr in "$old_gw" "$old_ops"; do
  SQUATTERS="$SQUATTERS $(docker run -d --rm --network "$network" --ip "$addr" nginx:1.28.0-alpine sleep 300)"
done
$DC up -d --wait --force-recreate gateway-api ops-ai-api
new_gw=$(ip_of "$($DC ps -q gateway-api)")
new_ops=$(ip_of "$($DC ps -q ops-ai-api)")

# Assert 0: the check is valid, i.e. web survived and the upstreams really moved
if [ "$($DC ps -q web)" != "$web_id" ]; then
  echo "FAIL: web container was replaced during the check; result is void"
  exit 1
fi
if [ "$new_gw" = "$old_gw" ] || [ "$new_ops" = "$old_ops" ]; then
  echo "FAIL: upstream IPs did not change (gw $old_gw -> $new_gw, ops $old_ops -> $new_ops); result is void"
  exit 1
fi
echo "upstream IPs moved: gateway-api $old_gw -> $new_gw, ops-ai-api $old_ops -> $new_ops"

# Assert 1: both proxied health endpoints return exactly 200 within ~20s
rc=0
for path in /api/gw/healthz /api/ops/healthz; do
  status=
  for _ in $(seq 20); do
    status=$($DC exec -T web wget -q -S -O /dev/null "http://127.0.0.1$path" 2>&1 \
      | sed -n 's|^ *HTTP/[0-9.]* \([0-9]*\).*|\1|p' | tail -n 1) || true
    [ "$status" = 200 ] && break
    sleep 1
  done
  if [ "$status" = 200 ]; then
    echo "ok   $path -> HTTP 200"
  else
    echo "FAIL $path -> HTTP ${status:-no response} (expected 200)"
    rc=1
  fi
done
exit $rc
