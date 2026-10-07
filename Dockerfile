# Bootsmith container image.
#
# Python is pinned to 3.12 on purpose: transport.py falls back to the stdlib
# telnetlib, which was removed in 3.13. 3.12 is the newest interpreter that
# still ships it.
FROM python:3.12-slim

# HOME=/tmp because the container is meant to run as the *host* user's
# uid/gid (see docker-compose.yml) so files written into the bind-mounted
# profiles directory stay readable and editable on the host. That uid has
# no /etc/passwd entry and therefore no home directory of its own.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    BOOTSMITH_PROFILES_DIR=/data/profiles

WORKDIR /app

# Dependencies first so edits to src/ don't invalidate the pip layer.
# packaging is listed explicitly: gunicorn 26.2.0 stopped depending on it, but
# its gevent worker still imports it, so without it the container exits at start.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "flask>=3.0" "flask-sock>=0.7" "gunicorn>=21.0" "gevent>=23.0" packaging

COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p /data/profiles \
    && chmod 777 /data/profiles

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=4)"]

# gevent worker is required -- sync workers serialize the SSE stream against
# /params/push. See src/bootsmith/wsgi.py.
CMD ["gunicorn", \
    "-k", "gevent", \
    "-w", "1", \
    "--timeout", "120", \
    "-b", "0.0.0.0:8080", \
    "bootsmith.wsgi:app"]
