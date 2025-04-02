# /app/scripts/db_utils.py
import os
import psycopg2
import sys
# DO NOT import anything from db_setup or load_umls_to_pgsql here

# --- Database Configuration (Read directly from environment) ---
DB_NAME = os.getenv('POSTGRES_DB', 'umls_db')
DB_USER = os.getenv('POSTGRES_USER', 'umls_user')
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD') # Make sure this is set
DB_HOST = os.getenv('DB_HOST', 'db')
DB_PORT = os.getenv('DB_PORT', '5432')

if not DB_PASSWORD:
    print("Error: POSTGRES_PASSWORD environment variable not set.", file=sys.stderr)
    # Decide how to handle this - exit or let connection fail later?
    # sys.exit(1) # Or maybe just print warning and return None below

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    conn = None
    try:
        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            host=DB_HOST,
            port=DB_PORT
        )
        # Optional: print("DB connection successful in db_utils")
        return conn
    except psycopg2.OperationalError as e:
        print(f"Error connecting to database in db_utils: {e}", file=sys.stderr)
        # Print details used, hiding password
        print(f"  Attempted connection to: host={DB_HOST}, port={DB_PORT}, dbname={DB_NAME}, user={DB_USER}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"An unexpected error occurred getting DB connection in db_utils: {e}", file=sys.stderr)
        return None

# --- Other Database Utility Functions ---

def find_concept_by_term_and_source(term, source_vocab='SNOMEDCT_US', exact_match=True):
    """
    Finds UMLS concepts (CUI) matching a given term string,
    optionally filtering by source vocabulary (SAB).
    Uses case-insensitive search by default if exact_match is False.
    """
    conn = get_db_connection()
    if conn is None:
        return [] # Return empty list if connection failed

    results = []
    try:
        with conn.cursor() as cur:
            # Use lower() and LIKE for case-insensitive partial match
            # Use = for case-sensitive exact match (adjust index usage accordingly)
            if exact_match:
                query = """
                    SELECT DISTINCT CUI, STR, SAB, CODE
                    FROM mrconso
                    WHERE STR = %s AND SAB = %s;
                """
                params = (term, source_vocab)
            else:
                # Uses the GIN index idx_mrconso_str_lower if created
                query = """
                    SELECT DISTINCT CUI, STR, SAB, CODE
                    FROM mrconso
                    WHERE lower(STR) = lower(%s) AND SAB = %s;
                """
                # Or for partial match:
                # query = """
                #     SELECT DISTINCT CUI, STR, SAB, CODE
                #     FROM mrconso
                #     WHERE lower(STR) LIKE lower(%s) AND SAB = %s;
                # """
                # params = (f"%{term}%", source_vocab) # for partial match
                params = (term, source_vocab) # for case-insensitive exact match

            # print(f"Executing query: {cur.mogrify(query, params)}") # Debug print
            cur.execute(query, params)
            rows = cur.fetchall()
            for row in rows:
                results.append({
                    'CUI': row[0],
                    'MatchedTerm': row[1],
                    'Source': row[2],
                    'SourceCode': row[3]
                })
    except psycopg2.Error as e:
        print(f"Database query error in find_concept_by_term_and_source: {e}", file=sys.stderr)
        conn.rollback() # Rollback on error
    except Exception as e:
         print(f"Unexpected error in find_concept_by_term_and_source: {e}", file=sys.stderr)
    finally:
        if conn:
            conn.close()
    return results

def get_semantic_types_for_cui(cui):
    """Retrieves semantic types (TUI and STY name) for a given CUI."""
    conn = get_db_connection()
    if conn is None:
        return []

    results = []
    try:
        with conn.cursor() as cur:
            query = """
                SELECT TUI, STY
                FROM mrsty
                WHERE CUI = %s;
            """
            cur.execute(query, (cui,))
            rows = cur.fetchall()
            for row in rows:
                results.append({
                    'TUI': row[0],
                    'SemanticType': row[1]
                })
    except psycopg2.Error as e:
        print(f"Database query error in get_semantic_types_for_cui: {e}", file=sys.stderr)
        conn.rollback()
    except Exception as e:
         print(f"Unexpected error in get_semantic_types_for_cui: {e}", file=sys.stderr)
    finally:
        if conn:
            conn.close()
    return results

# --- Add a main block for testing db_utils.py directly ---
if __name__ == "__main__":
    print("--- Testing db_utils functions ---")

    test_term = "Lung cancer"
    print(f"\nSearching for term: '{test_term}' in SNOMEDCT_US (case-insensitive exact)")
    concepts = find_concept_by_term_and_source(test_term, source_vocab='SNOMEDCT_US', exact_match=False)

    if concepts:
        print("Found concepts:")
        for concept in concepts:
            print(f"  CUI: {concept['CUI']}, Term: {concept['MatchedTerm']}, Code: {concept['SourceCode']}")
            cui_to_check = concept['CUI']
            print(f"    Getting semantic types for CUI: {cui_to_check}")
            sem_types = get_semantic_types_for_cui(cui_to_check)
            if sem_types:
                for st in sem_types:
                    print(f"      TUI: {st['TUI']}, Type: {st['SemanticType']}")
            else:
                print("      No semantic types found.")
            # Only check sem types for the first found concept for brevity
            break
    else:
        print(f"No concepts found for '{test_term}' in SNOMEDCT_US.")

    print("\n--- db_utils test finished ---")