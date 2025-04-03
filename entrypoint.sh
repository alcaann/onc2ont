#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

# --- Read Database Connection Details ---
DB_HOST="${DB_HOST:-db}" # Use DB_HOST env var, default to 'db' if not set
DB_PORT="${DB_PORT:-5432}" # Use DB_PORT env var, default to '5432' if not set
DB_USER="${POSTGRES_USER:-umls_user}" # Use POSTGRES_USER, default if not set
DB_NAME="${POSTGRES_DB:-umls_db}" # Use POSTGRES_DB, default if not set
# POSTGRES_PASSWORD MUST be set in the environment for psql/pg_isready

if [ -z "$POSTGRES_PASSWORD" ]; then
  echo "FATAL ERROR: POSTGRES_PASSWORD environment variable is not set."
  exit 1
fi

# --- Define Path to UMLS Source Data (inside this container) ---
# This MUST match the volume mount in docker-compose.yml
UMLS_META_DIR="/app/data/source/umls/META"
REQUIRED_FILE_1="${UMLS_META_DIR}/MRCONSO.RRF"
REQUIRED_FILE_2="${UMLS_META_DIR}/MRSTY.RRF"

# --- Function to check if DB seems populated ---
# Checks if the 'mrconso' table exists and has at least one row.
# Returns 0 (true) if populated, 1 (false) otherwise.
check_db_populated() {
    echo "Checking if database '$DB_NAME' table 'mrconso' seems populated..."
    export PGPASSWORD="$POSTGRES_PASSWORD"
    local check_sql="SELECT 1 FROM mrconso LIMIT 1;"
    local query_output

    # Execute query, capture output, check exit status AND output presence
    # Redirect stderr to /dev/null to hide "table does not exist" errors from output check
    query_output=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "$check_sql" 2>/dev/null)
    local exit_code=$?
    unset PGPASSWORD

    # Check if psql command succeeded (exit code 0) AND produced output (found a row)
    if [ $exit_code -eq 0 ] && [ -n "$query_output" ]; then
        echo "Database table 'mrconso' appears to be populated."
        return 0 # Success (populated)
    else
        if [ $exit_code -ne 0 ]; then
             echo "Database check failed (exit code $exit_code - table 'mrconso' might not exist)."
        else
             echo "Database table 'mrconso' exists but appears empty."
        fi
        return 1 # Failure (not populated or check failed)
    fi
}

# --- Function to check if required UMLS source files exist ---
check_source_files_exist() {
    echo "Checking for required UMLS source files in ${UMLS_META_DIR}..."
    if [ -f "$REQUIRED_FILE_1" ] && [ -f "$REQUIRED_FILE_2" ]; then
        echo "Required source files ($REQUIRED_FILE_1, $REQUIRED_FILE_2) found."
        return 0 # Success
    else
        echo "ERROR: One or both required UMLS source files not found!"
        [ ! -f "$REQUIRED_FILE_1" ] && echo "  Missing: $REQUIRED_FILE_1"
        [ ! -f "$REQUIRED_FILE_2" ] && echo "  Missing: $REQUIRED_FILE_2"
        echo "  Check volume mount for '${UMLS_META_DIR}'."
        return 1 # Failure
    fi
}


# --- Wait for Database ---
echo "Waiting for database service at $DB_HOST:$DB_PORT..."
export PGPASSWORD="$POSTGRES_PASSWORD"
# Loop until pg_isready reports the server is accepting connections
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -q; do
  sleep 2
  echo "Retrying DB connection to $DB_HOST:$DB_PORT for user $DB_USER on db $DB_NAME..."
done
unset PGPASSWORD # Unset password now that DB is ready for psql check
echo "Database $DB_NAME at $DB_HOST:$DB_PORT is ready!"


# --- Load Data Logic ---
# Check if data needs loading ONLY IF DB is not already populated
if ! check_db_populated; then
    echo "Database is not populated. Proceeding with checks before loading."
    # Check if source files exist before attempting load
    if check_source_files_exist; then
        echo "--- Running UMLS Data Loading Script (this will take a long time!) ---"
        # Execute the script. Ensure it uses the correct env vars if needed.
        python db_setup/load_umls_to_pgsql.py

        # Check the exit code of the python script
        load_exit_code=$?
        if [ $load_exit_code -eq 0 ]; then
            echo "--- UMLS Data Loading Script Completed Successfully (Exit Code 0) ---"
            # Optional: The python script could still create the flag file for reference
            # if [ -f "$LOAD_FLAG" ]; then echo "Load flag file created by script."; fi
        else
            echo "--- UMLS Data Loading Script Failed! Exit code: $load_exit_code. Check logs above. ---"
            exit 1 # Exit if loading fails
        fi
    else
        echo "--- UMLS source files missing. Cannot run loading script. Please mount data correctly. ---"
        exit 1 # Exit because source files are required but missing
    fi
else
    echo "Database already populated. Skipping UMLS data loading."
fi


# --- Execute Main Command ---
# Execute the command passed to the entrypoint (e.g., the CMD from Dockerfile or docker-compose)
echo "Entrypoint finished setup. Executing command: $@"
exec "$@"