# Trading Engine Dockerfile

FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Create non-root user
RUN groupadd -g 1001 python && \
    useradd -r -u 1001 -g python python

# Copy source code with correct ownership
COPY --chown=python:python src ./src

# Copy ML model files (for EKS deployment)
COPY --chown=python:python models ./models

USER python

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV ML_MODEL_DIR=/app/models

# Health check (simple process check)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import sys; sys.exit(0)"

# Start the service
CMD ["python", "-m", "src.main", "realtime", "--live"]
