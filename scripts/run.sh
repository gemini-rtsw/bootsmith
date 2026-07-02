#!/usr/bin/env bash
# Run Bootsmith under gunicorn + gevent so the SSE stream and the
# /params/push handler can actually run concurrently. Werkzeug's
# threaded=True dev server stalls SSE while long HTTP requests are
# in flight, which is why the terminal would hang mid-push.
#
# Usage:
#   scripts/run.sh [PORT] [HOST]
# Defaults to port 5050 on 0.0.0.0 (all interfaces, reachable remotely).
# Pass 127.0.0.1 as HOST to restrict to loopback.

set -e

PORT="${1:-5050}"
HOST="${2:-0.0.0.0}"
cd "$(dirname "$0")/.."

# Bootsmith needs Python 3.10+; pick the first interpreter on PATH that
# qualifies (system python3 on most boxes, python3.11 on RHEL/Rocky 8
# where the default python3 is still 3.6).
PYTHON=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[run.sh] no Python 3.10+ interpreter found on PATH." >&2
    echo "[run.sh] on RHEL/Rocky 8, install one from AppStream: sudo dnf install python3.11" >&2
    exit 1
fi

# Make sure Bootsmith's dependencies (per pyproject.toml) are installed
# for the current user, plus gunicorn/gevent to serve it.
"$PYTHON" -c "import flask, flask_sock, gunicorn, gevent" 2>/dev/null || {
    echo "[run.sh] installing bootsmith deps + gunicorn + gevent (--user) with $PYTHON"
    "$PYTHON" -m pip install --user -e . "gunicorn>=21.0" "gevent>=23.0"
}

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m gunicorn \
    -k gevent \
    -w 1 \
    --timeout 120 \
    -b "${HOST}:${PORT}" \
    bootsmith.wsgi:app
