# syntax=docker/dockerfile:1
#
# IMPORTANT — build from the REPOSITORY ROOT, not from backend/:
#     docker build -f backend/Dockerfile .
#   Render (render.yaml): dockerContext "." / dockerFilePath "./backend/Dockerfile"
# Why: the image also needs sdoc-hackathon-bundle/ (inbox + attachments) which
# lives one level above backend/. Building with `backend/` as the context gives
# an empty inbox at runtime.
FROM python:3.11-slim

WORKDIR /app

# libGL / libglib are required by opencv, which rapidocr-onnxruntime pulls in
# for the OCR rung of the PDF reader ladder. Without them `import cv2` fails on
# slim images and OCR silently degrades to the `unreadable` escalation.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ---- deps first (layer cache: rebuild only when requirements change) --------
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ---- app + dataset ---------------------------------------------------------
COPY backend/ /app/
COPY sdoc-hackathon-bundle/ /bundle/

# Stage the inbox into /app/data so the container has emails to process.
# Free hosting tiers have no volumes, so the ~3.5 MB dataset is baked in.
# `data/` is git-ignored — never commit it; this step recreates it at build time.
RUN python scripts/prepare_data.py && rm -rf /bundle

ENV DATA_SOURCE=/app/data

EXPOSE 8000

# Single worker keeps the SQLite file safe; switch to 2-4 workers with Postgres.
# Render injects $PORT (10000 by default) — honour it, fall back to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
