# ── ytauto — Production Dockerfile ────────────────────────────────────────────
# Runs the FastAPI dashboard + full automation pipeline inside a Linux container.
# Secrets (token.json, client_secrets.json) are injected at runtime via env vars.

FROM python:3.12-slim

# ── System environment ──────────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── System packages ─────────────────────────────────────────────────────────────
# ffmpeg       — video assembly, frame extraction, audio encoding
# fonts-*      — thumbnail text rendering (replaces Windows fonts)
# ca-certs     — HTTPS for Pexels / YouTube / Telegram APIs
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-liberation \
        fonts-freefont-ttf \
        fonts-dejavu-core \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python: CPU-only PyTorch first ──────────────────────────────────────────────
# Must be installed BEFORE openai-whisper so pip picks the CPU build (~700 MB)
# instead of the CUDA build (~3 GB).
RUN pip install \
        torch \
        torchvision \
        torchaudio \
        --index-url https://download.pytorch.org/whl/cpu

# ── Python: application dependencies ───────────────────────────────────────────
COPY requirements.txt .
RUN pip install -r requirements.txt

# ── Application code ────────────────────────────────────────────────────────────
# .dockerignore excludes: output/, .env, token.json, client_secrets.json, *.mp4/mp3
COPY . .

# ── Runtime directories ─────────────────────────────────────────────────────────
# Mount /app/output as a persistent volume in your container platform so
# generated videos survive container restarts.
RUN mkdir -p /app/output

# ── Entrypoint ──────────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["uvicorn", "dashboard:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-keep-alive", "75"]
