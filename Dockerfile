# Build the frontend in the image; no host node_modules or compiled output is copied.
FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY src/frontend/package.json src/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    HF_HOME=/tmp/huggingface
WORKDIR /app/backend
COPY src/backend/requirements.voice.lock ./requirements.voice.lock
# Azure Speech native SDK and ONNX/Silero runtime dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 libasound2 libgomp1 libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --require-hashes -r requirements.voice.lock \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app app \
    && mkdir -p /app/backend/data \
    && chown 10001:10001 /app/backend/data
COPY --chown=10001:10001 src/backend/app/ ./app/
COPY --from=frontend --chown=10001:10001 /build/dist/ /app/frontend/dist/
USER 10001:10001
EXPOSE 8000 8081
# A single process owns the in-memory coordinator and the local SQLite files.
# Port 8000 is reachable only on the private Compose network.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
