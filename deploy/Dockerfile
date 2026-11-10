# Global Intelligence & Wealth Brief - server image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=America/Toronto
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 intel

WORKDIR /app
COPY intel/requirements.txt deploy/requirements-server.txt ./
RUN pip install -r requirements.txt -r requirements-server.txt

COPY intel/ ./intel/
COPY deploy/schema.sql ./deploy/schema.sql
RUN mkdir -p /data && chown -R intel:intel /data /app
USER intel

ENV INTEL_OUTDIR=/data/site
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${INTEL_HEALTH_PORT:-8080}/healthz || exit 1

CMD ["python", "-m", "intel", "serve"]
