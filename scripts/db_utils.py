# /app/scripts/db_utils.py
import os
import psycopg2
import sys
import logging # Import logging
from typing import List, Tuple, Dict, Optional # Added type hinting

# Configure logging
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Database Configuration ---
DB_NAME = os.getenv('POSTGRES_DB', 'umls_postgres_db') # Match docker-compose
DB_USER = os.getenv('POSTGRES_USER', 'umls_user')     # Match docker-compose
DB_PASSWORD = os.getenv('POSTGRES_PASSWORD')
DB_HOST = os.getenv('DB_HOST', 'db')
DB_PORT = os.getenv('DB_PORT', '5432')

if not DB_PASSWORD:
    logger.error("FATAL: POSTGRES_PASSWORD environment variable not set.")
    # Exiting might be safer if DB is absolutely required downstream
    # sys.exit(1)

def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    conn = None
    if not DB_PASSWORD:
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
        logger.debug("DB connection successful") # Simplified log message
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Error connecting to database: {e}")
        logger.error(f"  Attempted: host={DB_HOST}, port={DB_PORT}, dbname={DB_NAME}, user={DB_USER}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred getting DB connection: {e}")
        return None

# --- Concept & Semantic Type Functions ---

def find_concept_by_term_and_source(term: str,
                                    source_sab: str,
                                    exact_match: bool = True,
                                    case_sensitive: bool = False,
                                    limit: int = 5) -> List[Tuple[str, str, str]]:
    """
    Finds concepts (CUI, STR, CODE) in MRCONSO by term, filtering by source SAB.

    Args:
        term: The term string to search for.
        source_sab: The source abbreviation (SAB) (e.g., 'NCI', 'SNOMEDCT_US').
        exact_match: If True, looks for an exact match. If False, uses LIKE.
        case_sensitive: If True, matching is case-sensitive. Default False.
        limit: Maximum number of results to return.

    Returns:
        A list of tuples (cui, matched_term, code). Empty list on error/no match.
    """
    conn = None
    results = []
    try:
        conn = get_db_connection()
        if not conn:
            return results

        with conn.cursor() as cur:
            term_param = term.strip()
            sab_param = source_sab.strip()
            params_list = [] # Build params list dynamically

            # Base query
            query_base = "SELECT cui, str, code FROM mrconso WHERE sab = %s AND lat = 'ENG'"
            params_list.append(sab_param)

            # Add term matching condition
            if exact_match:
                if case_sensitive:
                    query_base += " AND str = %s"
                    params_list.append(term_param)
                else:
                    query_base += " AND LOWER(str) = LOWER(%s)"
                    params_list.append(term_param)
            else: # Partial match (LIKE)
                term_param_like = f"%{term_param}%"
                if case_sensitive:
                    query_base += " AND str LIKE %s"
                    params_list.append(term_param_like)
                else:
                    query_base += " AND LOWER(str) LIKE LOWER(%s)"
                    params_list.append(term_param_like)

            # Add LIMIT
            query_base += " LIMIT %s;"
            params_list.append(limit)

            params = tuple(params_list)
            logger.debug(f"Executing SQL: {cur.mogrify(query_base, params).decode('utf-8', 'ignore')}")
            cur.execute(query_base, params)
            results = cur.fetchall() # Returns list of tuples directly

    except psycopg2.Error as e:
        logger.error(f"Database error in find_concept_by_term_and_source: {e}")
    except Exception as e:
        logger.error(f"Unexpected error in find_concept_by_term_and_source: {e}")
    finally:
        if conn:
            conn.close()
    logger.debug(f"Found {len(results)} concepts for term '{term}' [exact={exact_match}, case_sensitive={case_sensitive}] with SAB '{source_sab}'")
    return results


def get_semantic_types_for_cui(cui: str) -> List[Tuple[str, str]]:
    """
    Retrieves semantic types (TUI and STY name) for a given CUI from MRSTY.

    Args:
        cui: The Concept Unique Identifier (CUI).

    Returns:
        A list of tuples (tui, type_name). Empty list on error/no types.
    """
    conn = None
    results = []
    if not cui or not isinstance(cui, str): # Basic validation
        logger.warning(f"Invalid CUI provided to get_semantic_types_for_cui: {cui}")
        return results
    try:
        conn = get_db_connection()
        if not conn:
            return results

        with conn.cursor() as cur:
            query = "SELECT TUI, STY FROM mrsty WHERE CUI = %s;"
            params = (cui,)
            logger.debug(f"Executing SQL: {cur.mogrify(query, params).decode('utf-8', 'ignore')}")
            cur.execute(query, params)
            results = cur.fetchall() # Returns list of tuples directly
    except psycopg2.Error as e:
        logger.error(f"Database query error in get_semantic_types_for_cui for CUI {cui}: {e}")
    except Exception as e:
         logger.error(f"Unexpected error in get_semantic_types_for_cui for CUI {cui}: {e}")
    finally:
        if conn:
            conn.close()
    logger.debug(f"Found {len(results)} semantic types for CUI {cui}")
    return results


# --- Relation Extraction Function --- <<< NEW FUNCTION >>>

def get_relations_for_cui_pair(cui1: str, cui2: str,
                               target_sabs: Optional[Tuple[str, ...]] = None,
                               limit: int = 10) -> List[Dict[str, str]]:
    """
    Retrieves relationships between two CUIs from the MRREL table.

    Searches for relationships in both directions (CUI1->CUI2 and CUI2->CUI1).

    Args:
        cui1: The first Concept Unique Identifier.
        cui2: The second Concept Unique Identifier.
        target_sabs: Optional tuple of source abbreviations (SABs) to filter by
                     (e.g., ('NCI', 'SNOMEDCT_US')). If None, returns relations
                     from any source.
        limit: Maximum number of relation entries to return.

    Returns:
        A list of dictionaries, each representing a relationship with keys:
        'cui1', 'cui2', 'rel', 'rela', 'sab'. Returns an empty list if no
        relationship is found or an error occurs.
    """
    conn = None
    relations = []
    if not all([cui1, cui2, isinstance(cui1, str), isinstance(cui2, str)]):
        logger.warning(f"Invalid CUIs provided to get_relations_for_cui_pair: {cui1}, {cui2}")
        return relations

    try:
        conn = get_db_connection()
        if not conn:
            return relations

        with conn.cursor() as cur:
            # Select the columns defined during table creation
            query_base = """
                SELECT CUI1, CUI2, REL, RELA, SAB
                FROM mrrel
                WHERE ((CUI1 = %s AND CUI2 = %s) OR (CUI1 = %s AND CUI2 = %s))
            """
            params_list = [cui1, cui2, cui2, cui1] # Parameters for the WHERE clause

            # Add SAB filtering if specified
            if target_sabs and isinstance(target_sabs, (list, tuple)) and len(target_sabs) > 0:
                query_base += " AND SAB = ANY(%s)" # Use = ANY for list/tuple of SABs
                params_list.append(list(target_sabs)) # Pass SABs as a list for ANY
            elif target_sabs and isinstance(target_sabs, str): # Allow single SAB string
                query_base += " AND SAB = %s"
                params_list.append(target_sabs)

            # Add LIMIT
            query_base += " LIMIT %s;"
            params_list.append(limit)

            params = tuple(params_list)
            logger.debug(f"Executing SQL: {cur.mogrify(query_base, params).decode('utf-8', 'ignore')}")
            cur.execute(query_base, params)

            # Fetch results and format as list of dictionaries
            colnames = [desc[0].lower() for desc in cur.description] # Get column names dynamically
            fetched_rows = cur.fetchall()
            for row in fetched_rows:
                relations.append(dict(zip(colnames, row)))

    except psycopg2.Error as e:
        logger.error(f"Database query error in get_relations_for_cui_pair for CUIs ({cui1}, {cui2}): {e}")
    except Exception as e:
        logger.error(f"Unexpected error in get_relations_for_cui_pair for CUIs ({cui1}, {cui2}): {e}")
    finally:
        if conn:
            conn.close()

    logger.debug(f"Found {len(relations)} relations between CUIs {cui1} and {cui2}" +
                 (f" matching SABs {target_sabs}" if target_sabs else ""))
    return relations


# --- Main block for testing ---
if __name__ == "__main__":
    logger.info("--- Testing db_utils functions ---")

    # --- Test Concept Lookup ---
    # Using NCI thesaurus (SAB='NCI') as primary target now
    test_term = "Melanoma"
    test_sab = "NCI"
    logger.info(f"\n1. Searching for term: '{test_term}' in {test_sab} (case-insensitive exact)")
    concepts = find_concept_by_term_and_source(term=test_term,
                                               source_sab=test_sab,
                                               exact_match=True,
                                               case_sensitive=False,
                                               limit=1) # Limit to 1 for testing clarity

    test_cui_melanoma = None
    if concepts:
        print("Found concepts:")
        for cui, matched_term, code in concepts:
            test_cui_melanoma = cui # Store CUI for relation test
            print(f"  CUI: {cui}, Term: {matched_term}, Code: {code}")
            print(f"    Getting semantic types for CUI: {cui}")
            sem_types = get_semantic_types_for_cui(cui)
            if sem_types:
                for tui, type_name in sem_types:
                    print(f"      TUI: {tui}, Type: {type_name}")
            else:
                print("      No semantic types found.")
    else:
        print(f"No concepts found for '{test_term}' in {test_sab}.")

    # --- Test finding another concept ---
    test_term_location = "Liver"
    logger.info(f"\n2. Searching for term: '{test_term_location}' in {test_sab} (case-insensitive exact)")
    concepts_loc = find_concept_by_term_and_source(term=test_term_location,
                                                    source_sab=test_sab,
                                                    exact_match=True,
                                                    case_sensitive=False,
                                                    limit=1)
    test_cui_liver = None
    if concepts_loc:
        print("Found concepts:")
        for cui, matched_term, code in concepts_loc:
            test_cui_liver = cui # Store CUI
            print(f"  CUI: {cui}, Term: {matched_term}, Code: {code}")
    else:
         print(f"No concepts found for '{test_term_location}' in {test_sab}.")


    # --- Test Relation Lookup (using CUIs found above if available) --- <<< UPDATED TEST >>>
    logger.info(f"\n3. Testing relation lookup between CUIs '{test_cui_melanoma}' and '{test_cui_liver}'")
    if test_cui_melanoma and test_cui_liver:
        # Example 1: Find any relation between them
        print(f"\n  Searching for *any* relation between {test_cui_melanoma} and {test_cui_liver}:")
        relations_any = get_relations_for_cui_pair(test_cui_melanoma, test_cui_liver)
        if relations_any:
            print(f"  Found {len(relations_any)} relations:")
            for rel in relations_any:
                print(f"    - CUI1: {rel['cui1']}, CUI2: {rel['cui2']}, REL: {rel['rel']}, RELA: {rel['rela']}, SAB: {rel['sab']}")
        else:
            print("    No relations found in MRREL.")

        # Example 2: Find relations specifically from NCI
        target_rel_sabs = ('NCI',) # Looking only for NCI relationships
        print(f"\n  Searching for relations between {test_cui_melanoma} and {test_cui_liver} with SAB in {target_rel_sabs}:")
        relations_nci = get_relations_for_cui_pair(test_cui_melanoma, test_cui_liver, target_sabs=target_rel_sabs)
        if relations_nci:
            print(f"  Found {len(relations_nci)} relations from {target_rel_sabs}:")
            for rel in relations_nci:
                # Displaying RELA if available, otherwise REL
                rel_display = rel['rela'] if rel['rela'] else rel['rel']
                print(f"    - {rel['cui1']} --[{rel_display} ({rel['sab']})]-- {rel['cui2']}")
        else:
            print(f"    No relations found with SAB in {target_rel_sabs}.")
    else:
        print("\n  Skipping relation test as one or both CUIs were not found in previous steps.")

    logger.info("--- db_utils test finished ---")

# --- END OF FILE db_utils.py ---