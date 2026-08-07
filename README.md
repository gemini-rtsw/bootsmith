# Bootsmith

A small local web GUI for editing VME boot parameters on PPCBug and Tornado/VxWorks
boards over a WTI console server.

## Run (container)

This is the deployment path. Profiles are bind-mounted from `./profiles` on the
host, so you can read and edit the JSON configs normally while the container runs.

```sh
scripts/docker-run.sh          # build + start detached on port 8080
scripts/docker-run.sh logs     # follow logs
scripts/docker-run.sh down     # stop
```

Then open http://<host>:8080/.

Set `BOOTSMITH_PORT` to use a different host port:

```sh
BOOTSMITH_PORT=9000 scripts/docker-run.sh
```

The container runs as your uid/gid (the wrapper passes `id -u`/`id -g` through
to compose), so profiles the app writes stay owned by you on the host rather
than by root. Plain `docker compose up -d --build` works too, but set
`BOOTSMITH_UID`/`BOOTSMITH_GID` yourself or you'll get uid 1000.

Bootsmith only makes outbound telnet connections to the WTI console servers, so
default bridge networking is fine. If a WTI is unreachable from the bridge, swap
`ports:` for `network_mode: host` in `docker-compose.yml`.

The image pins Python 3.12: the transport layer falls back to the stdlib
`telnetlib`, which was removed in 3.13.

## Deploy from GHCR

GitHub Actions builds the image on every push to `main` and publishes it to
`ghcr.io/gemini-rtsw/bootsmith` (`.github/workflows/build-image.yml`). Tags:
`latest` on `main`, `sha-<short>` per commit, and semver tags for `v*` releases.

On the target host you only need `docker-compose.deploy.yml`, `scripts/deploy.sh`
and a `profiles/` directory — no source tree, no local build:

```sh
./deploy.sh            # pull latest and (re)start
./deploy.sh logs       # follow logs
./deploy.sh down       # stop and remove
```

Set `BOOTSMITH_PORT` if 8080 is taken (it is on `mkorpmfs-lv1`, where `rpm-repo`
already holds it):

```sh
BOOTSMITH_PORT=8081 ./deploy.sh
```

Pin a specific build instead of tracking `latest`:

```sh
BOOTSMITH_IMAGE=ghcr.io/gemini-rtsw/bootsmith:sha-1a2b3c4 ./deploy.sh
```

If the GHCR package is private, authenticate once on the target with a PAT that
has `read:packages`: `docker login ghcr.io -u <user>`.

## Deploy to an air-gapped host

For a target with no GitHub / GHCR / Docker Hub / PyPI access, build a bundle on
a connected machine:

```sh
scripts/make-offline-bundle.sh
# -> dist/bootsmith-offline-<commit>.tar.gz  (~48 MB)
```

The tarball carries the fully-built image (`docker save`), the compose file, the
profiles, and a `start.sh`. The target host needs Docker and nothing else — it
never pulls anything, not even the Python base image. Copy it over, then:

```sh
tar xzf bootsmith-offline-<commit>.tar.gz
cd bootsmith-offline
./start.sh
```

Profiles sit in `bootsmith-offline/profiles/` on the target and are bind-mounted
in, same as the source-tree deploy. Full instructions ship inside the bundle.

## Run (from source)

Bootsmith needs Python 3.10 or newer and Flask. No virtualenv required.

```sh
# install Flask once (user-local, no root)
python3 -m pip install --user flask

# run the app from the source tree
PYTHONPATH=src python3 -m bootsmith --port 8080
```

Then open http://127.0.0.1:8080/.

If your default `python3` is older than 3.10, use the explicit version, e.g.:

```sh
python3.11 -m pip install --user flask
PYTHONPATH=src python3.11 -m bootsmith --port 8080
```

## How it works

VME boards typically have a single serial line that is either bound to the running
EPICS IOC or sitting at the boot loader. Bootsmith does **not** power-cycle the board.

1. Pick (or create) a target profile (e.g. `MCS`).
2. Bootsmith connects to the WTI port and shows live serial output.
3. You reboot the board however you normally do.
4. Bootsmith watches for the boot banner and spams an abort character during the countdown.
5. On catch: it auto-detects the loader, reads current parameters, lets you edit, writes back, and verifies.
6. On miss: it tells you, and you can reboot again.

## Profiles

Stored as one JSON file per target, `<name>.json`. The directory is resolved in
this order:

1. `BOOTSMITH_PROFILES_DIR` (or `--profiles-dir`) — the container sets this to
   `/data/profiles`, which is the bind mount of `./profiles` on the host.
2. `./profiles/` if it exists in the working directory.
3. `~/.bootsmith/profiles/`.
