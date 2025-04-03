# --- START OF FILE scripts/process_phrase.py ---

import spacy
import logging
# Make sure db_utils functions are imported correctly
from db_utils import find_concept_by_term_and_source, get_semantic_types_for_cui

# Configure logging (ensure level is DEBUG to see the new logs)
# You might need to set LOG_LEVEL=DEBUG as an environment variable
# or change level=logging.INFO to level=logging.DEBUG here.
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Load the spaCy model (consider loading globally if this becomes part of a larger app)
try:
    # Using the model downloaded into the Docker image via Dockerfile instruction
    nlp = spacy.load("en_core_web_sm")
    logging.info("spaCy model 'en_core_web_sm' loaded successfully.")
except OSError:
    # This error shouldn't happen if the model was added in the Dockerfile build
    logging.error("spaCy model 'en_core_web_sm' not found. Ensure it was downloaded during image build ('RUN python -m spacy download en_core_web_sm' in Dockerfile)")
    nlp = None # Ensure nlp is defined even if loading fails

TARGET_SOURCE = "SNOMEDCT_US" # Focus on SNOMED CT U.S. Edition terms

def process_phrase(phrase: str) -> list:
    """
    Processes a natural language phrase to identify potential SNOMED CT concepts.

    Steps:
    1. Uses spaCy to identify noun chunks as potential terms.
    2. Looks up these terms in the UMLS database (filtered for SNOMED CT).
    3. Retrieves CUI, SCTID (Code), and Semantic Types for found concepts.

    Args:
        phrase: The input text phrase.

    Returns:
        A list of dictionaries, where each dictionary represents a found concept
        and contains 'text_span', 'matched_term', 'cui', 'sctid', 'sem_types'.
        Returns an empty list if the spaCy model failed to load or no concepts are found.
    """
    if not nlp:
        logging.error("spaCy model not loaded. Cannot process phrase.")
        return []

    logging.info(f"Processing phrase: '{phrase}'")
    doc = nlp(phrase)
    found_concepts = []
    processed_cuis = set() # Keep track of CUIs already added to avoid duplicates

    # Use noun chunks as primary candidates for medical terms
    candidate_terms = [chunk.text for chunk in doc.noun_chunks]
    logging.info(f"Identified candidate terms (noun chunks): {candidate_terms}")

    # You could also add individual relevant tokens (e.g., nouns, proper nouns) if needed:
    # candidate_terms.extend([token.lemma_ for token in doc if token.pos_ in ["NOUN", "PROPN"]])
    # candidate_terms = list(set(candidate_terms)) # Remove duplicates if combining strategies

    for term_text in candidate_terms:
        logging.debug(f"Looking up term: '{term_text}' using exact_match=True") # Adjust log if changing exact_match
        # Use the existing db_utils function.
        # *** Consider changing exact_match=True to exact_match=False later to improve matching ***
        db_results = find_concept_by_term_and_source(
            term=term_text,
            source_sab=TARGET_SOURCE,
            exact_match=False, # Start with exact matches (can be changed later)
            case_sensitive=False # Usually preferred for clinical text
        )

        if db_results:
            logging.info(f"Found {len(db_results)} DB match(es) for '{term_text}'")
            # db_results is expected to be a list of tuples: [(cui, matched_term, sctid), ...]
            for cui, matched_term, sctid in db_results:
                if cui in processed_cuis:
                    logging.debug(f"Skipping already processed CUI: {cui} for term '{term_text}'")
                    continue

                logging.debug(f"  Processing Match: CUI={cui}, Term='{matched_term}', SCTID={sctid}")

                # --- Debugging Semantic Types ---
                # Call the function to get semantic types (expects list of tuples: [(tui, type_name), ...])
                semantic_types_raw = get_semantic_types_for_cui(cui)
                # Log exactly what was returned
                logging.debug(f"Raw semantic types returned for CUI {cui}: {semantic_types_raw}")
                # --- End of Debugging ---

                sem_type_names = [] # Initialize empty list for names

                # Safely process the returned data
                if isinstance(semantic_types_raw, list):
                    # Iterate and check if each item is a tuple with at least 2 elements
                    for st in semantic_types_raw:
                        if isinstance(st, tuple) and len(st) >= 2:
                            # Assume the type name is the second element (index 1)
                            sem_type_names.append(st[1])
                        else:
                             logging.warning(f"Unexpected item format in semantic types list for CUI {cui}: {st}")
                    logging.debug(f"Extracted semantic type names: {sem_type_names}")
                else:
                    logging.warning(f"Unexpected format for semantic types for CUI {cui}: {semantic_types_raw} (Expected a list)")


                found_concepts.append({
                    "text_span": term_text, # The chunk identified in the text
                    "matched_term": matched_term, # The term matched in the DB
                    "cui": cui,
                    "sctid": sctid,
                    "sem_types": sem_type_names # Use the safely extracted list of names
                })
                processed_cuis.add(cui)
                # Optional: break here if you only want the first match per text_span
                # break
        else:
            logging.debug(f"No exact DB match found for '{term_text}' in {TARGET_SOURCE}")

    logging.info(f"Found {len(found_concepts)} concepts in total.")
    return found_concepts

# --- Example Usage ---
if __name__ == "__main__":
    # Ensure db_utils can connect (implicitly tested by its functions)
    # Add more test phrases as needed
    test_phrases = [
        "Patient presents with metastatic lung cancer and shortness of breath.",
        "History of Asthma, no current exacerbation.",
        "Procedure: appendectomy due to acute appendicitis.",
        "Discussed risks including myocardial infarction.",
        "Negative for fever." # Example for testing terms not found / negation (negation not handled yet)
    ]

    for p in test_phrases:
        concepts = process_phrase(p)
        print("-" * 40)
        print(f"Results for: '{p}'")
        if concepts:
            for concept in concepts:
                print(f"  - Text Span: '{concept['text_span']}'")
                print(f"    Matched:   '{concept['matched_term']}' (CUI: {concept['cui']}, SCTID: {concept['sctid']})")
                # Check if sem_types list is not empty before joining
                if concept['sem_types']:
                    print(f"    Sem Types: {', '.join(concept['sem_types'])}")
                else:
                    print(f"    Sem Types: [Not Found or Empty]") # Indicate clearly if empty
        else:
            print("  No concepts identified.")
        print("-" * 40)
        print()

# --- END OF FILE scripts/process_phrase.py ---