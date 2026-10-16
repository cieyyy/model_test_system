#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
exec 9>/run/model-test-update.lock
flock -n 9 || { echo 'Another update is running.' >&2; exit 1; }
base=/opt/model-test
repo=$base/repository
git -C "$repo" fetch --prune origin
revision=$(git -C "$repo" rev-parse --verify "${1:-origin/main}^{commit}")
release=$base/releases/$revision
previous=$(readlink -f "$base/current" || true)
if [[ $previous == "$release" ]]; then
    echo "Already running $revision"
    exit 0
fi
if [[ ! -f "$release/.ready" ]]; then
    mkdir -p "$release"
    git -C "$repo" archive "$revision" | tar -x -C "$release"
    python3 -m venv "$release/.venv"
    "$release/.venv/bin/pip" install --disable-pip-version-check -q -r "$release/backend/requirements.lock"
    ln -sfn /var/lib/model-test/acceptance-fixtures "$release/test-scripts/standalone-acceptance/fixtures"
    (cd "$release/backend" && "$release/.venv/bin/python" -m unittest -q test_suites)
    (cd "$release/test-scripts/llm-benchmark" && "$release/.venv/bin/python" -m unittest discover -q -s tests)
    touch "$release/.ready"
    chmod -R go+rX "$release"
fi
marker=/etc/model-test/maintenance
backup=/var/backups/model-test/$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:8}
switched=0
stopped=0
finish() {
    result=$?
    trap - EXIT
    if [[ $result != 0 && $stopped == 1 && -n $previous && -d $previous ]]; then
        systemctl stop model-test || true
        if [[ $switched == 1 && -d $backup/data ]]; then
            rsync -a --delete "$backup/data/" /var/lib/model-test/data/
            chown -R model-test:model-test /var/lib/model-test/data
        fi
        ln -sfn "$previous" "$base/current"
        systemctl start model-test || true
        echo "Update failed; restored $previous. Backup: $backup" >&2
    fi
    [[ ! -e $marker ]] || unlink "$marker"
    exit "$result"
}
trap finish EXIT
touch "$marker"
if systemctl is-active --quiet model-test; then
    for attempt in {1..30}; do
        state=$(curl --fail --silent http://127.0.0.1:8032/api/deployment-status)
        if ! python3 -c 'import json,sys; s=json.loads(sys.argv[1]); sys.exit(0 if s["active_jobs"] == 0 else 1)' "$state"; then
            echo 'Active tests found. Retry after they finish.' >&2
            exit 1
        fi
        if python3 -c 'import json,sys; s=json.loads(sys.argv[1]); sys.exit(0 if s["mutating_requests"] == 0 else 1)' "$state"; then
            break
        fi
        [[ $attempt != 30 ]] || { echo 'Requests still active.' >&2; exit 1; }
        sleep 1
    done
fi
systemctl stop model-test
stopped=1
mkdir -p "$backup"
python3 "$release/deploy/backup.py" /var/lib/model-test/data "$backup/data"
cp /etc/model-test/platform.env "$backup/platform.env"
printf '%s\n' "$previous" > "$backup/previous-release"
ln -sfn "$release" "$base/current"
switched=1
systemctl start model-test
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8032/api/health >/dev/null; then
        echo "Running $revision. Backup: $backup"
        exit 0
    fi
    sleep 1
done
echo 'Health check failed.' >&2
exit 1
