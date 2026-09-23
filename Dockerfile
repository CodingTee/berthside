# syntax=docker/dockerfile:1
#
# Build from the REPOSITORY ROOT:
#     docker build .
#
# Render:
#     dockerContext: .
#     dockerfilePath: ./Dockerfile

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

COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/
COPY scripts/ /app/scripts/
COPY web/ /app/web/

COPY data/corpus/ /bundle/

# Build the ShipMail static inbox from the same corpus the Dashboard uses, so
# the two surfaces always show the same emails.
RUN python scripts/generate_shipmail_inbox.py --bundle /bundle --out web/shipmail/data

# Prepare the static dataset.
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

# Materialise the curated multi-shipment demo dataset (SHP-001 … SHP-006).
# `--spawn` drives the real routes (it starts and stops a private server), so the
# shipments, document versions and verification results are baked into the same
# database the pre-computed corpus lives in and the shipment API is populated on
# first boot instead of on first click.
RUN python scripts/load_demo_shipments.py --spawn

# The source corpus is no longer needed after the dataset
# has been copied into /app/data and reports have been generated.
RUN rm -rf /bundle

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
