FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY constraints.txt constraints-ocr.txt requirements.txt requirements-ocr.txt ./
# Core and OCR constraints intentionally differ: PaddleX 3.7.x requires NumPy <2.4.
# Install the core first, then resolve the isolated worker against its own constraints.
RUN python -m pip install -r requirements.txt -c constraints.txt \
    && python -m pip install -r requirements-ocr.txt

COPY sherpa ./sherpa
COPY docker/ocr/NOTICE.md docker/ocr-models.lock.json /opt/sherpa-ocr/
# これは依存棚卸し用package inventoryであり、CycloneDX/SPDX等のSBOMではない。
RUN python -m pip list --format=json > /opt/sherpa-ocr/python-package-inventory.json

ENTRYPOINT []
CMD ["python", "-m", "sherpa.ingest.ocr_worker"]
