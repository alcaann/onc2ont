#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

# --- Read Database Connection Details ---
DB_HOST="${DB_HOST:-db}" # Use DB_HOST env var, default to 'db' if not set
DB_PORT="${DB_PORT:-5432}" # Use DB_PORT env var, default to '5432' if not set
# --- ALIGNED DEFAULTS with docker-compose.yml defaults ---
DB_USER="${POSTGRES_USER:-umls_user}"
DB_NAME="${POSTGRES_DB:-umls_db}"
# --- End Aligned Defaults ---

# POSTGRES_PASSWORD MUST be set in the environment for psql/pg_isready
if [ -z "$POSTGRES_PASSWORD" ]; then
  echo "FATAL ERROR: POSTGRES_PASSWORD environment variable is not set."
  exit 1
fi
export PGPASSWORD="$POSTGRES_PASSWORD" # Export password for psql/pg_isready usage

# --- Define Path to UMLS Source Data (inside this container) ---
# This MUST match the volume mount in docker-compose.yml
UMLS_META_DIR="/app/data/source/umls/META"
MRCONSO_FILE="${UMLS_META_DIR}/MRCONSO.RRF"
MRSTY_FILE="${UMLS_META_DIR}/MRSTY.RRF"
MRREL_FILE="${UMLS_META_DIR}/MRREL.RRF"

# --- Define Path to Load Success Flag File ---
# This should match the path used in load_umls_to_pgsql.py
LOAD_SUCCESS_FLAG_FILE="/tmp/umls_load_complete.flag"

# --- Function to check if DB seems populated ---
# Checks using the flag file first, then falls back to querying mrconso.
# Returns 0 (true) if populated (flag exists or table has data), 1 (false) otherwise.
check_db_populated() {
    echo "Checking if database '$DB_NAME' seems populated..."

    # 1. Check for the success flag file first (faster and more reliable if loader creates it)
    if [ -f "$LOAD_SUCCESS_FLAG_FILE" ]; then
        echo "Load success flag file found ($LOAD_SUCCESS_FLAG_FILE). Assuming DB is populated."
        return 0 # Success (populated based on flag)
    fi
    echo "Load success flag file not found. Checking table 'mrconso' content..."

    # 2. If flag not found, check the mrconso table content as a fallback
    local check_sql="SELECT 1 FROM mrconso LIMIT 1;"
    local query_output

    # Execute query, capture output, check exit status AND output presence
    # PGPASSWORD is already exported
    query_output=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -tAc "$check_sql" 2>/dev/null)
    local exit_code=$?

    # Check if psql command succeeded (exit code 0) AND produced output (found a row)
    if [ $exit_code -eq 0 ] && [ -n "$query_output" ]; then
        echo "Database table 'mrconso' appears to be populated (found rows)."
        # Optional: Create the flag file now if it was missing but DB is populated?
        # touch "$LOAD_SUCCESS_FLAG_FILE" # Could be useful if loader script failed to create it but data is there
        return 0 # Success (populated based on table content)
    else
        if [ $exit_code -ne 0 ]; then
             echo "Database check failed (exit code $exit_code - table 'mrconso' might not exist or DB connection issue)."
        else
             echo "Database table 'mrconso' exists but appears empty (or flag was missing)."
        fi
        return 1 # Failure (not populated or check failed)
    fi
}

# --- Function to check if required UMLS source files exist ---
check_source_files_exist() {
    echo "Checking for required UMLS source files in ${UMLS_META_DIR}..."
    local all_found=true # Assume all are found initially

    # Check each file individually and report missing ones
    if [ ! -f "$MRCONSO_FILE" ]; then
        echo "  ERROR: Missing required source file: $MRCONSO_FILE"
        all_found=false
    fi
    if [ ! -f "$MRSTY_FILE" ]; then
        echo "  ERROR: Missing required source file: $MRSTY_FILE"
        all_found=false
    fi
    if [ ! -f "$MRREL_FILE" ]; then
        echo "  ERROR: Missing required source file: $MRREL_FILE"
        all_found=false
    fi

    # Final decision based on checks
    if [ "$all_found" = true ]; then
        echo "Required source files (MRCONSO, MRSTY, MRREL) found."
        return 0 # Success
    else
        echo "ERROR: One or more required UMLS source files not found!"
        echo "  Check volume mount for '${UMLS_META_DIR}' in docker-compose.yml."
        return 1 # Failure
    fi
}


# --- Wait for Database ---
echo "Waiting for database service at $DB_HOST:$DB_PORT..."
# PGPASSWORD already exported
# Loop until pg_isready reports the server is accepting connections
# Added timeout (e.g., 60 seconds) to prevent infinite loop
timeout_sec=60
start_time=$(date +%s)
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -q; do
  current_time=$(date +%s)
  elapsed_time=$((current_time - start_time))
  if [ $elapsed_time -ge $timeout_sec ]; then
      echo "FATAL ERROR: Database connection timeout after $timeout_sec seconds."
      # Unset password before exiting
      unset PGPASSWORD
      exit 1
  fi
  sleep 2
  echo "Retrying DB connection to $DB_HOST:$DB_PORT for user $DB_USER on db $DB_NAME..."
done
echo "Database $DB_NAME at $DB_HOST:$DB_PORT is ready!"


# --- Load Data Logic ---
# Check if data needs loading ONLY IF DB is not already populated (using updated check function)
if ! check_db_populated; then
    echo "Database is not populated (or flag file missing). Proceeding with checks before loading."
    # Check if source files exist before attempting load
    if check_source_files_exist; then
        echo "--- Running UMLS Data Loading Script (this will take a long time!) ---"
        # Execute the script. Ensure it uses the correct env vars if needed.
        # Use `python -u` for unbuffered output to see progress/errors sooner
        # Ensure the script scripts/load_umls_to_pgsql.py correctly reads env vars for DB connection
        python -u scripts/load_umls_to_pgsql.py

        # Check the exit code of the python script
        load_exit_code=$?
        if [ $load_exit_code -eq 0 ]; then
            echo "--- UMLS Data Loading Script Completed Successfully (Exit Code 0) ---"
            # Verify the flag file was created by the script as final confirmation
            if [ -f "$LOAD_SUCCESS_FLAG_FILE" ]; then
                 echo "Load success flag file verified."
            else
                 # If the script exited cleanly but didn't create the flag, maybe create it now?
                 # This depends on whether the script *guarantees* flag creation on success.
                 echo "WARNING: Python script exited 0, but load success flag file ($LOAD_SUCCESS_FLAG_FILE) is missing!" >&2
                 echo "         Creating flag file now, but load might be incomplete. Check python script logs." >&2
                 # Let's create it here to avoid re-running the load next time, but the warning is important.
                 touch "$LOAD_SUCCESS_FLAG_FILE" || echo "Warning: Failed to create flag file." >&2
                 # Decide if this is a fatal error or just a warning
                 # exit 1
            fi
        else
            echo "--- UMLS Data Loading Script Failed! Exit code: $load_exit_code. Check logs above. ---" >&2
            # Ensure flag file doesn't exist if load failed
            rm -f "$LOAD_SUCCESS_FLAG_FILE"
            unset PGPASSWORD # Unset password before exiting
            exit 1 # Exit if loading fails
        fi
    else
        echo "--- UMLS source files missing. Cannot run loading script. ---" >&2
        echo "--- Please mount data correctly and restart the container. ---" >&2
        unset PGPASSWORD # Unset password before exiting
        exit 1 # Exit because source files are required but missing
    fi
else
    echo "Database already populated (or flag file exists). Skipping UMLS data loading."
fi

# --- Unset Password ---
# Best practice to unset the password from env once DB checks/loads are done
unset PGPASSWORD

# --- Execute Main Command ---
# Execute the command passed to the entrypoint (e.g., the CMD from Dockerfile or docker-compose)
echo "Entrypoint finished setup. Executing command: $@"
exec "$@"