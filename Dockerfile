FROM python:3.11-slim

# Run as a non-root user for security
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
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# Copy the rest of the backend source code
COPY --chown=user . .

EXPOSE 7860

# FastAPI Healthcheck
HEALTHCHECK CMD curl --fail http://localhost:7860/health || exit 1

# Start the FastAPI server using Uvicorn on port 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]