# --- START OF FILE db_utils.py ---

# /app/scripts/db_utils.py
import os
import psycopg2
import sys
import logging # Import logging

# Configure logging
# You might want to configure this globally in your main app entry point eventually
# For now, basic config here is okay for direct script execution.
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), # Default to INFO, can be set via env var
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Get logger for this module

# --- Database Configuration (Read directly from environment) ---
DB_NAME = os.getenv('POSTGRES_DB', 'umls_db')
DB_USER = os.getenv('POSTGRES_USER', 'umls_user')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD') # Make sure this is set
DB_HOST = os.getenv('DB_HOST', 'db')
DB_PORT = os.getenv('DB_PORT', '5432')

if not DB_PASSWORD:
    logger.error("FATAL: POSTGRES_PASSWORD environment variable not set.")
    # Decide how to handle this - exit or let connection fail later?
    # Consider raising an exception or exiting if this is critical
    # sys.exit(1)

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    conn = None
    if not DB_PASSWORD: # Prevent connection attempt if password missing
        logger.error("Cannot connect to DB: POSTGRES_PASSWORD not set.")
        return None
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
        logger.debug("DB connection successful in db_utils")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Error connecting to database in db_utils: {e}")
        # Log details used, hiding password
        logger.error(f"  Attempted connection to: host={DB_HOST}, port={DB_PORT}, dbname={DB_NAME}, user={DB_USER}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred getting DB connection in db_utils: {e}")
        return None

# --- Database Utility Functions ---

# MODIFIED FUNCTION
def find_concept_by_term_and_source(term: str,
                                    source_sab: str, # Renamed from source_vocab for clarity
                                    exact_match: bool = True,
                                    case_sensitive: bool = False,
                                    limit: int = 5): # Added limit parameter
    """
    Finds concepts in MRCONSO by term, filtering by source vocabulary (SAB).

    Args:
        term: The term string to search for.
        source_sab: The source abbreviation (SAB) to filter by (e.g., 'SNOMEDCT_US').
        exact_match: If True, looks for an exact match. If False, uses LIKE.
        case_sensitive: If True, matching is case-sensitive. Default False.
        limit: Maximum number of results to return.

    Returns:
        A list of tuples, where each tuple is (cui, matched_term, code).
        Returns an empty list if no match or error.
    """
    conn = None
    results = []
    try:
        conn = get_db_connection()
        if not conn:
            return results # Return empty list if connection failed

        with conn.cursor() as cur:
            # Basic sanitization (consider more robust methods if needed)
            term_param = term.strip()
            sab_param = source_sab.strip()

            # Build the WHERE clause based on parameters
            if exact_match:
                if case_sensitive:
                    sql = """
                        SELECT cui, str, code
                        FROM mrconso
                        WHERE str = %s AND sab = %s AND lat = 'ENG'
                        LIMIT %s;
                    """
                    params = (term_param, sab_param, limit)
                else:
                    # Use LOWER() for case-insensitive exact match
                    sql = """
                        SELECT cui, str, code
                        FROM mrconso
                        WHERE LOWER(str) = LOWER(%s) AND sab = %s AND lat = 'ENG'
                        LIMIT %s;
                    """
                    params = (term_param, sab_param, limit)
            else: # Partial match (LIKE)
                term_param_like = f"%{term_param}%" # Add wildcards for LIKE
                if case_sensitive: # Less common for LIKE search
                     sql = """
                        SELECT cui, str, code
                        FROM mrconso
                        WHERE str LIKE %s AND sab = %s AND lat = 'ENG'
                        LIMIT %s;
                    """
                     params = (term_param_like, sab_param, limit)
                else:
                     # Use LOWER() for case-insensitive partial match
                     sql = """
                        SELECT cui, str, code
                        FROM mrconso
                        WHERE LOWER(str) LIKE LOWER(%s) AND sab = %s AND lat = 'ENG'
                        LIMIT %s;
                    """
                     params = (term_param_like, sab_param, limit)

            logger.debug(f"Executing SQL: {cur.mogrify(sql, params).decode('utf-8')}") # Decode for clean logging
            cur.execute(sql, params)
            # Fetch results directly as tuples, matching process_phrase.py expectation
            results = cur.fetchall()

    except psycopg2.Error as e:
        logger.error(f"Database error in find_concept_by_term_and_source: {e}")
        # Optional: rollback if using transactions: if conn: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error in find_concept_by_term_and_source: {e}")
    finally:
        if conn:
            conn.close()
    logger.debug(f"Found {len(results)} concepts for term '{term}' with SAB '{source_sab}'")
    return results

# MODIFIED FUNCTION
def get_semantic_types_for_cui(cui: str):
    """
    Retrieves semantic types (TUI and STY name) for a given CUI.

    Args:
        cui: The Concept Unique Identifier (CUI).

    Returns:
        A list of tuples, where each tuple is (tui, type_name).
        Returns an empty list if no types found or error.
    """
    conn = None # Initialize conn to None
    results = []
    try:
        conn = get_db_connection()
        if not conn:
            return []

        with conn.cursor() as cur:
            query = """
                SELECT TUI, STY
                FROM mrsty
                WHERE CUI = %s;
            """
            logger.debug(f"Executing SQL: {cur.mogrify(query, (cui,)).decode('utf-8')}")
            cur.execute(query, (cui,))
            # Fetch results directly as tuples, matching process_phrase.py expectation
            results = cur.fetchall()
    except psycopg2.Error as e:
        logger.error(f"Database query error in get_semantic_types_for_cui for CUI {cui}: {e}")
        # Optional: if conn: conn.rollback()
    except Exception as e:
         logger.error(f"Unexpected error in get_semantic_types_for_cui for CUI {cui}: {e}")
    finally:
        if conn:
            conn.close()
    logger.debug(f"Found {len(results)} semantic types for CUI {cui}")
    return results

# --- Add a main block for testing db_utils.py directly ---
# UPDATED TEST BLOCK
if __name__ == "__main__":
    logger.info("--- Testing db_utils functions ---")

    test_term = "Lung cancer"
    test_sab = "SNOMEDCT_US"
    logger.info(f"Searching for term: '{test_term}' in {test_sab} (case-insensitive exact)")
    # Note: Changed exact_match to True and case_sensitive to False to match common use case
    concepts = find_concept_by_term_and_source(term=test_term,
                                               source_sab=test_sab,
                                               exact_match=True,
                                               case_sensitive=False)

    if concepts:
        print("Found concepts:") # Using print for direct test output clarity
        # concepts is now list of tuples: (cui, matched_term, code)
        for cui, matched_term, code in concepts:
            print(f"  CUI: {cui}, Term: {matched_term}, Code: {code}")
            print(f"    Getting semantic types for CUI: {cui}")
            # sem_types is now list of tuples: (tui, type_name)
            sem_types = get_semantic_types_for_cui(cui)
            if sem_types:
                for tui, type_name in sem_types:
                    print(f"      TUI: {tui}, Type: {type_name}")
            else:
                print("      No semantic types found.")
            # Only check sem types for the first found concept for brevity in testing
            break
    else:
        print(f"No concepts found for '{test_term}' in {test_sab}.")

    logger.info("--- db_utils test finished ---")

# --- END OF FILE db_utils.py ---