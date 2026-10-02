FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    USAGE_DB_PATH=/app/data/usage.sqlite3

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 speech \
    && useradd --uid 10001 --gid speech --no-create-home --home-dir /app speech \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 speech_proxy ./speech_proxy

USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).read()"]
CMD ["uvicorn", "speech_proxy.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--no-access-log"]
