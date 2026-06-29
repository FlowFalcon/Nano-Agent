FROM python:3.12-slim

# Install system dependencies (git, curl etc might be needed by some tools/MCPs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV AGENT_CONFIG_PATH=/app/config.json

# Expose port for webhook mode
EXPOSE 8080

CMD ["python", "main.py"]
