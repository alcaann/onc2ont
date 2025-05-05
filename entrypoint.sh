#!/bin/bash
# filepath: d:\TFG\onc2ont\entrypoint.sh
# (REVISED - Removed all database and UMLS loading logic)

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Check for required cTAKES environment variables ---
# These are needed for the application logic (e.g., in api/main.py or processor.py)
echo "Checking for required cTAKES environment variables..."
if [ -z "$CTAKES_URL" ]; then
  echo "FATAL ERROR: CTAKES_URL environment variable is not set."
  exit 1
fi
if [ -z "$PROCESSING_PIPELINE" ]; then
  echo "FATAL ERROR: PROCESSING_PIPELINE environment variable is not set."
  exit 1
fi
echo "Required environment variables seem present."

# --- Execute Main Command ---
# Execute the command passed to the entrypoint (e.g., the CMD from Dockerfile or docker-compose)
echo "Entrypoint finished setup. Executing command: $@"
exec "$@"