# Dockerfile for school-core
# Usage: docker compose up (see docker-compose.yml)

FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data dir
RUN mkdir -p data

# Default env
ENV MODEL_PROVIDER=nous
ENV ORCA_MODE=http
ENV ORCA_HTTP=http://orca-shim:9100
ENV NOUS_MODEL=meituan/longcat-2.0:free

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)"

# Default command: run conductor
CMD ["python3", "conductor.py", "--serve"]
