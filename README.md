# Bootsmith

A small local web GUI for editing VME boot parameters on PPCBug and Tornado/VxWorks
boards over a WTI console server.

## Run

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e .
bootsmith
```

Then open http://127.0.0.1:5000/.

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
