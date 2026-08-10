#!/usr/bin/env bash
# Build a self-contained tarball for deploying Bootsmith on a host with no
# access to GitHub, GHCR, Docker Hub or PyPI.
#
# The tarball carries the fully-built image (docker save), so the target host
# never pulls anything -- not even the python base image.
#
# Usage:
#   scripts/make-offline-bundle.sh [OUTPUT_DIR]
# Writes OUTPUT_DIR/bootsmith-offline-<version>.tar.gz (default OUTPUT_DIR: ./dist)

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

OUT_DIR="${1:-dist}"
IMAGE="bootsmith:latest"
VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

BUNDLE="bootsmith-offline"
STAGE_DIR="$STAGE/$BUNDLE"
mkdir -p "$STAGE_DIR"

# --platform is not optional: the target hosts are x86_64, and a bundle built
# on an Apple Silicon Mac would otherwise ship an arm64 image that exits 255
# in a restart loop there. Override PLATFORM only for a non-amd64 target.
PLATFORM="${PLATFORM:-linux/amd64}"

echo "[bundle] building $IMAGE for $PLATFORM"
docker build --platform "$PLATFORM" -t "$IMAGE" .

echo "[bundle] saving image (this takes a moment)"
docker save "$IMAGE" | gzip -9 >"$STAGE_DIR/bootsmith-image.tar.gz"

echo "[bundle] copying profiles"
cp -r profiles "$STAGE_DIR/profiles"
# Strip editor/tmp leftovers so the target starts clean.
find "$STAGE_DIR/profiles" -name '*.json.tmp' -delete

# Compose file for the offline host: no `build:` stanza, because the target
# has no source tree and no way to pull the base image.
cat >"$STAGE_DIR/docker-compose.yml" <<'COMPOSE'
services:
  bootsmith:
    image: bootsmith:latest
    container_name: bootsmith
    restart: unless-stopped

    # Run as the host user so profile JSON written by the app stays owned by
    # you on the host, not root. start.sh sets these from `id -u` / `id -g`.
    user: "${BOOTSMITH_UID:-1000}:${BOOTSMITH_GID:-1000}"

    ports:
      - "${BOOTSMITH_PORT:-8080}:8080"

    volumes:
      # Configs live on the host and are editable there while the container
      # runs. Do not bake profiles into the image.
      - ./profiles:/data/profiles

    environment:
      BOOTSMITH_PROFILES_DIR: /data/profiles

    # Bootsmith only makes *outbound* telnet connections to WTI console
    # servers, so default bridge networking is fine. If a WTI sits on a
    # network the bridge can't reach, comment out `ports:` above and use:
    #   network_mode: host
COMPOSE

cat >"$STAGE_DIR/start.sh" <<'START'
#!/usr/bin/env bash
# Start Bootsmith from the offline bundle. Loads the bundled image on first
# run, then brings the container up with ./profiles bind-mounted.
#
# Usage:
#   ./start.sh            # load image if needed, start detached
#   ./start.sh logs       # follow logs
#   ./start.sh down       # stop and remove
#   ./start.sh load       # (re)load the bundled image only
#
# BOOTSMITH_PORT (default 8080) sets the host port.

set -euo pipefail
cd "$(dirname "$0")"

IMAGE="bootsmith:latest"

export BOOTSMITH_UID="$(id -u)"
export BOOTSMITH_GID="$(id -g)"
export BOOTSMITH_PORT="${BOOTSMITH_PORT:-8080}"

if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    echo "[start.sh] neither 'docker compose' nor 'docker-compose' found." >&2
    exit 1
fi

load_image() {
    echo "[start.sh] loading $IMAGE from bootsmith-image.tar.gz"
    docker load -i bootsmith-image.tar.gz
}

case "${1:-up}" in
load)
    load_image
    ;;
up)
    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        load_image
    fi
    "${COMPOSE[@]}" up -d
    echo "[start.sh] bootsmith listening on http://0.0.0.0:${BOOTSMITH_PORT}/"
    echo "[start.sh] profiles bind-mounted from $(pwd)/profiles"
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
START
chmod +x "$STAGE_DIR/start.sh"

cat >"$STAGE_DIR/README.md" <<README
# Bootsmith — offline deployment bundle

Built from commit \`$VERSION\`. Everything needed to run is in this directory;
the target host needs **Docker only** — no GitHub, GHCR, Docker Hub or PyPI access.

## Install

\`\`\`sh
tar xzf bootsmith-offline-$VERSION.tar.gz
cd bootsmith-offline
./start.sh
\`\`\`

Then open http://<host>:8080/.

\`start.sh\` loads the bundled image on first run (\`docker load\`), then starts
the container detached.

\`\`\`sh
./start.sh logs     # follow logs
./start.sh down     # stop and remove
BOOTSMITH_PORT=9000 ./start.sh    # different host port
\`\`\`

## Configs

Profiles live in \`./profiles/*.json\` next to this README and are bind-mounted
into the container. Edit them on the host at any time — the app reads them on
each request, so no restart or rebuild is needed. Profiles the app writes are
owned by the user who ran \`start.sh\`, not root.

## Contents

| File | Purpose |
| --- | --- |
| \`bootsmith-image.tar.gz\` | The full container image (\`docker load\`-able) |
| \`docker-compose.yml\` | Service definition — bind mount, port, uid |
| \`start.sh\` | Load + up/logs/down wrapper |
| \`profiles/\` | Target profile JSON, bind-mounted into the container |

## Upgrading

Build a fresh bundle on a connected machine with
\`scripts/make-offline-bundle.sh\`, copy it over, then:

\`\`\`sh
./start.sh down
# unpack the new bundle over this directory, keeping your profiles/
./start.sh load && ./start.sh up
\`\`\`
README

mkdir -p "$OUT_DIR"
TARBALL="$REPO_ROOT/$OUT_DIR/bootsmith-offline-$VERSION.tar.gz"
tar czf "$TARBALL" -C "$STAGE" "$BUNDLE"

echo "[bundle] wrote $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "[bundle] copy to the target host, then: tar xzf $(basename "$TARBALL") && cd $BUNDLE && ./start.sh"
