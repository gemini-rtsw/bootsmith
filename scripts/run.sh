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

# Make sure gunicorn + gevent are installed for the current user.
python3 -c "import gunicorn, gevent" 2>/dev/null || {
    echo "[run.sh] installing gunicorn + gevent (--user)"
    python3 -m pip install --user "gunicorn>=21.0" "gevent>=23.0"
}

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m gunicorn \
    -k gevent \
    -w 1 \
    --timeout 120 \
    -b "${HOST}:${PORT}" \
    bootsmith.wsgi:app
