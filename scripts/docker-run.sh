#!/usr/bin/env bash
# Build and run Bootsmith in a container, with ./profiles bind-mounted so
# the configs stay on the host where you can edit them directly.
#
# Usage:
#   scripts/docker-run.sh              # build if needed, start detached
#   scripts/docker-run.sh up           # same
#   scripts/docker-run.sh build        # rebuild the image only
#   scripts/docker-run.sh logs         # follow logs
#   scripts/docker-run.sh down         # stop and remove
#
# BOOTSMITH_PORT (default 8080) sets the host port.

set -euo pipefail

cd "$(dirname "$0")/.."

# Run the container as the invoking user so profiles written by the app are
# owned by you on the host rather than by root.
export BOOTSMITH_UID="$(id -u)"
export BOOTSMITH_GID="$(id -g)"
export BOOTSMITH_PORT="${BOOTSMITH_PORT:-8080}"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "[docker-run.sh] neither 'docker compose' nor 'docker-compose' found." >&2
    exit 1
fi

mkdir -p profiles

case "${1:-up}" in
build)
    "${COMPOSE[@]}" build
    ;;
up)
    "${COMPOSE[@]}" up -d --build
    echo "[docker-run.sh] bootsmith listening on http://0.0.0.0:${BOOTSMITH_PORT}/"
    echo "[docker-run.sh] profiles bind-mounted from $(pwd)/profiles"
    ;;
logs)
    "${COMPOSE[@]}" logs -f
    ;;
down)
    "${COMPOSE[@]}" down
    ;;
*)
    "${COMPOSE[@]}" "$@"
    ;;
esac
