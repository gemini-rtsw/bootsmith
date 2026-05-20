# Bootsmith

A small local web GUI for editing VME boot parameters on PPCBug and Tornado/VxWorks
boards over a WTI console server.

## Run

Bootsmith needs Python 3.10 or newer and Flask. No virtualenv required.

```sh
# install Flask once (user-local, no root)
python3 -m pip install --user flask

# run the app from the source tree
PYTHONPATH=src python3 -m bootsmith --port 5050
```

Then open http://127.0.0.1:5050/.

If your default `python3` is older than 3.10, use the explicit version, e.g.:

```sh
python3.11 -m pip install --user flask
PYTHONPATH=src python3.11 -m bootsmith --port 5050
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

Stored as JSON in `~/.bootsmith/profiles/<name>.json`.
