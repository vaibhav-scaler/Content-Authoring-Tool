# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Set work directory
WORKDIR /app

# Install system dependencies (build-essential and curl if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy backend and frontend
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# Expose port (Railway dynamically injects $PORT)
EXPOSE 8000

# Work in backend directory for database and app execution
WORKDIR /app/backend

# Run seed if database doesn't exist, then start uvicorn
CMD ["sh", "-c", "python -m seed_curriculum || true; uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
