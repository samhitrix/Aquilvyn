#!/bin/sh
# E33h Health-Ping (anti-sleep) Engine: every PING_INTERVAL seconds hit each service's readiness
# probe through the gateway (keeps DB/Redis pools, caches and the Next.js server warm).
INTERVAL="${PING_INTERVAL:-240}"
TARGETS="${PING_TARGETS:-identity portfolio market analytics advisor dashboard}"
while true; do
  for svc in $TARGETS; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://gateway:8080/health/$svc")
    echo "{\"service\":\"healthping\",\"target\":\"$svc\",\"status\":$code,\"level\":\"$( [ "$code" = 200 ] && echo info || echo warning )\"}"
  done
  curl -s -o /dev/null --max-time 10 http://gateway:8080/ || true
  sleep "$INTERVAL"
done
