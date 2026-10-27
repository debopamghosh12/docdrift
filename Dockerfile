# Driftless web app + webhook server (`driftless serve`), sized for a small host
# (e.g. Render's free tier: 512 MB, ephemeral disk). See README "Deploy your own (Render)".
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Everything written at runtime lives under /tmp: the disk is ephemeral anyway.
    DATABASE_URL=sqlite:////tmp/driftless.db \
    SANDBOX_CACHE_DIR=/tmp/driftless_cache \
    UV_LINK_MODE=copy \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY driftless ./driftless
# uv builds the venvs for the apps under test much faster than venv + pip.
# The server runs as the unprivileged `driftless` user by default: Render's containers refuse
# the setuid/setgid a user switch needs, and a server running as root there would run the app
# under test as root too. The app then runs as `driftless` with a clean environment (a note says
# so in each report). Where the host allows the switch (plain Docker), start the container with
# `--user root` and every app runs as `sandbox` (SANDBOX_USER), which can't read the server's
# /proc/<pid>/environ (its secrets), its database or the locked venv cache. See SECURITY.md.
RUN pip install . uv     && useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --uid 10002 sandbox     && useradd --create-home --shell /usr/sbin/nologin --uid 10001 driftless

ENV SANDBOX_USER=sandbox
USER driftless
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"
# `driftless serve` reads the port from PORT.
CMD ["driftless", "serve", "--host", "0.0.0.0"]
