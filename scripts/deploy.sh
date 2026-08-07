#!/usr/bin/env bash
# Deploy Bootsmith on a target host by pulling the image GitHub Actions
# published to GHCR. No source tree or local build required -- this script
# and docker-compose.deploy.yml (plus ./profiles) are all you need to copy.
#
# Usage:
#   ./deploy.sh              # pull latest and (re)start
#   ./deploy.sh up           # same
#   ./deploy.sh pull         # pull only
#   ./deploy.sh logs         # follow logs
#   ./deploy.sh down         # stop and remove
#
# Environment:
#   BOOTSMITH_PORT   host port (default 8080)
#   BOOTSMITH_IMAGE  image ref (default ghcr.io/gemini-rtsw/bootsmith:latest)

set -euo pipefail
cd "$(dirname "$0")"

# Works whether this script sits in scripts/ of a checkout or next to the
# compose file in a copied-over deploy directory.
COMPOSE_FILE="docker-compose.deploy.yml"
if [ ! -f "$COMPOSE_FILE" ] && [ -f "../$COMPOSE_FILE" ]; then
    cd ..
fi
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "[deploy.sh] $COMPOSE_FILE not found next to this script or one level up." >&2
    exit 1
fi

# Run the container as the invoking user so profiles written by the app are
# owned by you on the host rather than by root. Do NOT run this under sudo.
export BOOTSMITH_UID="$(id -u)"
export BOOTSMITH_GID="$(id -g)"
export BOOTSMITH_PORT="${BOOTSMITH_PORT:-8080}"
export BOOTSMITH_IMAGE="${BOOTSMITH_IMAGE:-ghcr.io/gemini-rtsw/bootsmith:latest}"

if [ "$BOOTSMITH_UID" = "0" ]; then
    echo "[deploy.sh] warning: running as root -- profiles written by the app" >&2
    echo "[deploy.sh] will be root-owned on the host. Prefer the docker group." >&2
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose -f "$COMPOSE_FILE")
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose -f "$COMPOSE_FILE")
else
    echo "[deploy.sh] neither 'docker compose' nor 'docker-compose' found." >&2
    exit 1
fi

mkdir -p profiles

case "${1:-up}" in
pull)
    "${COMPOSE[@]}" pull
    ;;
up)
    # A failed pull is only fatal if we have nothing to fall back on --
    # otherwise a GHCR blip would block an otherwise fine restart.
    if ! "${COMPOSE[@]}" pull; then
        if docker image inspect "$BOOTSMITH_IMAGE" >/dev/null 2>&1; then
            echo "[deploy.sh] pull failed; starting the image already on this host." >&2
        else
            echo "[deploy.sh] pull failed and $BOOTSMITH_IMAGE is not present locally." >&2
            echo "[deploy.sh] if the package is private: docker login ghcr.io -u <user>" >&2
            exit 1
        fi
    fi
    "${COMPOSE[@]}" up -d
    echo "[deploy.sh] bootsmith listening on http://0.0.0.0:${BOOTSMITH_PORT}/"
    echo "[deploy.sh] image:    ${BOOTSMITH_IMAGE}"
    echo "[deploy.sh] profiles: $(pwd)/profiles"
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
