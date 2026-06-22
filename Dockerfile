# ── Stage 1: Builder ─────────────────────────────────────────────────────
FROM python:3.14-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2: Runtime base ────────────────────────────────────────────────
FROM python:3.14-slim AS base

RUN groupadd --gid 1000 settle \
    && useradd --uid 1000 --gid settle --shell /bin/bash --create-home settle

COPY --from=builder /install /usr/local

WORKDIR /app
COPY . .

RUN mkdir -p data/generated data/knowledge_base/faiss_index \
    && chown -R settle:settle data/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SETTLE_LOG_FORMAT=json \
    SETTLE_LOG_LEVEL=INFO

# ── Target: pipeline ────────────────────────────────────────────────────
FROM base AS pipeline

USER settle
ENTRYPOINT ["python", "-m", "main"]

# ── Target: dashboard ───────────────────────────────────────────────────
FROM base AS dashboard

EXPOSE 8501

USER settle
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

ENTRYPOINT ["streamlit", "run", "dashboard/app.py", \
    "--server.port=8501", \
    "--server.address=0.0.0.0", \
    "--server.headless=true", \
    "--browser.gatherUsageStats=false"]

# ── Target: test ────────────────────────────────────────────────────────
FROM base AS test

USER settle
ENTRYPOINT ["python", "-m", "pytest", "tests/", "-v", "--tb=short"]
