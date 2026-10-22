#!/bin/sh
set -eu
certificate=/etc/letsencrypt/live/model-test/fullchain.pem
nginx -t
nginx -g 'daemon off;' &
server=$!
watch_cert() {
    previous=$(sha256sum "$certificate" 2>/dev/null || true)
    while sleep 60; do
        current=$(sha256sum "$certificate" 2>/dev/null || true)
        if [ -n "$current" ] && [ "$current" != "$previous" ]; then
            if nginx -t && nginx -s reload; then previous=$current; fi
        fi
    done
}
watch_cert &
watcher=$!
trap 'kill -TERM "$server" "$watcher" 2>/dev/null || true' TERM INT
result=0
wait "$server" || result=$?
kill -TERM "$watcher" 2>/dev/null || true
wait "$watcher" 2>/dev/null || true
exit "$result"
