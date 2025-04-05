# --- START OF FILE Dockerfile ---
# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
# Prevents python from writing pyc files
ENV PYTHONDONTWRITEBYTECODE 1  
# Prevents python from buffering stdout/stderr
ENV PYTHONUNBUFFERED 1      
# Add scripts to PYTHONPATH for imports
ENV PYTHONPATH=/app:/app/scripts 

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for psycopg2 and postgresql-client
# build-essential might be needed for some packages, keep it for now
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
       postgresql-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Copy only requirements first to leverage Docker cache
COPY requirements.txt .
# Make sure the requirements file includes fastapi and uvicorn[standard]
RUN pip install --no-cache-dir -r requirements.txt
# Note: The en_ner_bc5cdr_md model specified in requirements.txt will be downloaded here

# Copy application code - split for potentially better caching
COPY scripts/ /app/scripts/
# ADDED: Copy the new API directory
COPY api/ /app/api/ 
COPY db_setup/ /app/db_setup/
COPY entrypoint.sh /app/entrypoint.sh
# Copy other potential top-level files if needed (e.g., configuration)
# COPY *.py /app/

# Create a non-root user and switch to it (good practice)
RUN adduser --disabled-password --gecos "" appuser

# Give ownership of the app directory and copied files to the user
# Run chown *before* switching user
RUN chown -R appuser:appuser /app

# Ensure the entrypoint script is executable
# (Should also be executable on host before building)
RUN chmod +x /app/entrypoint.sh

# Switch to the non-root user
USER appuser

# Set the entrypoint script to run when the container starts
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command (will be overridden by docker-compose.yml to start the API server)
CMD ["bash"]
# --- END OF FILE Dockerfile ---