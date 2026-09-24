# Sherpa optional OCR worker notices

This image is optional and is not part of the FastAPI/core Markdown conversion environment.

- Python dependencies are installed only from `requirements-ocr.txt` plus the core runtime requirements.
- The resolved Python package inventory is written to `/opt/sherpa-ocr/python-package-inventory.json` during
  image build. This `pip list` inventory is not a CycloneDX/SPDX SBOM and does not include a completed license review.
- PaddleOCR and PaddlePaddle versions are fixed by the worker profile. A real SBOM and review of upstream notices,
  transitive dependency licenses, and redistribution obligations are still required before commercial distribution.
- PP-OCR model files are not copied into this image or repository. They are supplied through a read-only mount,
  and the worker refuses models whose tree hashes do not match `docker/ocr-models.lock.json`.
- License names in the model lock are upstream declarations, not a completed legal approval by this project. Before
  that approval, model files must not be bundled into the repository, container image, or distribution package.
- Runtime model downloads and network OCR services are prohibited by the worker contract.
