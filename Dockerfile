FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="model-test-platform" \
      org.opencontainers.image.source="https://github.com/cieyyy/model_test_system"
ARG APP_UID=999
ARG APP_GID=988
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    MT_DATA_DIR=/var/lib/model-test/data \
    MT_MEDIA_DIR=/var/lib/model-test/media \
    MT_RESULT_ROOT=/var/lib/model-test/results \
    TIKTOKEN_CACHE_DIR=/var/lib/model-test/tiktoken \
    MT_SCRIPTS_ROOT=/app/test-scripts TZ=Asia/Shanghai
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl procps tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "$APP_GID" model-test \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --no-create-home \
      --home-dir /var/lib/model-test --shell /usr/sbin/nologin model-test
WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN pip install --disable-pip-version-check -r /app/backend/requirements.lock
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY test-scripts/ /app/test-scripts/
COPY docs/models/ /app/docs/models/
RUN mkdir -p /app/test-scripts/standalone-acceptance/fixtures /var/lib/model-test \
    && chown model-test:model-test /var/lib/model-test
USER model-test
WORKDIR /app/backend
EXPOSE 8032
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8032/api/health', timeout=3).read()"
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8032", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
