# syntax=docker/dockerfile:1
#
# IMPORTANT — build from the REPOSITORY ROOT, not from backend/:
#     docker build -f backend/Dockerfile .
#
# Render:
#     dockerContext: .
#     dockerfilePath: ./backend/Dockerfile

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATA_SOURCE=/app/data \
    OCR_ENABLED=1

COPY backend/requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY backend/ /app/

COPY sdoc-hackathon-bundle/ /bundle/

# Prepare the static hackathon dataset.
RUN python scripts/prepare_data.py

# Pre-compute all email reports during Docker build.
# This prevents Render startup from processing 520 emails.
#
# OCR_ENABLED=1 costs about 21s here (measured: the same run takes 2.1s with it
# off). The corpus has three emails whose SI/BL pair is a scanned PDF with no
# text layer, and transcribing them is the only way those pages are ever read.
# Measured: switching OCR off changes 0 of 520 verdicts, reasons and defect
# lists, so this is not about the score. It is about what the reviewer is handed:
# with OCR on, those escalations carry the transcription as evidence and record
# `ocr_documents`; with OCR off they say only "could not read". It is also the
# one path that could ever compare a scanned document, which is what a real
# inbox eventually contains.
RUN python scripts/precompute_reports.py

# The source bundle is no longer needed after the dataset
# has been copied into /app/data and reports have been generated.
RUN rm -rf /bundle

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
