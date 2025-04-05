# --- START OF FILE scripts/process_phrase.py ---
# Based on the version that produced the 2025-04-05 07:39:09 output,
# + Displacy block + Minimal log cleanup

import spacy
import scispacy
import logging
import os
from typing import List, Dict, Tuple, Any, Set
# <<< ADDED FOR VISUALIZATION >>>
from spacy import displacy
# import time # time.sleep might be useful if server closes too fast
# <<< END ADDED FOR VISUALIZATION >>>


# Import DB utils needed for concept extraction
from db_utils import find_concept_by_term_and_source, get_semantic_types_for_cui
# Import the relation extractor
from relation_extractor import extract_relations_hybrid # Assuming relation_extractor.py is the latest one
# Import rapidfuzz for string similarity scoring
from rapidfuzz import fuzz

# Configure logging
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Load the ScispaCy NER model ---
SCISPACY_MODEL = "en_ner_bc5cdr_md"
try:
    # Load default components, including tagger and parser
    nlp = spacy.load(SCISPACY_MODEL)
    logger.info(f"ScispaCy model '{SCISPACY_MODEL}' loaded successfully.")
    logger.info(f"Loaded pipeline components: {nlp.pipe_names}")
    if 'parser' not in nlp.pipe_names: logger.error("Dependency parser ('parser') not found. Rule-based RE disabled.")
    if 'tagger' not in nlp.pipe_names: logger.warning("POS Tagger ('tagger') not found. Fallback/rules may be less accurate.")
except OSError:
    logger.error(f"ScispaCy model '{SCISPACY_MODEL}' not found. Please ensure it's installed.")
    nlp = None
except Exception as e:
    logger.error(f"Error loading Spacy model '{SCISPACY_MODEL}': {e}", exc_info=True)
    nlp = None


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


def process_phrase(phrase: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Processes a phrase: identifies NCI concepts using primary NER + token-based fallback,
    and extracts relationships between them.
    """
    if not nlp:
        logger.error("ScispaCy model not loaded. Cannot process phrase.")
        return {'concepts': [], 'relations': []}

    logger.info(f"Processing phrase: '{phrase}'")
    try:
        doc = nlp(phrase) # Keep this doc object for relation extraction
    except Exception as e:
        logger.error(f"Error during NLP processing for phrase '{phrase}': {e}", exc_info=True)
        return {'concepts': [], 'relations': []}

    final_concepts = []
    processed_cuis_in_phrase: Set[str] = set()
    processed_spans: List[Tuple[int, int]] = []

    ner_entities_log = [(ent.text, ent.label_, ent.start_char, ent.end_char) for ent in doc.ents]
    logger.info(f"ScispaCy NER found entities: {ner_entities_log}")

    # --- Step 1: Concept Extraction from Primary NER ---
    for ent in doc.ents:
        entity_text = ent.text
        entity_label = ent.label_
        start_char = ent.start_char
        end_char = ent.end_char
        processed_spans.append((start_char, end_char)) # Add span regardless of match outcome

        try:
            ranked_db_matches = get_filtered_db_matches(entity_text, entity_label)
            if ranked_db_matches:
                best_match = ranked_db_matches[0]
                cui = best_match['cui']
                score = best_match['score']
                if cui not in processed_cuis_in_phrase:
                    logger.info(f"[NER] Selected concept for '{entity_text}': CUI={cui}, Term='{best_match['matched_term']}' (Score: {score:.2f})")
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
                # else: logger.debug(f"[NER] Skipping CUI {cui} for entity '{entity_text}' as already added.") # Keep DEBUG logs commented
            # else: logger.info(f"[NER] No suitable NCI concept match found for entity '{entity_text}' ({entity_label}).")
        except Exception as e:
            logger.error(f"Error processing NER entity '{entity_text}': {e}", exc_info=True)

    logger.info(f"Primary NER concept extraction finished. Found {len(final_concepts)} concepts.")
    # logger.debug(f"Processed spans by NER: {processed_spans}") # Keep DEBUG logs commented

    # --- Step 2: Fallback Concept Detection using Tokens ---
    logger.info("Starting fallback concept detection using individual tokens...")
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

            # Optional basic filtering
            # if len(token_text) < 3 or not token_text.isalnum(): continue

            # logger.debug(f"Processing non-overlapping token: '{token_text}' (POS: {token_pos})")
            ranked_fallback_matches = get_filtered_db_matches(token_text, entity_label="UNKNOWN")

            if ranked_fallback_matches:
                best_fallback_match = ranked_fallback_matches[0]
                fallback_cui = best_fallback_match['cui']
                fallback_score = best_fallback_match['score']

                if fallback_score >= FALLBACK_SCORE_THRESHOLD:
                    if fallback_cui not in processed_cuis_in_phrase:
                        logger.info(f"[Fallback] Selected concept for token '{token_text}': CUI={fallback_cui}, Term='{best_fallback_match['matched_term']}' (Score: {fallback_score:.2f})")
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
         logger.error(f"Error during fallback token processing: {e}", exc_info=True)

    logger.info(f"Fallback concept detection finished. Found {fallback_concepts_found} additional concepts.")
    logger.info(f"Total concepts found after fallback: {len(final_concepts)}")

    # --- Step 3: Relation Extraction (using combined concepts) ---
    extracted_relations = []
    parser_available = nlp and 'parser' in nlp.pipe_names
    if not parser_available:
        logger.warning("Skipping relation extraction: parser component not available.")

    if len(final_concepts) >= 2 and parser_available:
        logger.info("Starting relation extraction...")
        try:
            # Sort concepts by start_char before passing - helps readability and potentially rules
            final_concepts.sort(key=lambda x: x['start_char'])
            extracted_relations = extract_relations_hybrid(doc, final_concepts) # Pass the original doc
            logger.info(f"Relation extraction finished. Found {len(extracted_relations)} relations.")
        except ImportError:
             logger.error("Could not import 'extract_relations_hybrid'.", exc_info=True)
        except Exception as e:
            logger.error(f"An error occurred during relation extraction: {e}", exc_info=True)
    elif len(final_concepts) < 2:
        logger.info("Skipping relation extraction (less than 2 concepts found).")

    # --- Step 4: Return combined results ---
    # Sort concepts again before returning for consistent final output
    final_concepts.sort(key=lambda x: x['start_char'])
    return {
        'concepts': final_concepts,
        'relations': extracted_relations
    }

# --- Example Usage & Visualization ---
if __name__ == "__main__":
    # Define which phrases to visualize (modify this list as needed)
    # Select ONLY ONE phrase at a time for visualization unless you handle threading/manual stopping
    phrases_to_visualize = [
         # "Patient presents with metastatic lung cancer to the liver.",
         # "Treatment included cisplatin and radiation.",
         # "History of Asthma, no current exacerbation.",
         # "Negative for fever and chills.",
         "Severe pain in abdomen." # Example: Visualize this one
    ]

    test_phrases = [ # Keep the full list for processing
        "Patient presents with metastatic lung cancer to the liver.",
        "Melanoma stage III diagnosed.",
        "Treatment included cisplatin and radiation.",
        "History of Asthma, no current exacerbation.",
        "Negative for fever and chills.",
        "Severe pain in abdomen."
    ]

    if not nlp:
         print("Cannot run examples because ScispaCy model failed to load.", file=sys.stderr)
    else:
        # Keep logging at INFO level by default
        # logging.getLogger().setLevel(logging.DEBUG) # Uncomment for full DEBUG logs

        for p in test_phrases:

            # --- Visualization Block START --- <<< TEMPORARY for DEBUGGING >>>
            # This block uses the *phrase string* 'p'. The main processing uses 'doc'.
            # Ensure this works or adjust to pass the 'doc' object if needed.
            if p in phrases_to_visualize:
                print("\n" + "="*20 + " VISUALIZATION " + "="*20)
                print(f"Attempting to serve displacy visualization for:\n'{p}'")
                print("Access http://localhost:5000 (or host IP) in your browser.")
                print("Stop the script with Ctrl+C in the terminal when done viewing.")
                print("="*55 + "\n")
                try:
                    # It's better to use the same 'doc' object that relation extraction will use
                    # We process it here *before* calling process_phrase for visualization
                    doc_for_viz = nlp(p)
                    displacy.serve(doc_for_viz, style="dep", port=5000, host="0.0.0.0")
                    # displaCy serve blocks here until stopped
                except Exception as e:
                    logger.error(f"Failed to serve displacy: {e}", exc_info=True)
                print("\n" + "="*20 + " Resuming Processing... " + "="*20)
                # Since displacy blocks, only one phrase will be visualized per run unless stopped manually.
                # Comment out the displacy.serve line or the whole block when done debugging.
            # --- Visualization Block END ---

            # --- Regular Processing ---
            results = process_phrase(p) # Call the main processing function
            concepts = results.get('concepts', [])
            relations = results.get('relations', [])

            # --- Print Results ---
            print("-" * 60)
            print(f"Results for: '{p}'")
            print("-" * 60)
            if concepts:
                print("\n[Concepts Identified]")
                # Concepts are already sorted by start_char inside process_phrase before return
                for concept in concepts:
                    source_tag = f"({concept.get('source', 'ner')})"
                    print(f"  - Span:      '{concept['text_span']}' ({concept['start_char']}:{concept['end_char']}) {source_tag}")
                    print(f"    Matched:   '{concept['matched_term']}'")
                    print(f"    CUI:       {concept['cui']} (Code: {concept['code']})")
                    print(f"    Sem Types: {', '.join(concept['sem_types']) if concept['sem_types'] else '[None Found]'}")
                    print(f"    Score:     {concept.get('score', 'N/A'):.2f}")
            else: print("\n[Concepts Identified]\n  No concepts identified.")
            if relations:
                 print("\n[Relations Extracted]")
                 for rel in relations:
                    subj_text = next((c['text_span'] for c in concepts if c['cui'] == rel.get('subject_cui')), rel.get('subj_text', '???'))
                    obj_text = next((c['text_span'] for c in concepts if c['cui'] == rel.get('object_cui')), rel.get('obj_text', '???'))
                    print(f"  - ({subj_text}) --[{rel.get('relation', '?')} ({rel.get('source', '?')})]--> ({obj_text})")
            else: print("\n[Relations Extracted]\n  No relations extracted.")
            print("-" * 60 + "\n")

# --- END OF FILE scripts/process_phrase.py ---