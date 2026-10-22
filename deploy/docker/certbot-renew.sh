#!/bin/sh
set -eu
child=
trap '[ -z "$child" ] || kill -TERM "$child" 2>/dev/null; exit 0' TERM INT
while :; do
    delay=1800
    certbot renew --quiet --no-random-sleep-on-renew &
    child=$!
    if wait "$child"; then
        touch /tmp/renew-ok
        delay=21600
    else
        echo 'Certificate renewal check failed; retry in 30 minutes.' >&2
    fi
    sleep "$delay" &
    child=$!
    wait "$child" || true
done
