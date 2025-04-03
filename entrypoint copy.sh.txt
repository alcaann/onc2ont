#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

# Get database connection details from environment variables
DB_HOST="$POSTGRES_HOST"
DB_PORT="$POSTGRES_PORT"
DB_USER="$POSTGRES_USER"
DB_NAME="$POSTGRES_DB"
# POSTGRES_PASSWORD is used by pg_isready/psql implicitly via PGPASSWORD,
# and by the python script directly

# --- Define Flag File Path (Must match Python script) ---
LOAD_FLAG="/tmp/umls_load_complete.flag"

# --- Updated function to check for flag file ---
check_data_loaded() {
    echo "Checking for UMLS load completion flag: ${LOAD_FLAG}..."
    if [ -f "$LOAD_FLAG" ]; then
        echo "Load flag file found. Assuming data loaded."
        return 0 # True, data loaded
    else
        echo "Load flag file not found. Assuming data needs loading."
        return 1 # False, data not loaded
    fi
}

# Wait for the database to be ready
echo "Waiting for database at $DB_HOST:$DB_PORT..."
export PGPASSWORD="$POSTGRES_PASSWORD"
# Loop until pg_isready reports the server is accepting connections
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -q; do
  sleep 2
  echo "Retrying DB connection..."
done
unset PGPASSWORD
echo "Database is ready!"

# Check if data needs loading (using flag file) and run script if necessary
if ! check_data_loaded; then
    echo "--- Running UMLS Data Loading Script (this will take a long time!) ---"
    # Execute the script as the 'appuser'
    python db_setup/load_umls_to_pgsql.py
    # Check the exit code of the python script
    if [ $? -eq 0 ]; then
        # Double-check if the flag file was actually created by the successful script
        if [ -f "$LOAD_FLAG" ]; then
            echo "--- UMLS Data Loading Script Completed Successfully (Flag file created) ---"
        else
            echo "--- WARNING: Loading script exited successfully BUT flag file was NOT created! Check script logic. ---"
            # Decide if this should be a fatal error - exiting for safety
            exit 1
        fi
    else
        echo "--- UMLS Data Loading Script Failed! Check logs above. ---"
        exit 1 # Exit if loading fails
    fi
else
    echo "Skipping UMLS data loading (flag file exists)."
fi

# Execute the command passed to the entrypoint (e.g., the CMD from Dockerfile or docker-compose)
echo "Entrypoint finished setup. Executing command: $@"
exec "$@"