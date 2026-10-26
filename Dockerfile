# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Set work directory
WORKDIR /app

# Install system dependencies (build-essential, curl, nodejs for code runner)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy backend and frontend
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
# the curriculum catalog the first start reads to create programs, modules and classes (seed_curriculum.py looks for it next to backend/)
COPY Scaler_Official_Curriculum_And_Course_Catalog.txt /app/Scaler_Official_Curriculum_And_Course_Catalog.txt

# Expose port (Railway dynamically injects $PORT)
EXPOSE 8000

# Work in backend directory for database and app execution
WORKDIR /app/backend

# Run seed if database doesn't exist, then start uvicorn
CMD ["sh", "-c", "python -m seed_curriculum || true; uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
