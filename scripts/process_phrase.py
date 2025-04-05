# --- START OF FILE scripts/process_phrase.py ---
import spacy
import scispacy
import logging
import os
from typing import List, Dict, Tuple, Any, Set, Callable, Optional # Added Callable, Optional
# Removed displacy import

# Import DB utils needed for concept extraction
from db_utils import find_concept_by_term_and_source, get_semantic_types_for_cui
# Import the relation extractor
from relation_extractor import extract_relations_hybrid # Assuming relation_extractor.py is the latest one
# Import rapidfuzz for string similarity scoring
from rapidfuzz import fuzz

# Configure logging (remains for backend logs)
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Load the ScispaCy NER model ---
SCISPACY_MODEL = "en_ner_bc5cdr_md"
nlp = None # Initialize nlp to None
try:
    # Load default components, including tagger and parser
    nlp = spacy.load(SCISPACY_MODEL)
    logger.info(f"ScispaCy model '{SCISPACY_MODEL}' loaded successfully.")
    logger.info(f"Loaded pipeline components: {nlp.pipe_names}")
    if 'parser' not in nlp.pipe_names: logger.error("Dependency parser ('parser') not found. Rule-based RE disabled.")
    if 'tagger' not in nlp.pipe_names: logger.warning("POS Tagger ('tagger') not found. Fallback/rules may be less accurate.")
except OSError:
    logger.error(f"ScispaCy model '{SCISPACY_MODEL}' not found. Please ensure it's installed.")
    # nlp remains None
except Exception as e:
    logger.error(f"Error loading Spacy model '{SCISPACY_MODEL}': {e}", exc_info=True)
    # nlp remains None


# --- Configuration ---
TARGET_SOURCE = "NCI"
FALLBACK_SCORE_THRESHOLD = 1000
FALLBACK_POS_TAGS = {'NOUN', 'PROPN', 'ADJ'}

# --- Mapping from ScispaCy labels to plausible UMLS Semantic Types ---
LABEL_TO_SEMANTIC_TYPES = {
    "DISEASE": ["Disease or Syndrome", "Neoplastic Process", "Sign or Symptom", "Pathologic Function", "Congenital Abnormality", "Mental or Behavioral Dysfunction", "Finding", "Injury or Poisoning", "Anatomical Abnormality"],
    "CHEMICAL": ["Chemical", "Pharmacologic Substance", "Clinical Drug", "Antibiotic", "Biologically Active Substance", "Hazardous or Poisonous Substance", "Therapeutic or Preventive Procedure"]
}
EXCLUDE_SEMANTIC_TYPES = {"Experimental Model of Disease"}


def get_filtered_db_matches(entity_text: str, entity_label: str = "UNKNOWN") -> List[Dict[str, Any]]:
    """
    Finds concepts in the DB (exact first, then partial), filters them based on
    semantic types, ranks them based on string similarity, and returns the
    ranked list.
    (Internal logging uses logger, no need for client-facing logs here)
    """
    # logger.debug(f"Looking up term: '{entity_text}' (Label: {entity_label}) for SAB='{TARGET_SOURCE}'")
    db_results_exact = find_concept_by_term_and_source(
        term=entity_text, source_sab=TARGET_SOURCE, exact_match=True, case_sensitive=False, limit=5
    )

    if not db_results_exact:
        # logger.debug(f"No exact match for '{entity_text}', trying partial match...")
        db_results_partial = find_concept_by_term_and_source(
            term=entity_text, source_sab=TARGET_SOURCE, exact_match=False, case_sensitive=False, limit=20
        )
        db_results = db_results_partial
        if not db_results:
             # logger.debug(f"No partial DB match found for '{entity_text}' in {TARGET_SOURCE}")
             return []
    else:
         db_results = db_results_exact
         # logger.debug(f"Found {len(db_results)} exact matches for '{entity_text}'.")


    # logger.debug(f"Processing {len(db_results)} DB candidates for '{entity_text}'. Filtering and Ranking...")
    filtered_matches = []
    processed_cuis_for_term = set()
    allowed_sem_types = LABEL_TO_SEMANTIC_TYPES.get(entity_label) if entity_label != "UNKNOWN" else None
    if not allowed_sem_types and entity_label != "UNKNOWN":
         logger.warning(f"No semantic type mapping defined for NER label '{entity_label}'. Filtering effectiveness reduced.")

    entity_text_lower = entity_text.lower()

    for cui, matched_term, code in db_results:
        if cui in processed_cuis_for_term: continue

        semantic_types_raw = get_semantic_types_for_cui(cui)
        sem_type_names = [st[1] for st in semantic_types_raw if isinstance(st, tuple) and len(st) >= 2] if semantic_types_raw else []
        # if not semantic_types_raw: logger.debug(f"No semantic types found in MRSTY for CUI {cui} ('{matched_term}')")

        # --- Filtering Logic ---
        if any(st_name in EXCLUDE_SEMANTIC_TYPES for st_name in sem_type_names):
            # logger.debug(f"  Match CUI {cui} ('{matched_term}') FAILED filter: Excluded semantic type.")
            continue
        if allowed_sem_types:
            passes_label_filter = False
            if not sem_type_names: passes_label_filter = False
            elif any(st_name in allowed_sem_types for st_name in sem_type_names): passes_label_filter = True
            # else: logger.debug(f"  Match CUI {cui} ('{matched_term}') FAILED label filter.")

            if not passes_label_filter: continue

        # --- Scoring Logic ---
        score = 0
        matched_term_lower = matched_term.lower()
        exact_match_bonus = 1000
        is_exact_match = (matched_term_lower == entity_text_lower)
        if is_exact_match: score += exact_match_bonus
        similarity_score = fuzz.ratio(entity_text_lower, matched_term_lower)
        score += similarity_score
        length_diff = len(matched_term) - len(entity_text)
        if length_diff > 5 and not is_exact_match: score -= length_diff * 0.25

        # logger.debug(f"  Calculating score for CUI {cui} ('{matched_term}'): Score={score:.2f} (Similarity={similarity_score:.2f}, Exact={is_exact_match})")
        match_data = {'cui': cui, 'matched_term': matched_term, 'code': code, 'sem_types': sem_type_names, 'score': score}
        filtered_matches.append(match_data)
        processed_cuis_for_term.add(cui)

    # --- Sorting ---
    if filtered_matches:
        filtered_matches.sort(key=lambda x: x['score'], reverse=True)
        # logger.debug(f"Sorted {len(filtered_matches)} matches for '{entity_text}'. Best: {filtered_matches[0]['matched_term']} (Score: {filtered_matches[0]['score']:.2f})")
    # else: logger.debug(f"No matches remained after filtering for '{entity_text}'.")

    return filtered_matches


def overlaps(start1: int, end1: int, start2: int, end2: int) -> bool:
    """Checks if two spans [start1, end1) and [start2, end2) overlap."""
    return max(start1, start2) < min(end1, end2)


def process_phrase(phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """
    Processes a phrase: identifies NCI concepts using primary NER + token-based fallback,
    and extracts relationships between them. Sends logs via optional log_func.
    """
    if not nlp:
        error_msg = "ScispaCy model not loaded. Cannot process phrase."
        logger.error(error_msg)
        if log_func: log_func(f"ERROR: {error_msg}")
        return {'concepts': [], 'relations': []}

    if log_func: log_func(f"Processing phrase: '{phrase}'")
    logger.info(f"Processing phrase: '{phrase}'") # Keep backend log

    doc = None
    try:
        if log_func: log_func("Starting NLP processing...")
        doc = nlp(phrase) # Keep this doc object for relation extraction
        if log_func: log_func("NLP processing complete.")
    except Exception as e:
        error_msg = f"Error during NLP processing for phrase '{phrase}': {e}"
        logger.error(error_msg, exc_info=True)
        if log_func: log_func(f"ERROR: {error_msg}")
        return {'concepts': [], 'relations': []}

    final_concepts = []
    processed_cuis_in_phrase: Set[str] = set()
    processed_spans: List[Tuple[int, int]] = []

    ner_entities_log = [(ent.text, ent.label_, ent.start_char, ent.end_char) for ent in doc.ents]
    if log_func: log_func(f"Found {len(ner_entities_log)} NER entities: {ner_entities_log}")
    logger.info(f"ScispaCy NER found entities: {ner_entities_log}") # Keep backend log

    # --- Step 1: Concept Extraction from Primary NER ---
    if log_func: log_func("Starting NER-based concept extraction...")
    ner_concepts_found = 0
    for ent in doc.ents:
        entity_text = ent.text
        entity_label = ent.label_
        start_char = ent.start_char
        end_char = ent.end_char
        processed_spans.append((start_char, end_char)) # Add span regardless of match outcome

        try:
            if log_func: log_func(f"  Looking up NER entity: '{entity_text}' ({entity_label})")
            ranked_db_matches = get_filtered_db_matches(entity_text, entity_label)
            if ranked_db_matches:
                best_match = ranked_db_matches[0]
                cui = best_match['cui']
                score = best_match['score']
                if cui not in processed_cuis_in_phrase:
                    msg = f"[NER] Found concept for '{entity_text}': CUI={cui}, Term='{best_match['matched_term']}' (Score: {score:.2f})"
                    if log_func: log_func(f"  {msg}")
                    logger.info(msg) # Keep backend log
                    final_concepts.append({
                        "text_span": entity_text,
                        "start_char": start_char,
                        "end_char": end_char,
                        "matched_term": best_match['matched_term'],
                        "cui": cui,
                        "code": best_match['code'],
                        "sem_types": best_match['sem_types'],
                        "score": score,
                        "source": "ner"
                    })
                    processed_cuis_in_phrase.add(cui)
                    ner_concepts_found += 1
                # else: logger.debug(f"[NER] Skipping CUI {cui} for entity '{entity_text}' as already added.") # Keep DEBUG logs commented
            # else: logger.info(f"[NER] No suitable NCI concept match found for entity '{entity_text}' ({entity_label}).")
        except Exception as e:
            error_msg = f"Error processing NER entity '{entity_text}': {e}"
            logger.error(error_msg, exc_info=True)
            if log_func: log_func(f"ERROR: {error_msg}")

    msg = f"NER concept extraction finished. Found {ner_concepts_found} new concepts."
    if log_func: log_func(msg)
    logger.info(msg) # Keep backend log

    # --- Step 2: Fallback Concept Detection using Tokens ---
    if log_func: log_func("Starting fallback concept detection using individual tokens...")
    fallback_concepts_found = 0
    try:
        for token in doc:
            token_start = token.idx
            token_end = token.idx + len(token.text)
            token_text = token.text
            token_pos = token.pos_

            is_overlapping = any(overlaps(token_start, token_end, p_start, p_end) for p_start, p_end in processed_spans)
            if is_overlapping:
                # logger.debug(f"Skipping token '{token_text}' (POS: {token_pos}) as it overlaps with processed NER span.")
                continue

            if token_pos not in FALLBACK_POS_TAGS:
                 # logger.debug(f"Skipping token '{token_text}' due to irrelevant POS tag: {token_pos}")
                 continue

            # logger.debug(f"Processing non-overlapping token: '{token_text}' (POS: {token_pos})")
            ranked_fallback_matches = get_filtered_db_matches(token_text, entity_label="UNKNOWN")

            if ranked_fallback_matches:
                best_fallback_match = ranked_fallback_matches[0]
                fallback_cui = best_fallback_match['cui']
                fallback_score = best_fallback_match['score']

                if fallback_score >= FALLBACK_SCORE_THRESHOLD: # Strict fallback requires exact match score
                    if fallback_cui not in processed_cuis_in_phrase:
                        msg = f"[Fallback] Found concept for token '{token_text}': CUI={fallback_cui}, Term='{best_fallback_match['matched_term']}' (Score: {fallback_score:.2f})"
                        if log_func: log_func(f"  {msg}")
                        logger.info(msg) # Keep backend log
                        final_concepts.append({
                            "text_span": token_text,
                            "start_char": token_start,
                            "end_char": token_end,
                            "matched_term": best_fallback_match['matched_term'],
                            "cui": fallback_cui,
                            "code": best_fallback_match['code'],
                            "sem_types": best_fallback_match['sem_types'],
                            "score": fallback_score,
                            "source": "fallback_token"
                        })
                        processed_cuis_in_phrase.add(fallback_cui)
                        processed_spans.append((token_start, token_end))
                        fallback_concepts_found += 1
                    # else: logger.debug(f"[Fallback] Skipping CUI {fallback_cui} for token '{token_text}' as already added.")
                # else: logger.debug(f"[Fallback] Match for '{token_text}' score ({fallback_score:.2f}) below threshold.")
    except Exception as e:
         error_msg = f"Error during fallback token processing: {e}"
         logger.error(error_msg, exc_info=True)
         if log_func: log_func(f"ERROR: {error_msg}")

    msg = f"Fallback concept detection finished. Found {fallback_concepts_found} additional concepts."
    if log_func: log_func(msg)
    logger.info(msg) # Keep backend log
    msg = f"Total concepts found: {len(final_concepts)}"
    if log_func: log_func(msg)
    logger.info(msg)

    # --- Step 3: Relation Extraction (using combined concepts) ---
    extracted_relations = []
    parser_available = nlp and 'parser' in nlp.pipe_names
    if not parser_available:
        warn_msg = "Skipping relation extraction: parser component not available."
        logger.warning(warn_msg)
        if log_func: log_func(f"WARNING: {warn_msg}")

    if len(final_concepts) >= 2 and parser_available:
        if log_func: log_func("Starting relation extraction...")
        logger.info("Starting relation extraction...") # Keep backend log
        try:
            # Sort concepts by start_char before passing - helps readability and potentially rules
            final_concepts.sort(key=lambda x: x['start_char'])
            extracted_relations = extract_relations_hybrid(doc, final_concepts) # Pass the original doc
            msg = f"Relation extraction finished. Found {len(extracted_relations)} relations."
            if log_func: log_func(msg)
            logger.info(msg) # Keep backend log
        except ImportError:
             error_msg = "Could not import 'extract_relations_hybrid'."
             logger.error(error_msg, exc_info=True)
             if log_func: log_func(f"ERROR: {error_msg}")
        except Exception as e:
            error_msg = f"An error occurred during relation extraction: {e}"
            logger.error(error_msg, exc_info=True)
            if log_func: log_func(f"ERROR: {error_msg}")
    elif len(final_concepts) < 2:
        msg = "Skipping relation extraction (less than 2 concepts found)."
        if log_func: log_func(msg)
        logger.info(msg) # Keep backend log

    # --- Step 4: Return combined results ---
    # Sort concepts again before returning for consistent final output
    final_concepts.sort(key=lambda x: x['start_char'])
    if log_func: log_func("Processing complete. Returning results.")
    return {
        'concepts': final_concepts,
        'relations': extracted_relations
    }

# NOTE: The if __name__ == "__main__": block has been removed.
#       Testing and execution should now happen via the API.
# --- END OF FILE scripts/process_phrase.py ---