# syntax=docker/dockerfile:1
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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The cloud backend needs the inbox at runtime. Run
#   python scripts/prepare_data.py
# before `docker build` (or a Render deploy) to stage the dataset into data/.
# Free hosting tiers have no volumes, so the ~3.5 MB dataset is baked into the
# image. `data/` is git-ignored — never commit the dataset.
#
# If you deploy straight from git (Render auto-build, etc.) that step does NOT
# happen, so bake it into the build instead: uncomment the line below.
# RUN python scripts/prepare_data.py
ENV DATA_SOURCE=/app/data

EXPOSE 8000

# Single worker keeps the SQLite file safe; switch to 2-4 workers with Postgres.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
