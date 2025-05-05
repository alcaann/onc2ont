# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV PYTHONPATH=/app

# Set the working directory in the container
WORKDIR /app

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Copy only requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# Copy the entrypoint script
COPY entrypoint.sh /app/entrypoint.sh

# Create a non-root user and switch to it
RUN adduser --disabled-password --gecos "" appuser

# Give ownership of the app directory and entrypoint to the user
# Note: /app/api and /app/pipelines will be mounted later, ownership might be root depending on host OS/Docker settings
RUN chown appuser:appuser /app /app/entrypoint.sh /app/requirements.txt

# Ensure the entrypoint script is executable
RUN chmod +x /app/entrypoint.sh

# Switch to the non-root user
USER appuser

# Set the entrypoint script to run when the container starts
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command (will be overridden by docker-compose.yml)
CMD ["bash"]