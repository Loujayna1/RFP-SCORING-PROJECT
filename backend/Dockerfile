FROM python:3.11-slim

# System deps: Tesseract OCR + PDF tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-fra \
    tesseract-ocr-ara \
    poppler-utils \
    libgl1 \
    curl \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libffi-dev \
    fonts-dejavu \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cache Hugging Face / sentence-transformers downloads across container runs.
# PDF parsing defaults to pdfplumber (USE_DOCLING=false). Set USE_DOCLING=true
# only after prefetching Docling models via scripts/download_docling_models.py.
ENV HF_HOME=/cache/huggingface \
    TRANSFORMERS_CACHE=/cache/huggingface/transformers \
    SENTENCE_TRANSFORMERS_HOME=/cache/sentence-transformers \
    DOCLING_ARTIFACTS_PATH=/opt/docling/models \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

# Install CPU-only PyTorch first (much smaller — no CUDA)
RUN pip install --no-cache-dir \
    torch==2.3.1+cpu \
    torchvision==0.18.1+cpu \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Install the rest. Pin CPU torch via constraints so docling/sentence-transformers
# do not pull a 500MB+ CUDA torch wheel during dependency resolution.
COPY requirements.txt .
RUN printf '%s\n' 'torch==2.3.1' 'torchvision==0.18.1' > /tmp/constraints.txt \
    && grep -vE '^(torch|torchvision)' requirements.txt \
    | pip install --no-cache-dir \
        --default-timeout=${PIP_DEFAULT_TIMEOUT} \
        --retries=${PIP_RETRIES} \
        -r /dev/stdin \
        -c /tmp/constraints.txt

COPY . .
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000
CMD ["/bin/bash", "/app/entrypoint.sh"]
