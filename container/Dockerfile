# ---- Builder stage: compile native wheels ----
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS builder

WORKDIR /build

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip "setuptools>=78.1.1"

COPY requirements-torch.txt requirements.lock ./

# CPU-only PyTorch, version pinned in requirements-torch.txt. --no-deps: its
# runtime dependencies come hashed from requirements.lock, and the CUDA stack
# of the PyPI wheel (~4.3 GB of nvidia/cuda/triton libraries) is never pulled.
RUN pip install --no-cache-dir --prefix=/install --no-deps --ignore-installed \
    --index-url https://download.pytorch.org/whl/cpu \
    -r requirements-torch.txt

# Everything else from the fully pinned, hash-checked lock (generated from
# requirements.txt by `python scripts/ci.py --update-lock`). --ignore-installed
# keeps a package that the builder's own interpreter already has (setuptools)
# from being skipped instead of landing in /install.
RUN pip install --no-cache-dir --prefix=/install --require-hashes --no-deps \
    --ignore-installed -r requirements.lock

# ---- Final stage: slim runtime ----
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

WORKDIR /app

# Copy installed packages from builder (setuptools comes pinned from the lock)
COPY --from=builder /install /usr/local

# Fail the build when the separately installed torch and the lock disagree
RUN pip check

# Copy application code
COPY app/ ./app/

# Create non-root user and data directory
RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data/chromadb /data/hf_cache \
    && chown -R app:app /data /app
# /data/chromadb now holds the sqlite-vec entity-vector DB + cache.db
# (kept under the legacy name to avoid env-var churn).

# Entrypoint: fix permissions on mounted volumes then drop to non-root
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Ensure ca-certificates is present for HTTPS. The upgrade pulls Debian
# security fixes newer than the pinned base image digest (Trivy gates on them).
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Default environment variables
ENV CONTAINER_HOST=0.0.0.0
ENV CONTAINER_PORT=8080
ENV LOG_LEVEL=INFO
ENV CHROMADB_PERSIST_DIR=/data/chromadb
ENV SQLITE_DB_PATH=/data/agent_assist.db
ENV HF_HOME=/data/hf_cache

# Pre-download the default local embedding model so first start does not
# block on a ~1.1 GB download and HF_HUB_OFFLINE=1 works out of the box.
# Keep in sync with DEFAULT_LOCAL_EMBEDDING_MODEL in app/defaults.py.
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-base
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}')"

# Pre-download the speaker-identification model (CAM++ English VoxCeleb,
# 16 kHz) with a checksum check. Keep in sync with MODEL_URL / MODEL_SHA256
# in app/voice/model.py; the app falls back to a one-time download into
# /data/models/speaker when this file is missing.
ARG SPEAKER_MODEL_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx
ARG SPEAKER_MODEL_SHA256=357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b
RUN mkdir -p /opt/models/speaker \
    && python -c "import hashlib, sys, urllib.request; \
p = '/opt/models/speaker/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx'; \
urllib.request.urlretrieve('${SPEAKER_MODEL_URL}', p); \
digest = hashlib.sha256(open(p, 'rb').read()).hexdigest(); \
sys.exit(0 if digest == '${SPEAKER_MODEL_SHA256}' else 'speaker model checksum mismatch')" \
    && chmod -R a+rX /opt/models

# Pre-download the Silero voice-activity detector that trims silence before
# speaker embedding. Keep in sync with VAD_MODEL_URL / VAD_MODEL_SHA256 in
# app/voice/model.py; the app falls back to a one-time download into
# /data/models/speaker (and to untrimmed audio) when this file is missing.
ARG VAD_MODEL_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
ARG VAD_MODEL_SHA256=9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6
RUN python -c "import hashlib, sys, urllib.request; \
p = '/opt/models/speaker/silero_vad.onnx'; \
urllib.request.urlretrieve('${VAD_MODEL_URL}', p); \
digest = hashlib.sha256(open(p, 'rb').read()).hexdigest(); \
sys.exit(0 if digest == '${VAD_MODEL_SHA256}' else 'VAD model checksum mismatch')" \
    && chmod -R a+rX /opt/models

# Default port; CONTAINER_PORT / CONTAINER_HOST override the listen address
# (the compose files map and health-check the same variable).
EXPOSE 8080

# Same probe and start period as the compose files.
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('CONTAINER_PORT', '8080') + '/healthz')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# sh -c expands the listen address; exec replaces the shell so uvicorn runs
# as PID 1 (entrypoint.sh and setpriv exec as well) and receives SIGTERM.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host \"${CONTAINER_HOST:-0.0.0.0}\" --port \"${CONTAINER_PORT:-8080}\" --ws-ping-interval 30 --ws-ping-timeout 10 --timeout-graceful-shutdown 30"]
