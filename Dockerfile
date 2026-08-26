FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata

WORKDIR /app

# Install system dependencies for Chromium & Playwright
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium && \
    playwright install-deps chromium

COPY . .

ENTRYPOINT ["python", "ola_master_pipeline.py"]
