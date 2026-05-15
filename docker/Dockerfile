# Slim Debian base — small image while still allowing apt installs.
FROM python:3.11-slim

# No .pyc files, unbuffered stdout (logs surface immediately), no pip
# wheel cache (smaller image), and skip pip's self-update check.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ``curl`` is used by docker-compose's healthcheck to hit /healthz.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install deps *before* copying the source so this expensive layer is
# cached across rebuilds when only application code changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

# Drop root.  Files in /app are copied as root and remain world-readable,
# which is what the app needs.  Running as an unprivileged user means a
# compromised process cannot mutate the image filesystem or escalate.
RUN groupadd --system app \
    && useradd --system --gid app --no-create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8000

# Single uvicorn worker is sufficient for the challenge.  Production
# deployments would typically front this with gunicorn + N workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]