#!/usr/bin/env bash
set -Eeuo pipefail
base=/opt/model-test/current
[[ -f /etc/model-test/docker.env ]] || { echo 'Run the Docker installer first.' >&2; exit 1; }
exec docker compose --env-file /etc/model-test/docker.env -f "$base/compose.yaml" "$@"
