# ---- Builder stage: compile native wheels ----
FROM python:3.12-slim-bookworm@sha256:d193c6f51a7dbd10395d6328de3a7edb0516fb0608ca138036576f574c3e07d2 AS builder

WORKDIR /build

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip "setuptools>=78.1.1"

COPY requirements.txt .

# Install CPU-only PyTorch BEFORE main requirements.
# sentence-transformers depends on torch; by pre-installing the CPU-only
# variant, pip will find torch already satisfied and skip the CUDA build.
# This eliminates ~4.3 GB of nvidia/cuda/triton libraries.
RUN pip install --no-cache-dir --prefix=/install \
    torch torchaudio torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# torchvision pulls in Pillow; force-upgrade to a secure version afterward
RUN pip install --upgrade --no-cache-dir --prefix=/install "Pillow>=12.2.0"

# Make pre-installed packages visible so pip skips torch on the next install
ENV PYTHONPATH=/install/lib/python3.12/site-packages

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Final stage: slim runtime ----
FROM python:3.12-slim-bookworm@sha256:d193c6f51a7dbd10395d6328de3a7edb0516fb0608ca138036576f574c3e07d2

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

RUN pip install --upgrade --no-cache-dir "setuptools>=78.1.1,<82"

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

# Ensure ca-certificates is present for HTTPS
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

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--ws-ping-interval", "30", "--ws-ping-timeout", "10", "--timeout-graceful-shutdown", "30"]
