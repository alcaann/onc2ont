# db_setup/load_umls_to_pgsql.py
import sys
import os
import time
import psycopg2
from psycopg2 import sql
import csv
import io # For StringIO used with COPY FROM STDIN

# --- Import the database connection function ---
# Make sure 'scripts/db_utils.py' exists and defines 'get_db_connection'
try:
    # Renamed import based on user request
    from scripts.db_utils import get_db_connection
except ImportError as e:
    print(f"Error importing get_db_connection from scripts.db_utils: {e}", file=sys.stderr)
    print("Please ensure scripts/db_utils.py exists and defines the get_db_connection function.", file=sys.stderr)
    sys.exit(1)
# ---------------------------------------------

# --- Configuration (Load from Environment Variables or defaults) ---
# Database connection details (replace with your actual env var names if different)
DB_NAME = os.getenv('POSTGRES_DB', 'umls_db')
DB_USER = os.getenv('POSTGRES_USER', 'umls_user')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD') # Ensure this is set in docker-compose.yml
DB_HOST = os.getenv('DB_HOST', 'db') # Service name in docker-compose
DB_PORT = os.getenv('DB_PORT', '5432')

# Source Data File Paths (relative to the mount point inside the container)
UMLS_META_PATH = '/app/data/source/umls/META'
MRCONSO_FILE = os.path.join(UMLS_META_PATH, 'MRCONSO.RRF')
MRSTY_FILE = os.path.join(UMLS_META_PATH, 'MRSTY.RRF')

# Target language for MRCONSO
TARGET_LANG = 'ENG'

# Use a path likely writable by the appuser inside the container
LOAD_SUCCESS_FLAG_FILE = "/tmp/umls_load_complete.flag"

# --- Column Definitions for RRF files (adjust if your UMLS version differs) ---
# Based on standard UMLS RRF format
MRCONSO_COLS = ['CUI', 'LAT', 'TS', 'LUI', 'STT', 'SUI', 'ISPREF', 'AUI', 'SAUI', 'SCUI', 'SDUI', 'SAB', 'TTY', 'CODE', 'STR', 'SRL', 'SUPPRESS', 'CVF']
MRSTY_COLS = ['CUI', 'TUI', 'STN', 'STY', 'ATUI', 'CVF']

# --- Target Table Column Definitions in PostgreSQL ---
# Define the columns we want to load into the PostgreSQL tables
PG_MRCONSO_TARGET_COLS = ['CUI', 'LAT', 'AUI', 'SAB', 'TTY', 'CODE', 'STR']
PG_MRSTY_TARGET_COLS = ['CUI', 'TUI', 'STY']

# Create lookup dictionaries for column indices
MRCONSO_COLS_LOOKUP = {name: idx for idx, name in enumerate(MRCONSO_COLS)}
MRSTY_COLS_LOOKUP = {name: idx for idx, name in enumerate(MRSTY_COLS)}

# Lookup for target PostgreSQL column indices relative to RRF columns
PG_MRCONSO_TARGET_COLS_LOOKUP = {col: MRCONSO_COLS_LOOKUP[col] for col in PG_MRCONSO_TARGET_COLS}
PG_MRSTY_TARGET_COLS_LOOKUP = {col: MRSTY_COLS_LOOKUP[col] for col in PG_MRSTY_TARGET_COLS}

# --- SQL Strings for COPY command ---
# Generate the column list string for the COPY command dynamically
# Corrected: Removed the extra [0] index which was taking only the first character
PG_MRCONSO_COPY_COLS_SQL = ", ".join([sql.Identifier(col).strings[0] for col in PG_MRCONSO_TARGET_COLS])
PG_MRSTY_COPY_COLS_SQL = ", ".join([sql.Identifier(col).strings[0] for col in PG_MRSTY_TARGET_COLS])


# --- Function Definitions ---

def check_file_exists(filepath):
    """Checks if a given file exists and is readable."""
    if not os.path.exists(filepath):
        print(f"Error: File not found at {filepath}", file=sys.stderr)
        return False
    if not os.path.isfile(filepath):
        print(f"Error: Path is not a file: {filepath}", file=sys.stderr)
        return False
    if not os.access(filepath, os.R_OK):
        print(f"Error: File is not readable: {filepath}", file=sys.stderr)
        return False
    print(f"File found and readable: {filepath}")
    return True

def create_tables(conn):
    """Creates the necessary tables (mrconso, mrsty) if they don't exist."""
    if conn is None:
        print("Error: No database connection provided to create_tables.", file=sys.stderr)
        return False
    try:
        with conn.cursor() as cur:
            # Drop tables if they exist (for idempotency during testing, remove in prod if needed)
            print("Dropping existing tables (if any)...")
            cur.execute("DROP TABLE IF EXISTS mrconso;")
            cur.execute("DROP TABLE IF EXISTS mrsty;")
            print("Tables dropped.")

            print("Creating table: mrconso")
            # Ensure these column names match PG_MRCONSO_TARGET_COLS exactly
            cur.execute("""
                CREATE TABLE mrconso (
                    CUI CHAR(8) NOT NULL,
                    LAT CHAR(3) NOT NULL,
                    AUI VARCHAR(9) PRIMARY KEY, -- AUI is unique
                    SAB VARCHAR(40) NOT NULL,  -- Increased length based on common values
                    TTY VARCHAR(20) NOT NULL,
                    CODE VARCHAR(100) NOT NULL, -- Increased length for various source codes
                    STR TEXT NOT NULL
                );
            """)
            print("Creating table: mrsty")
            # Ensure these column names match PG_MRSTY_TARGET_COLS exactly
            cur.execute("""
                CREATE TABLE mrsty (
                    CUI CHAR(8) NOT NULL,
                    TUI CHAR(4) NOT NULL,
                    STY VARCHAR(100) NOT NULL, -- Semantic Type Name
                    PRIMARY KEY (CUI, TUI) -- Composite primary key
                );
            """)
            conn.commit()
            print("Tables created successfully.")
            return True
    except psycopg2.DatabaseError as e:
        print(f"Database error during table creation: {e}", file=sys.stderr)
        conn.rollback() # Roll back changes on error
        return False
    except Exception as e:
        print(f"An unexpected error occurred during table creation: {e}", file=sys.stderr)
        conn.rollback()
        return False


def load_data_pgsql(conn, filepath, table_name, rrf_cols, target_cols_lookup, copy_cols_sql_str, filter_col=None, filter_val=None):
    """
    Loads data from an RRF file into a PostgreSQL table using COPY FROM STDIN.
    Optionally filters rows based on a specific column value.
    """
    if conn is None:
        print(f"Error: No database connection provided for loading {table_name}.", file=sys.stderr)
        return False

    if not check_file_exists(filepath):
        return False

    print(f"Starting data load for {table_name} from {filepath}...")
    start_time = time.time()
    rows_processed = 0
    rows_loaded = 0
    buffer_size = 100000 # Process in chunks

    # Construct the full COPY SQL command - ensure copy_cols_sql_str is correct here
    copy_sql = f"COPY {sql.Identifier(table_name).string} ({copy_cols_sql_str}) FROM STDIN WITH (FORMAT TEXT, DELIMITER '|', NULL '', ENCODING 'UTF8')"
    print(f"Executing COPY command structure: {copy_sql}") # Debug print

    target_indices = list(target_cols_lookup.values())
    filter_idx = rrf_cols.index(filter_col) if filter_col else -1

    try:
        with open(filepath, 'r', encoding='utf-8') as infile, conn.cursor() as cur:
            data_buffer = io.StringIO()
            # Use csv.reader to handle potential quoting issues, though RRF usually doesn't quote
            reader = csv.reader(infile, delimiter='|', quoting=csv.QUOTE_NONE, lineterminator='\n')

            for row in reader:
                rows_processed += 1
                # RRF files end with a delimiter and newline (| \n), csv.reader might produce an extra empty field
                # Check if the last field is empty and if the number of fields is one more than expected
                if len(row) == len(rrf_cols) + 1 and row[-1] == '':
                    row = row[:-1] # Remove the trailing empty field

                if len(row) != len(rrf_cols): # Check exact length after potential correction
                    print(f"Warning: Skipping row {rows_processed} in {filepath} due to unexpected number of fields ({len(row)} instead of {len(rrf_cols)}): {row}", file=sys.stderr)
                    continue

                # Apply filter if specified
                try:
                    if filter_idx != -1 and row[filter_idx] != filter_val:
                        continue
                except IndexError:
                     print(f"Warning: Skipping row {rows_processed} due to index error during filtering (Index: {filter_idx}): {row}", file=sys.stderr)
                     continue

                # Select and order the target columns for COPY
                try:
                    target_row_data = [row[i] for i in target_indices]
                except IndexError:
                    print(f"Warning: Skipping row {rows_processed} due to index error selecting target columns (Indices: {target_indices}): {row}", file=sys.stderr)
                    continue

                # Write processed row to buffer, properly handling delimiters and special characters for COPY TEXT format
                # Need to escape backslash (\), newline (\n), carriage return (\r), and the delimiter (|)
                processed_line = "|".join(item.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('|', '\\|') for item in target_row_data)
                data_buffer.write(processed_line + "\n")
                rows_loaded += 1

                # When buffer is full, execute COPY
                if rows_loaded > 0 and rows_loaded % buffer_size == 0:
                    data_buffer.seek(0)
                    cur.copy_expert(sql=copy_sql, file=data_buffer)
                    conn.commit() # Commit chunk
                    data_buffer.close() # Close old buffer
                    data_buffer = io.StringIO() # Reset buffer
                    elapsed_time = time.time() - start_time
                    print(f"  {table_name}: Loaded {rows_loaded} rows... ({elapsed_time:.1f}s)", end='\r')

            # Load any remaining data in the buffer
            data_buffer.seek(0)
            if data_buffer.tell() > 0: # Check if there's anything left
                 cur.copy_expert(sql=copy_sql, file=data_buffer)
                 conn.commit() # Commit final chunk
            data_buffer.close()

        end_time = time.time()
        print(f"\nFinished loading {table_name}. Processed {rows_processed} lines, loaded {rows_loaded} rows in {end_time - start_time:.2f} seconds.")
        return True

    except psycopg2.DatabaseError as e:
        conn.rollback() # Rollback before printing error
        print(f"\nDatabase error during {table_name} load: {e}", file=sys.stderr)
        print(f"Error occurred processing data around source row {rows_processed}.", file=sys.stderr)
        # Optionally print the problematic buffer content if small enough
        # data_buffer.seek(0)
        # print(f"Buffer content near error:\n{data_buffer.read(500)}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"\nError: File not found during load: {filepath}", file=sys.stderr)
        # No rollback needed as transaction likely didn't start
        return False
    except Exception as e:
        conn.rollback() # Rollback on generic error too
        print(f"\nAn unexpected error occurred during {table_name} load: {e}", file=sys.stderr)
        print(f"Error occurred processing data around source row {rows_processed}.", file=sys.stderr)
        import traceback
        traceback.print_exc() # Print full traceback for unexpected errors
        return False
    finally:
        if 'data_buffer' in locals() and not data_buffer.closed:
             data_buffer.close()

def create_indexes(conn):
    """Creates indexes on the tables for faster lookups."""
    if conn is None:
        print("Error: No database connection provided to create_indexes.", file=sys.stderr)
        return False
    start_time = time.time()
    try:
        with conn.cursor() as cur:
            print("Creating indexes on mrconso...")
            # Index for looking up concepts by name (case-insensitive search often useful)
            print("  Creating index idx_mrconso_str_lower...")
            # Ensure pg_trgm extension is enabled before running this
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrconso_str_lower ON mrconso USING gin (lower(STR) gin_trgm_ops);")
            conn.commit() # Commit after each index potentially
            print("  Creating index idx_mrconso_cui...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrconso_cui ON mrconso (CUI);")
            conn.commit()
             # Index for SAB (Source Vocabulary) lookups if frequently filtering by source
            print("  Creating index idx_mrconso_sab...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrconso_sab ON mrconso (SAB);")
            conn.commit()

            print("Creating indexes on mrsty...")
            # Primary key already covers (CUI, TUI) lookups
            # Index for looking up CUIs by Semantic Type TUI or STY
            print("  Creating index idx_mrsty_tui...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrsty_tui ON mrsty (TUI);")
            conn.commit()
            print("  Creating index idx_mrsty_sty...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrsty_sty ON mrsty (STY);")
            conn.commit()

            end_time = time.time()
            print(f"Indexes created successfully in {end_time - start_time:.2f} seconds.")
            return True
    except psycopg2.DatabaseError as e:
        print(f"Database error during index creation: {e}", file=sys.stderr)
        conn.rollback()
        # Check if the error is about pg_trgm extension missing
        if "extension" in str(e).lower() and "pg_trgm" in str(e).lower():
             print("Hint: The GIN trigram index requires the 'pg_trgm' extension.", file=sys.stderr)
             print("Hint: Connect to the DB as superuser and run: CREATE EXTENSION IF NOT EXISTS pg_trgm;", file=sys.stderr)
        elif "must be superuser" in str(e).lower() and "create extension" in str(e).lower():
             print("Hint: The user specified in POSTGRES_USER needs superuser privileges or specific permission to create extensions.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"An unexpected error occurred during index creation: {e}", file=sys.stderr)
        conn.rollback()
        return False

# --- Main Execution Logic ---
if __name__ == "__main__":
    print("--- Starting UMLS Data Load into PostgreSQL ---")
    overall_success = False # Default to failure

    # Check if the success flag file already exists
    if os.path.exists(LOAD_SUCCESS_FLAG_FILE):
        print(f"Success flag file found ({LOAD_SUCCESS_FLAG_FILE}). Assuming data is already loaded. Skipping load.")
        print("--- UMLS Load Script finished (skipped). ---")
        sys.exit(0) # Exit successfully as data is presumed loaded

    # --- Establish DB Connection ---
    # Renamed function call based on user request
    conn = get_db_connection() # Imported function

    if conn is None:
        print("Exiting: Could not establish database connection.", file=sys.stderr)
        sys.exit(1) # Exit with error code
    else:
        print("Database connection established successfully.")
        # Enable pg_trgm extension if not already enabled (requires sufficient privileges)
        try:
            with conn.cursor() as cur:
                print("Ensuring pg_trgm extension is enabled...")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
                conn.commit()
                print("pg_trgm extension is enabled.")
        except psycopg2.Error as ext_err:
             conn.rollback() # Rollback the failed extension command
             print(f"Warning: Could not ensure pg_trgm extension is enabled: {ext_err}", file=sys.stderr)
             print("         GIN index on mrconso.STR might fail or be less efficient.", file=sys.stderr)
             # Check if it's a permissions issue
             if "must be superuser" in str(ext_err).lower() or "permission denied" in str(ext_err).lower():
                 print("Hint: The database user may lack privileges to CREATE EXTENSION.", file=sys.stderr)
                 print("Hint: You might need to connect as a PostgreSQL superuser (e.g., 'postgres') and run 'CREATE EXTENSION pg_trgm;' manually in the 'umls_db' database.", file=sys.stderr)


    try:
        # --- Step 1: Create Tables ---
        print("\nStep 1: Creating Tables...")
        if not create_tables(conn):
            print("Exiting: Table creation failed.", file=sys.stderr)
            raise RuntimeError("Table creation failed") # Raise exception to jump to finally block

        # --- Step 2: Load MRCONSO Data ---
        print("\nStep 2: Loading MRCONSO...")
        if not load_data_pgsql(conn, MRCONSO_FILE, 'mrconso', MRCONSO_COLS, PG_MRCONSO_TARGET_COLS_LOOKUP, PG_MRCONSO_COPY_COLS_SQL, filter_col='LAT', filter_val=TARGET_LANG):
            print("Exiting: MRCONSO load failed.", file=sys.stderr)
            raise RuntimeError("MRCONSO load failed") # Raise exception

        # --- Step 3: Load MRSTY Data ---
        print("\nStep 3: Loading MRSTY...")
        if not load_data_pgsql(conn, MRSTY_FILE, 'mrsty', MRSTY_COLS, PG_MRSTY_TARGET_COLS_LOOKUP, PG_MRSTY_COPY_COLS_SQL):
            print("Exiting: MRSTY load failed.", file=sys.stderr)
            raise RuntimeError("MRSTY load failed") # Raise exception

        # --- Step 4: Create Indexes ---
        print("\nStep 4: Creating Indexes...")
        if not create_indexes(conn):
            print("Exiting: Index creation failed.", file=sys.stderr)
            raise RuntimeError("Index creation failed") # Raise exception

        # If all steps passed without raising an exception
        overall_success = True
        print("\n--- All data loading and indexing steps completed successfully. ---")

    except (Exception, psycopg2.DatabaseError, RuntimeError) as error:
        print(f"\n--- An error occurred during the data loading process: {error} ---", file=sys.stderr)
        # Avoid printing full traceback for expected RuntimeErrors, but maybe for others
        if not isinstance(error, RuntimeError):
             import traceback
             traceback.print_exc()
        overall_success = False # Ensure it's marked as failure

    finally:
        # --- Close DB Connection ---
        if conn is not None:
            conn.close()
            print("Database connection closed.")

        # --- Create Flag File ONLY on Overall Success ---
        if overall_success:
            print(f"Attempting to create success flag file: {LOAD_SUCCESS_FLAG_FILE}")
            try:
                # Ensure the directory exists (useful if /tmp gets cleared unexpectedly)
                os.makedirs(os.path.dirname(LOAD_SUCCESS_FLAG_FILE), exist_ok=True)
                with open(LOAD_SUCCESS_FLAG_FILE, 'w') as f:
                    f.write(f"Load completed successfully at: {time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
                print("Success flag file created.")
            except IOError as e:
                print(f"Warning: Could not create success flag file '{LOAD_SUCCESS_FLAG_FILE}': {e}", file=sys.stderr)
            # --- End Flag File Creation ---

            print("--- UMLS Load Script finished SUCCESSFULLY. ---")
            sys.exit(0) # Explicitly exit with success code
        else:
            print("--- UMLS Load Script finished with ERRORS. ---", file=sys.stderr)
            # Optionally remove flag file if it somehow exists from a previous failed run
            if os.path.exists(LOAD_SUCCESS_FLAG_FILE):
                print(f"Removing potentially incomplete flag file: {LOAD_SUCCESS_FLAG_FILE}")
                try:
                    os.remove(LOAD_SUCCESS_FLAG_FILE)
                except OSError as e:
                     print(f"Warning: Could not remove flag file '{LOAD_SUCCESS_FLAG_FILE}': {e}", file=sys.stderr)
            sys.exit(1) # Explicitly exit with error code