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
# The server runs as root only so it can start each app under test as the unprivileged
# `sandbox` user (SANDBOX_USER): that user can't read the server's /proc/<pid>/environ (its
# secrets), its database or the locked venv cache. See SECURITY.md.
RUN pip install . uv && useradd --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin --uid 10002 sandbox

ENV SANDBOX_USER=sandbox
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"
# `driftless serve` reads the port from PORT.
CMD ["driftless", "serve", "--host", "0.0.0.0"]
