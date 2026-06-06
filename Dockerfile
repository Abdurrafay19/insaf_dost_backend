# Use Python 3.11 (Highly recommended over 3.13 for PyTorch/ML library stability)
FROM python:3.11-slim

# Hugging Face Spaces require running as a non-root user for security
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

# Set the working directory
WORKDIR $HOME/app

# Set Hugging Face cache directory to be writable by the non-root user
ENV HF_HOME=$HOME/app/.cache

# Switch back to root temporarily to install system dependencies
USER root
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*
# Give the non-root user ownership of the app directory
RUN chown -R user:user $HOME/app
USER user

# Copy requirements first to leverage Docker cache
COPY --chown=user requirements.txt ./

# Install Python dependencies
# We use --no-cache-dir to keep the image size small
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the rest of the backend source code (main.py, ingest.py, etc.)
COPY --chown=user . .

# Hugging Face Spaces exposes port 7860 by default
EXPOSE 7860

# FastAPI Healthcheck (points to the /health endpoint we created)
HEALTHCHECK CMD curl --fail http://localhost:7860/health || exit 1

# Start the FastAPI server using Uvicorn on port 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]