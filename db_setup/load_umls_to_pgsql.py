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
# Adjust the path if your project structure is different
module_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if module_dir not in sys.path:
    sys.path.insert(0, module_dir)

try:
    from scripts.db_utils import get_db_connection
except ImportError as e:
    print(f"Error importing get_db_connection from scripts.db_utils: {e}", file=sys.stderr)
    print(f"Searched in sys.path including: {module_dir}", file=sys.stderr)
    print("Please ensure scripts/db_utils.py exists and defines the get_db_connection function.", file=sys.stderr)
    sys.exit(1)
# ---------------------------------------------

# --- Configuration (Load from Environment Variables or defaults) ---
DB_NAME = os.getenv('POSTGRES_DB', 'umls_postgres_db') # Match docker-compose
DB_USER = os.getenv('POSTGRES_USER', 'test_user')     # Match docker-compose
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD')
DB_HOST = os.getenv('DB_HOST', 'db')
DB_PORT = os.getenv('DB_PORT', '5432')

# Source Data File Paths (relative to the mount point inside the container)
UMLS_META_PATH = '/app/data/source/umls/META'
MRCONSO_FILE = os.path.join(UMLS_META_PATH, 'MRCONSO.RRF')
MRSTY_FILE = os.path.join(UMLS_META_PATH, 'MRSTY.RRF')
MRREL_FILE = os.path.join(UMLS_META_PATH, 'MRREL.RRF') # <<< Added MRREL file

# Target language for MRCONSO
TARGET_LANG = 'ENG'

# Flag file to prevent re-loading
LOAD_SUCCESS_FLAG_FILE = "/tmp/umls_load_complete.flag"

# --- Column Definitions for RRF files ---
MRCONSO_COLS = ['CUI', 'LAT', 'TS', 'LUI', 'STT', 'SUI', 'ISPREF', 'AUI', 'SAUI', 'SCUI', 'SDUI', 'SAB', 'TTY', 'CODE', 'STR', 'SRL', 'SUPPRESS', 'CVF']
MRSTY_COLS = ['CUI', 'TUI', 'STN', 'STY', 'ATUI', 'CVF']
# Common MRREL columns (check your UMLS version documentation for specifics)
MRREL_COLS = ['CUI1', 'AUI1', 'STYPE1', 'REL', 'CUI2', 'AUI2', 'STYPE2', 'RELA', 'RUI', 'SRUI', 'SAB', 'SL', 'RG', 'DIR', 'SUPPRESS', 'CVF']

# --- Target Table Column Definitions in PostgreSQL ---
PG_MRCONSO_TARGET_COLS = ['CUI', 'LAT', 'AUI', 'SAB', 'TTY', 'CODE', 'STR']
PG_MRSTY_TARGET_COLS = ['CUI', 'TUI', 'STY']
# Select key columns for relation extraction. Add more if needed (e.g., AUI1/2, STYPE1/2 if joining required later)
PG_MRREL_TARGET_COLS = ['CUI1', 'CUI2', 'REL', 'RELA', 'SAB'] # <<< Added MRREL target cols

# Create lookup dictionaries for column indices
MRCONSO_COLS_LOOKUP = {name: idx for idx, name in enumerate(MRCONSO_COLS)}
MRSTY_COLS_LOOKUP = {name: idx for idx, name in enumerate(MRSTY_COLS)}
MRREL_COLS_LOOKUP = {name: idx for idx, name in enumerate(MRREL_COLS)} # <<< Added MRREL lookup

# Lookup for target PostgreSQL column indices relative to RRF columns
PG_MRCONSO_TARGET_COLS_LOOKUP = {col: MRCONSO_COLS_LOOKUP[col] for col in PG_MRCONSO_TARGET_COLS}
PG_MRSTY_TARGET_COLS_LOOKUP = {col: MRSTY_COLS_LOOKUP[col] for col in PG_MRSTY_TARGET_COLS}
PG_MRREL_TARGET_COLS_LOOKUP = {col: MRREL_COLS_LOOKUP[col] for col in PG_MRREL_TARGET_COLS} # <<< Added MRREL lookup

# --- SQL Strings for COPY command ---
PG_MRCONSO_COPY_COLS_SQL = ", ".join([sql.Identifier(col).strings[0] for col in PG_MRCONSO_TARGET_COLS])
PG_MRSTY_COPY_COLS_SQL = ", ".join([sql.Identifier(col).strings[0] for col in PG_MRSTY_TARGET_COLS])
PG_MRREL_COPY_COLS_SQL = ", ".join([sql.Identifier(col).strings[0] for col in PG_MRREL_TARGET_COLS]) # <<< Added MRREL SQL

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
    """Creates the necessary tables (mrconso, mrsty, mrrel) if they don't exist."""
    if conn is None:
        print("Error: No database connection provided to create_tables.", file=sys.stderr)
        return False
    try:
        with conn.cursor() as cur:
            print("Dropping existing tables (if any)...")
            cur.execute("DROP TABLE IF EXISTS mrconso;")
            cur.execute("DROP TABLE IF EXISTS mrsty;")
            cur.execute("DROP TABLE IF EXISTS mrrel;") # <<< Added drop mrrel
            print("Tables dropped.")

            print("Creating table: mrconso")
            cur.execute("""
                CREATE TABLE mrconso (
                    CUI CHAR(8) NOT NULL,
                    LAT CHAR(3) NOT NULL,
                    AUI VARCHAR(9) PRIMARY KEY,
                    SAB VARCHAR(40) NOT NULL,
                    TTY VARCHAR(20) NOT NULL,
                    CODE VARCHAR(100) NOT NULL,
                    STR TEXT NOT NULL
                );
            """)
            print("Creating table: mrsty")
            cur.execute("""
                CREATE TABLE mrsty (
                    CUI CHAR(8) NOT NULL,
                    TUI CHAR(4) NOT NULL,
                    STY VARCHAR(100) NOT NULL,
                    PRIMARY KEY (CUI, TUI)
                );
            """)
            print("Creating table: mrrel") # <<< Added create mrrel
            cur.execute("""
                CREATE TABLE mrrel (
                    CUI1 CHAR(8) NOT NULL,
                    CUI2 CHAR(8) NOT NULL,
                    REL VARCHAR(100),        -- Relationship name (broader category)
                    RELA VARCHAR(100),       -- Relationship attribute (more specific)
                    SAB VARCHAR(40) NOT NULL -- Source vocabulary of the relationship
                    -- Consider adding RUI or a unique ID if needed, but complex
                    -- Adding a PRIMARY KEY or UNIQUE constraint might be difficult
                    -- due to potential duplicates across sources or variations.
                    -- Indexes will be added later for querying.
                );
            """)
            conn.commit()
            print("Tables created successfully.")
            return True
    except psycopg2.DatabaseError as e:
        print(f"Database error during table creation: {e}", file=sys.stderr)
        conn.rollback()
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
    buffer_size = 100000 # Adjust buffer size based on available memory

    # Construct the full COPY SQL command
    copy_sql = f"COPY {sql.Identifier(table_name).string} ({copy_cols_sql_str}) FROM STDIN WITH (FORMAT TEXT, DELIMITER '|', NULL '', ENCODING 'UTF8')"
    # print(f"Executing COPY command structure: {copy_sql}") # Uncomment for debug

    target_indices = list(target_cols_lookup.values())
    filter_idx = rrf_cols.index(filter_col) if filter_col else -1

    try:
        with open(filepath, 'r', encoding='utf-8') as infile, conn.cursor() as cur:
            data_buffer = io.StringIO()
            # Using csv.reader for robust handling of potential edge cases in RRF format
            reader = csv.reader(infile, delimiter='|', quoting=csv.QUOTE_NONE, lineterminator='\n')

            for row in reader:
                rows_processed += 1
                # RRF files often end with a delimiter (|), csv.reader might add an extra empty string field
                if len(row) == len(rrf_cols) + 1 and row[-1] == '':
                    row = row[:-1]

                if len(row) != len(rrf_cols):
                    # Don't stop the whole load, just log problematic rows
                    # For MRREL, sometimes fields can be empty, but the number should still match
                    print(f"Warning: Skipping row {rows_processed} in {filepath} due to unexpected number of fields ({len(row)} vs {len(rrf_cols)}): {row[:50]}...", file=sys.stderr)
                    continue

                # Apply filter if specified (e.g., for MRCONSO LAT)
                try:
                    if filter_idx != -1 and row[filter_idx] != filter_val:
                        continue
                except IndexError:
                     print(f"Warning: Skipping row {rows_processed} due to index error during filtering (Index: {filter_idx}): {row[:50]}...", file=sys.stderr)
                     continue

                # Select and order the target columns for COPY
                try:
                    # Handle potential empty strings for columns like RELA which might be null
                    target_row_data = [(row[i] if row[i] is not None else '') for i in target_indices]
                except IndexError:
                    print(f"Warning: Skipping row {rows_processed} due to index error selecting target columns (Indices: {target_indices}): {row[:50]}...", file=sys.stderr)
                    continue

                # Write processed row to buffer, escaping necessary characters for COPY TEXT format
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
            if data_buffer.tell() > 0:
                 cur.copy_expert(sql=copy_sql, file=data_buffer)
                 conn.commit() # Commit final chunk
            data_buffer.close()

        end_time = time.time()
        # Clear the loading progress line before printing final summary
        print(' ' * 80, end='\r')
        print(f"Finished loading {table_name}. Processed {rows_processed} lines, loaded {rows_loaded} rows in {end_time - start_time:.2f} seconds.")
        return True

    except psycopg2.DatabaseError as e:
        conn.rollback()
        print(f"\nDatabase error during {table_name} load: {e}", file=sys.stderr)
        print(f"Error occurred processing data around source row {rows_processed}.", file=sys.stderr)
        return False
    except FileNotFoundError:
        print(f"\nError: File not found during load: {filepath}", file=sys.stderr)
        return False
    except Exception as e:
        conn.rollback()
        print(f"\nAn unexpected error occurred during {table_name} load: {e}", file=sys.stderr)
        print(f"Error occurred processing data around source row {rows_processed}.", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return False
    finally:
        if 'data_buffer' in locals() and data_buffer and not data_buffer.closed:
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
            print("  Creating index idx_mrconso_str_lower (GIN)...")
            # Assumes pg_trgm is enabled (attempted in main block)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrconso_str_lower ON mrconso USING gin (lower(STR) gin_trgm_ops);")
            print("  Creating index idx_mrconso_cui...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrconso_cui ON mrconso (CUI);")
            print("  Creating index idx_mrconso_sab...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrconso_sab ON mrconso (SAB);")
            conn.commit() # Commit after mrconso indexes

            print("Creating indexes on mrsty...")
            # Primary key already covers (CUI, TUI)
            print("  Creating index idx_mrsty_tui...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrsty_tui ON mrsty (TUI);")
            print("  Creating index idx_mrsty_sty...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrsty_sty ON mrsty (STY);")
            conn.commit() # Commit after mrsty indexes

            print("Creating indexes on mrrel...") # <<< Added mrrel indexes
            print("  Creating index idx_mrrel_cui1...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrrel_cui1 ON mrrel (CUI1);")
            print("  Creating index idx_mrrel_cui2...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrrel_cui2 ON mrrel (CUI2);")
            print("  Creating index idx_mrrel_sab...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrrel_sab ON mrrel (SAB);")
            print("  Creating index idx_mrrel_rela...")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrrel_rela ON mrrel (RELA);") # If filtering by specific relation types
            print("  Creating composite index idx_mrrel_cui1_cui2...") # For pair lookups
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mrrel_cui1_cui2 ON mrrel (CUI1, CUI2);")
            conn.commit() # Commit after mrrel indexes

            end_time = time.time()
            print(f"Indexes created successfully in {end_time - start_time:.2f} seconds.")
            return True
    except psycopg2.DatabaseError as e:
        print(f"Database error during index creation: {e}", file=sys.stderr)
        conn.rollback()
        if "extension" in str(e).lower() and "pg_trgm" in str(e).lower():
             print("Hint: The GIN trigram index requires the 'pg_trgm' extension.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"An unexpected error occurred during index creation: {e}", file=sys.stderr)
        conn.rollback()
        return False

# --- Main Execution Logic ---
if __name__ == "__main__":
    print("--- Starting UMLS Data Load into PostgreSQL ---")
    overall_success = False # Assume failure unless all steps complete

    # Check if the success flag file already exists
    if os.path.exists(LOAD_SUCCESS_FLAG_FILE):
        print(f"Success flag file found ({LOAD_SUCCESS_FLAG_FILE}). Assuming data is already loaded. Skipping load.")
        print("--- UMLS Load Script finished (skipped). ---")
        sys.exit(0)

    # Basic file checks before connecting to DB
    if not all(check_file_exists(f) for f in [MRCONSO_FILE, MRSTY_FILE, MRREL_FILE]):
         print("Error: One or more required RRF files not found or not readable. Please check paths and permissions.", file=sys.stderr)
         print("--- UMLS Load Script finished with ERRORS. ---", file=sys.stderr)
         sys.exit(1)

    # --- Establish DB Connection ---
    conn = get_db_connection()

    if conn is None:
        print("Exiting: Could not establish database connection.", file=sys.stderr)
        sys.exit(1)
    else:
        print("Database connection established successfully.")
        # Attempt to enable pg_trgm extension
        try:
            with conn.cursor() as cur:
                print("Ensuring pg_trgm extension is enabled...")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
                conn.commit()
                print("pg_trgm extension is enabled.")
        except psycopg2.Error as ext_err:
             conn.rollback()
             print(f"Warning: Could not ensure pg_trgm extension is enabled: {ext_err}", file=sys.stderr)
             print("         GIN index on mrconso.STR might fail or be less efficient.", file=sys.stderr)
             if "must be superuser" in str(ext_err).lower() or "permission denied" in str(ext_err).lower():
                 print("Hint: The database user may lack privileges to CREATE EXTENSION.", file=sys.stderr)

    try:
        # --- Step 1: Create Tables ---
        print("\nStep 1: Creating Tables...")
        if not create_tables(conn):
            raise RuntimeError("Table creation failed")

        # --- Step 2: Load MRCONSO Data ---
        print("\nStep 2: Loading MRCONSO...")
        if not load_data_pgsql(conn, MRCONSO_FILE, 'mrconso', MRCONSO_COLS, PG_MRCONSO_TARGET_COLS_LOOKUP, PG_MRCONSO_COPY_COLS_SQL, filter_col='LAT', filter_val=TARGET_LANG):
            raise RuntimeError("MRCONSO load failed")

        # --- Step 3: Load MRSTY Data ---
        print("\nStep 3: Loading MRSTY...")
        if not load_data_pgsql(conn, MRSTY_FILE, 'mrsty', MRSTY_COLS, PG_MRSTY_TARGET_COLS_LOOKUP, PG_MRSTY_COPY_COLS_SQL):
            raise RuntimeError("MRSTY load failed")

        # --- Step 4: Load MRREL Data --- <<< Added MRREL load step
        print("\nStep 4: Loading MRREL...")
        # No language filter usually needed for MRREL, loading all relationships
        if not load_data_pgsql(conn, MRREL_FILE, 'mrrel', MRREL_COLS, PG_MRREL_TARGET_COLS_LOOKUP, PG_MRREL_COPY_COLS_SQL):
            raise RuntimeError("MRREL load failed")

        # --- Step 5: Create Indexes --- (Renumbered)
        print("\nStep 5: Creating Indexes...")
        if not create_indexes(conn):
            raise RuntimeError("Index creation failed")

        overall_success = True
        print("\n--- All data loading and indexing steps completed successfully. ---")

    except (Exception, psycopg2.DatabaseError, RuntimeError) as error:
        print(f"\n--- An error occurred during the data loading process: {error} ---", file=sys.stderr)
        if not isinstance(error, RuntimeError):
             import traceback
             traceback.print_exc()
        overall_success = False

    finally:
        # --- Close DB Connection ---
        if conn is not None:
            conn.close()
            print("Database connection closed.")

        # --- Create Flag File ONLY on Overall Success ---
        if overall_success:
            print(f"Attempting to create success flag file: {LOAD_SUCCESS_FLAG_FILE}")
            try:
                os.makedirs(os.path.dirname(LOAD_SUCCESS_FLAG_FILE), exist_ok=True)
                with open(LOAD_SUCCESS_FLAG_FILE, 'w') as f:
                    f.write(f"Load completed successfully at: {time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n")
                print("Success flag file created.")
            except IOError as e:
                print(f"Warning: Could not create success flag file '{LOAD_SUCCESS_FLAG_FILE}': {e}", file=sys.stderr)

            print("--- UMLS Load Script finished SUCCESSFULLY. ---")
            sys.exit(0)
        else:
            print("--- UMLS Load Script finished with ERRORS. ---", file=sys.stderr)
            # Ensure flag file doesn't exist if load failed
            if os.path.exists(LOAD_SUCCESS_FLAG_FILE):
                print(f"Removing potentially incomplete flag file: {LOAD_SUCCESS_FLAG_FILE}")
                try:
                    os.remove(LOAD_SUCCESS_FLAG_FILE)
                except OSError as e:
                     print(f"Warning: Could not remove flag file '{LOAD_SUCCESS_FLAG_FILE}': {e}", file=sys.stderr)
            sys.exit(1)