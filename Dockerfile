# Dockerfile

# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1  # Prevents python from writing pyc files
ENV PYTHONUNBUFFERED 1      # Prevents python from buffering stdout/stderr
ENV PYTHONPATH=/app

# Set the working directory in the container
WORKDIR /app

# Install system dependencies required for psycopg2 and postgresql-client
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
RUN pip install --no-cache-dir -r requirements.txt


# Copy the rest of the application code into the working directory
# This includes scripts/, db_setup/, and data/ including source/
# Ensure data/source/umls/META with RRF files is present before building!
COPY . .

# Create a non-root user and switch to it (good practice)
# Ensures scripts run with appropriate permissions inside the container
RUN adduser --disabled-password --gecos "" appuser
# Give ownership of the app directory to the user
RUN chown -R appuser:appuser /app
USER appuser

# Ensure the entrypoint script is executable (copied with permissions)
# The chmod +x needs to be done on the HOST before building,
# but we can ensure it here too.
RUN chmod +x /app/entrypoint.sh

# Download the spaCy model during the build
RUN python -m spacy download en_core_web_sm

# Set the entrypoint script to run when the container starts
ENTRYPOINT ["/app/entrypoint.sh"]

# Default command (can be overridden in docker-compose.yml)
# Example: run an interactive shell if no command is given
# Or change to your main script: ["python", "scripts/preprocess_data.py"]
CMD ["bash"]