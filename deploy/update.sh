#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
exec 9>/run/model-test-update.lock
flock -n 9 || { echo 'Another update is running.' >&2; exit 1; }
base=/opt/model-test
repo=$base/repository
config=/etc/model-test
docker compose version >/dev/null
git -C "$repo" fetch --prune origin
revision=$(git -C "$repo" rev-parse --verify "${1:-origin/main}^{commit}")
git -C "$repo" cat-file -e "$revision:compose.yaml" || { echo 'Choose a Docker deployment revision.' >&2; exit 1; }
self_update=$(mktemp /tmp/model-test-update.XXXXXX)
git -C "$repo" show "$revision:deploy/update.sh" > "$self_update"
if ! cmp -s "$self_update" "$0"; then
    install -m 755 "$self_update" /usr/local/bin/model-test-update
    rm -f "$self_update"
    flock -u 9
    exec 9>&-
    exec /usr/local/bin/model-test-update "$revision"
fi
rm -f "$self_update"
release=$base/releases/$revision
previous=$(readlink -f "$base/current" || true)
if [[ $previous == "$release" && -f $config/docker.env ]]; then
    echo "Already running $revision. Use model-test-ctl for service operations."
    exit 0
fi
mkdir -p "$release"
git -C "$repo" archive "$revision" | tar -x -C "$release"
chmod -R go+rX "$release"

runtime=$(mktemp /tmp/model-test-docker.XXXXXX)
trap 'rm -f "$runtime"' EXIT
printf 'MT_VERSION=%s\nAPP_UID=%s\nAPP_GID=%s\n' "$revision" \
    "$(stat -c %u /var/lib/model-test)" "$(stat -c %g /var/lib/model-test)" > "$runtime"
compose() { docker compose --env-file "$runtime" -f "$release/compose.yaml" "$@"; }
compose config --quiet
compose pull --quiet proxy certbot
compose run --rm --no-deps --entrypoint nginx proxy -t
compose build app
image=local/model-test:$revision
# No production database or credentials are mounted in the offline test runner.
docker run --rm --entrypoint /bin/sh --tmpfs /tmp:rw,size=256m,mode=1777 \
    -e MT_RESULT_ROOT=/tmp/results -e TIKTOKEN_CACHE_DIR=/tmp/tiktoken \
    -v /var/lib/model-test/acceptance-fixtures:/app/test-scripts/standalone-acceptance/fixtures:ro \
    "$image" -ec 'python -m unittest -q test_suites test_web_auth test_folder_uploads; cd /app/test-scripts/llm-benchmark; python -m unittest discover -q -s tests; test -r /app/docs/models/模版.html; test -r /app/docs/models/测试结果模板.xlsx'

marker=$config/maintenance
backup=/var/backups/model-test/$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:8}
legacy=0
[[ -f $config/docker.env ]] || legacy=1
auth_gid=$(stat -c %g "$config/htpasswd")
stopped=0
switched=0
finish() {
    result=$?
    trap - EXIT
    if [[ $result != 0 && $stopped == 1 ]]; then
        compose down --timeout 30 || true
        if [[ $switched == 1 && -d $backup/data ]]; then
            rsync -a --delete "$backup/data/" /var/lib/model-test/data/
            chown -R "$(stat -c %u /var/lib/model-test):$(stat -c %g /var/lib/model-test)" /var/lib/model-test/data
        fi
        [[ -z $previous ]] || ln -sfn "$previous" "$base/current"
        chgrp "$auth_gid" "$config/htpasswd"
        if [[ $legacy == 1 ]]; then
            rm -f "$config/docker.env"
            if [[ -f $backup/renewal.conf ]]; then
                cp "$backup/renewal.conf" /etc/letsencrypt/renewal/model-test.conf
            fi
            if [[ -n $previous ]]; then
                systemctl start model-test nginx model-test-certbot.timer || true
            fi
        elif [[ -n $previous && -f $config/docker.env ]]; then
            [[ ! -f $backup/docker.env ]] || cp "$backup/docker.env" "$config/docker.env"
            docker compose --env-file "$config/docker.env" -f "$previous/compose.yaml" up -d --wait --wait-timeout 120 || true
        fi
        echo "Deployment failed; restored previous deployment. Backup: $backup" >&2
    fi
    rm -f "$marker" "$runtime"
    exit "$result"
}
trap finish EXIT
touch "$marker"
if curl -fsS http://127.0.0.1:8032/api/health >/dev/null 2>&1; then
    for attempt in {1..30}; do
        state=$(curl -fsS http://127.0.0.1:8032/api/deployment-status)
        if ! python3 -c 'import json,sys; s=json.loads(sys.argv[1]); sys.exit(s["active_jobs"] != 0)' "$state"; then
            echo 'Active tests found. Retry after they finish.' >&2
            exit 1
        fi
        if python3 -c 'import json,sys; s=json.loads(sys.argv[1]); sys.exit(s["mutating_requests"] != 0)' "$state"; then
            break
        fi
        [[ $attempt != 30 ]] || { echo 'Requests still active.' >&2; exit 1; }
        sleep 1
    done
elif [[ -n $previous ]]; then
    echo 'Current backend is unavailable; inspect it before deploying.' >&2
    exit 1
fi
if [[ $legacy == 1 && -n $previous ]]; then
    systemctl stop model-test
else
    compose stop app
fi
stopped=1
mkdir -p "$backup"
python3 "$release/deploy/backup.py" /var/lib/model-test/data "$backup/data"
cp "$config/platform.env" "$backup/platform.env"
[[ ! -f $config/docker.env ]] || cp "$config/docker.env" "$backup/docker.env"
printf '%s\n' "$previous" > "$backup/previous-release"
if [[ $legacy == 1 ]]; then
    # Retire the saved host reload hook: the proxy container watches cert changes.
    cp /etc/letsencrypt/renewal/model-test.conf "$backup/renewal.conf"
    sed -i '/^renew_hook = systemctl reload nginx$/d' /etc/letsencrypt/renewal/model-test.conf
    if [[ -n $previous ]]; then
        systemctl stop model-test-certbot.timer model-test-certbot.service nginx
    fi
fi
# Official nginx:alpine workers use GID 101; retain the protected hash-file mode.
chown root:101 "$config/htpasswd"
chmod 640 "$config/htpasswd"
install -m 600 "$runtime" "$config/docker.env"
ln -sfn "$release" "$base/current"
switched=1
compose up -d --wait --wait-timeout 180 --remove-orphans
curl -fsS http://127.0.0.1:8032/api/health >/dev/null
# Exercise TLS, browser sessions, and Basic checks without logging credentials.
python3 - <<'PY'
import base64, http.cookiejar, json, urllib.error, urllib.request
from pathlib import Path
env = dict(line.split("=",1) for line in Path("/etc/model-test/platform.env").read_text().splitlines() if "=" in line and not line.startswith("#"))
login = dict(line.split(": ",1) for line in Path("/root/model-test-login.txt").read_text().splitlines() if ": " in line)
base_url = env["MT_PUBLIC_URL"].rstrip("/")
url = base_url + "/api/health"
token = base64.b64encode((login["Username"]+":"+login["Password"]).encode()).decode()
req = urllib.request.Request(url, headers={"Authorization":"Basic "+token})
with urllib.request.urlopen(req, timeout=15) as r:
    assert r.status == 200
try:
    urllib.request.urlopen(url, timeout=15)
except urllib.error.HTTPError as error:
    assert error.code == 401 and "WWW-Authenticate" not in error.headers
else:
    raise AssertionError("Unauthenticated API access was accepted")
with urllib.request.urlopen(base_url + "/", timeout=15) as response:
    assert response.status == 200 and "/login" in response.url
browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
request = urllib.request.Request(base_url + "/auth/login", data=json.dumps({"username": login["Username"], "password": login["Password"]}).encode(), headers={"Content-Type": "application/json", "Origin": base_url})
with browser.open(request, timeout=15) as response:
    assert response.status == 200 and "HttpOnly" in response.headers.get("Set-Cookie", "")
with browser.open(url, timeout=15) as response:
    assert response.status == 200
PY
if [[ $legacy == 1 && -n $previous ]]; then
    systemctl disable model-test nginx model-test-certbot.timer
fi
install -m 755 "$release/deploy/ctl.sh" /usr/local/bin/model-test-ctl

# Retain only our current/previous releases and images, and five data snapshots.
while read -r directory; do
    [[ $directory == "$release" || $directory == "$previous" ]] || rm -rf -- "$directory"
done < <(find "$base/releases" -mindepth 1 -maxdepth 1 -type d)
while read -r tag; do
    [[ $tag == "$revision" || $tag == "${previous##*/}" || $tag == '<none>' ]] || docker image rm "local/model-test:$tag" || true
done < <(docker images local/model-test --format '{{.Tag}}')
docker image prune -f --filter 'label=org.opencontainers.image.title=model-test-platform' >/dev/null
keep=0
while read -r directory; do
    keep=$((keep + 1))
    (( keep <= 5 )) || rm -rf -- "$directory"
done < <(find /var/backups/model-test -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
echo "Docker deployment healthy: $revision. Backup: $backup"
