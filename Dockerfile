FROM python:3.12-slim

# System dependencies: Tesseract OCR + Poppler (pdf2image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies.
# See Dockerfile.lambda: poetry.lock is named exactly (not globbed) so a missing
# lockfile fails the build instead of silently resolving dependencies fresh.
# --no-root: app/ is copied below.
COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir poetry==1.8.4 \
    && poetry config virtualenvs.create false \
    && poetry install --only main --no-root --no-interaction --no-ansi

COPY app/ ./app/

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
