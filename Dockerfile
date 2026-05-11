# Multi-stage build for a slim, production-ready image.
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# System fonts: Arabic + Latin support so cards render correctly
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-noto-core \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-noto-extra \
    fonts-noto-unhinted \
    fonts-noto-mono \
    fontconfig \
    tini \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f -v

# Install Python deps first (cache-friendly)
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e .

# Playwright browsers
RUN playwright install chromium

# App source
COPY . .

# Default runtime dirs
RUN mkdir -p data/runtime/images data/runtime/screenshots

ENV PYTHONPATH=/app

# Healthcheck via dashboard
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD curl -fsSL http://127.0.0.1:8000/health || exit 1

EXPOSE 8000

# Tini reaps zombies (Chromium spawns many) + clean signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "auto_publish.py"]
