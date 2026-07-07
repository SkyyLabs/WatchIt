# WatchIt backend image — serves both deployables:
#   API (default):  uvicorn watchit_api.main:app
#   worker:         docker run ... python -m watchit_agents.worker
# Config is env-driven (.env is NOT baked in): DATABASE_URL, CLERK_*, WATCHIT_*.
# Multi-instance API or a standalone worker needs WATCHIT_SSE_BUS=postgres.
FROM python:3.11-slim

WORKDIR /app

# Docling/OCR native deps kept minimal; add tesseract/poppler here only if a
# deployment actually enables OCR paths that need them.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY apps/api/src apps/api/src
COPY services/agent-worker/src services/agent-worker/src
COPY services/learning-worker/src services/learning-worker/src
COPY packages/core/src packages/core/src
COPY migrations migrations
COPY alembic.ini ./

RUN pip install --no-cache-dir -e .

ENV PYTHONPATH=/app/apps/api/src:/app/services/agent-worker/src:/app/services/learning-worker/src:/app/packages/core/src \
    WATCHIT_BIND_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 4849

# Migrations run on startup (API and worker both call db.connect()).
CMD ["uvicorn", "watchit_api.main:app", "--host", "0.0.0.0", "--port", "4849"]
