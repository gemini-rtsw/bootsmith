"""WSGI entry point for gunicorn.

Usage:
    gunicorn -k gevent -w 1 -b 127.0.0.1:5050 bootsmith.wsgi:app

The gevent worker is REQUIRED -- sync workers serialize streaming
responses against other HTTP requests on the same connection pool,
which manifests as the SSE stream stalling while /params/push runs.

Profile directory resolution is the same as `python -m bootsmith`:
honors BOOTSMITH_PROFILES_DIR, otherwise ./profiles if it exists,
otherwise ~/.bootsmith/profiles.
"""
from __future__ import annotations

# gevent monkey-patch BEFORE anything else imports socket/threading.
# Without this, telnetlib's blocking socket.recv() pins a worker green-
# thread and the SSE stream can't make progress while a push runs.
from gevent import monkey  # noqa: E402

monkey.patch_all()

import os  # noqa: E402
from pathlib import Path  # noqa: E402

from . import profiles as profiles_mod  # noqa: E402
from .app import create_app  # noqa: E402


def _resolve_profile_dir() -> Path:
    env = os.environ.get("BOOTSMITH_PROFILES_DIR")
    if env:
        return Path(env).expanduser()
    if Path("profiles").is_dir():
        return Path("profiles").resolve()
    return Path("~/.bootsmith/profiles").expanduser()


_chosen = _resolve_profile_dir()
profiles_mod.set_profile_dir(_chosen)
print(f"[bootsmith] profiles directory: {_chosen}", flush=True)

app = create_app()
