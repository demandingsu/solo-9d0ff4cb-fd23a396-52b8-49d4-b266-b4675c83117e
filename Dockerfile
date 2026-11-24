FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/inspection.db \
    REQUIRE_SECRET_KEY=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY inspection ./inspection
COPY templates ./templates
COPY static ./static

RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# Writes are serialized by SQLite BEGIN IMMEDIATE and the split copy phase is
# guarded by a process-wide flock.  One worker with several threads is enough
# for this workload and avoids mixing in-memory CSRF/session state across
# processes if the worker count is ever raised without a stable SECRET_KEY.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "inspection.web:create_app()"]
