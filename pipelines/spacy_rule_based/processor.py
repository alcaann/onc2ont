# --- START OF FILE pipelines/spacy_rule_based/processor.py ---

import spacy
import scispacy # Although model loaded via spacy.load, import might be needed implicitly
import logging
import os
from typing import List, Dict, Tuple, Any, Set, Callable, Optional

# --- Project Structure Imports ---
# Import the base class
from pipelines.base_pipeline import BaseProcessingPipeline
# Import shared utilities (assuming 'scripts' is accessible via PYTHONPATH)
from scripts.db_utils import find_concept_by_term_and_source, get_semantic_types_for_cui
# Import the relation extractor from the same package
from .relation_extractor import extract_relations_hybrid
# --- Library Imports ---
from rapidfuzz import fuzz

# Module-level logger (can be used by helper functions too)
logger = logging.getLogger(__name__)

# --- Helper Function (can stay at module level or become static method) ---
def overlaps(start1: int, end1: int, start2: int, end2: int) -> bool:
    """Checks if two spans [start1, end1) and [start2, end2) overlap."""
    return max(start1, start2) < min(end1, end2)

# --- Implementation Class ---
class SpacyRuleBasedPipeline(BaseProcessingPipeline):
    """
    A processing pipeline implementation using ScispaCy for NER,
    rule-based relation extraction, and NCI Thesaurus lookup via DB.
    """

    # --- Configuration (Class Attributes) ---
    SCISPACY_MODEL = "en_ner_bc5cdr_md"
    TARGET_SOURCE = "NCI"
    FALLBACK_SCORE_THRESHOLD = 1000 # Exact match bonus in _get_filtered_db_matches
    FALLBACK_POS_TAGS = {'NOUN', 'PROPN', 'ADJ'}

    # Mapping from ScispaCy labels to plausible UMLS Semantic Types
    LABEL_TO_SEMANTIC_TYPES = {
        "DISEASE": ["Disease or Syndrome", "Neoplastic Process", "Sign or Symptom", "Pathologic Function", "Congenital Abnormality", "Mental or Behavioral Dysfunction", "Finding", "Injury or Poisoning", "Anatomical Abnormality"],
        "CHEMICAL": ["Chemical", "Pharmacologic Substance", "Clinical Drug", "Antibiotic", "Biologically Active Substance", "Hazardous or Poisonous Substance", "Therapeutic or Preventive Procedure"]
    }
    EXCLUDE_SEMANTIC_TYPES = {"Experimental Model of Disease"}

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """
        Initializes the pipeline, loading the ScispaCy model.

        Args:
            config: Optional dictionary for future configuration loading.
        """
        # Initialize instance logger
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.info("Initializing SpacyRuleBasedPipeline...")

        # Override defaults from config if provided (example)
        if config:
            self.SCISPACY_MODEL = config.get("scispacy_model", self.SCISPACY_MODEL)
            # ... load other config options

        # Load the ScispaCy NER model
        self.nlp = self._load_spacy_model()
        if self.nlp:
            self.logger.info(f"Pipeline initialized with model '{self.SCISPACY_MODEL}'. Components: {self.nlp.pipe_names}")
            if 'parser' not in self.nlp.pipe_names:
                 self.logger.error("Dependency parser ('parser') not found. Rule-based Relation Extraction may fail.")
            if 'tagger' not in self.nlp.pipe_names:
                 self.logger.warning("POS Tagger ('tagger') not found. Fallback/rules may be less accurate.")
        else:
            self.logger.error("Pipeline initialization failed: ScispaCy model could not be loaded.")
            # Further calls to process() will check self.nlp and return early

    def _load_spacy_model(self):
        """Loads the specified ScispaCy model."""
        try:
            nlp = spacy.load(self.SCISPACY_MODEL)
            self.logger.info(f"ScispaCy model '{self.SCISPACY_MODEL}' loaded successfully.")
            return nlp
        except OSError:
            self.logger.error(f"ScispaCy model '{self.SCISPACY_MODEL}' not found. Please ensure it's installed.")
            return None
        except Exception as e:
            self.logger.error(f"Error loading Spacy model '{self.SCISPACY_MODEL}': {e}", exc_info=True)
            return None

    def _get_filtered_db_matches(self, entity_text: str, entity_label: str = "UNKNOWN") -> List[Dict[str, Any]]:
        """
        Finds concepts in the DB, filters by semantic type/label, ranks by similarity.
        Uses class attributes for configuration (TARGET_SOURCE, etc.).
        Uses module-level logger for internal debug logging if needed.
        """
        # logger.debug(f"Looking up term: '{entity_text}' (Label: {entity_label}) for SAB='{self.TARGET_SOURCE}'")
        db_results_exact = find_concept_by_term_and_source(
            term=entity_text, source_sab=self.TARGET_SOURCE, exact_match=True, case_sensitive=False, limit=5
        )

        db_results = db_results_exact
        if not db_results_exact:
            # logger.debug(f"No exact match for '{entity_text}', trying partial match...")
            db_results_partial = find_concept_by_term_and_source(
                term=entity_text, source_sab=self.TARGET_SOURCE, exact_match=False, case_sensitive=False, limit=20
            )
            db_results = db_results_partial
            if not db_results:
                # logger.debug(f"No partial DB match found for '{entity_text}' in {self.TARGET_SOURCE}")
                return []
        # else: logger.debug(f"Found {len(db_results)} exact matches for '{entity_text}'.")

        # logger.debug(f"Processing {len(db_results)} DB candidates for '{entity_text}'. Filtering and Ranking...")
        filtered_matches = []
        processed_cuis_for_term = set()
        allowed_sem_types = self.LABEL_TO_SEMANTIC_TYPES.get(entity_label) if entity_label != "UNKNOWN" else None
        if not allowed_sem_types and entity_label != "UNKNOWN":
            logger.warning(f"No semantic type mapping defined for NER label '{entity_label}'. Filtering reduced.")

        entity_text_lower = entity_text.lower()

        for cui, matched_term, code in db_results:
            if cui in processed_cuis_for_term: continue

            semantic_types_raw = get_semantic_types_for_cui(cui)
            sem_type_names = [st[1] for st in semantic_types_raw if isinstance(st, tuple) and len(st) >= 2] if semantic_types_raw else []

            # Filtering Logic
            if any(st_name in self.EXCLUDE_SEMANTIC_TYPES for st_name in sem_type_names):
                # logger.debug(f"  Match CUI {cui} ('{matched_term}') FAILED filter: Excluded semantic type.")
                continue
            if allowed_sem_types:
                 if not sem_type_names or not any(st_name in allowed_sem_types for st_name in sem_type_names):
                      # logger.debug(f"  Match CUI {cui} ('{matched_term}') FAILED label filter.")
                      continue

            # Scoring Logic
            score = 0
            matched_term_lower = matched_term.lower()
            exact_match_bonus = 1000 # Use threshold as bonus
            is_exact_match = (matched_term_lower == entity_text_lower)
            if is_exact_match: score += exact_match_bonus
            similarity_score = fuzz.ratio(entity_text_lower, matched_term_lower)
            score += similarity_score
            length_diff = len(matched_term) - len(entity_text)
            if length_diff > 5 and not is_exact_match: score -= length_diff * 0.25 # Small penalty for longer inexact matches

            # logger.debug(f"  Calculating score for CUI {cui} ('{matched_term}'): Score={score:.2f}")
            match_data = {'cui': cui, 'matched_term': matched_term, 'code': code, 'sem_types': sem_type_names, 'score': score}
            filtered_matches.append(match_data)
            processed_cuis_for_term.add(cui)

        # Sorting
        if filtered_matches:
            filtered_matches.sort(key=lambda x: x['score'], reverse=True)
            # logger.debug(f"Sorted {len(filtered_matches)} matches for '{entity_text}'. Best: {filtered_matches[0]['matched_term']} (Score: {filtered_matches[0]['score']:.2f})")
        # else: logger.debug(f"No matches remained after filtering for '{entity_text}'.")

        return filtered_matches

    def process(self, phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Processes a single input phrase using ScispaCy NER, fallback, and relation extraction.

        Args:
            phrase: The input text phrase.
            log_func: Callback for sending logs to the client.

        Returns:
            Dictionary with 'concepts' and 'relations' lists.
        """
        # Log helper function
        def _log(msg: str, level: int = logging.INFO):
            # Send to client if callback provided
            if log_func:
                log_func(msg)
            # Also log to backend logger (using self.logger)
            self.logger.log(level, msg)

        if not self.nlp:
            error_msg = "Pipeline not initialized (ScispaCy model failed to load). Cannot process phrase."
            _log(f"ERROR: {error_msg}", level=logging.ERROR)
            return {'concepts': [], 'relations': []}

        _log(f"Processing phrase: '{phrase}'")

        doc = None
        try:
            _log("Starting NLP processing...", level=logging.DEBUG)
            doc = self.nlp(phrase)
            _log("NLP processing complete.", level=logging.DEBUG)
        except Exception as e:
            error_msg = f"Error during NLP processing for phrase '{phrase}': {e}"
            self.logger.error(error_msg, exc_info=True) # Log full error internally
            _log(f"ERROR: NLP processing failed.") # Inform client concisely
            return {'concepts': [], 'relations': []}

        final_concepts = []
        processed_cuis_in_phrase: Set[str] = set()
        processed_spans: List[Tuple[int, int]] = []

        ner_entities_log = [(ent.text, ent.label_, ent.start_char, ent.end_char) for ent in doc.ents]
        _log(f"Found {len(ner_entities_log)} NER entities: {ner_entities_log}", level=logging.INFO)

        # --- Step 1: Concept Extraction from Primary NER ---
        _log("Starting NER-based concept extraction...", level=logging.DEBUG)
        ner_concepts_found = 0
        for ent in doc.ents:
            entity_text = ent.text
            entity_label = ent.label_
            start_char = ent.start_char
            end_char = ent.end_char
            processed_spans.append((start_char, end_char))

            try:
                # _log(f"  Looking up NER entity: '{entity_text}' ({entity_label})", level=logging.DEBUG)
                ranked_db_matches = self._get_filtered_db_matches(entity_text, entity_label)
                if ranked_db_matches:
                    best_match = ranked_db_matches[0]
                    cui = best_match['cui']
                    score = best_match['score']
                    if cui not in processed_cuis_in_phrase:
                        msg = f"[NER] Found: '{best_match['matched_term']}' (CUI={cui}, Score={score:.0f}) for '{entity_text}'"
                        _log(f"  {msg}") # Log detailed match to client
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
                    # else: logger.debug(f"[NER] Skipping CUI {cui} for entity '{entity_text}' as already added.")
                # else: logger.info(f"[NER] No suitable NCI concept match found for entity '{entity_text}' ({entity_label}).")
            except Exception as e:
                error_msg = f"Error processing NER entity '{entity_text}': {e}"
                self.logger.error(error_msg, exc_info=True) # Log full error internally
                _log(f"ERROR: Processing entity '{entity_text}' failed.") # Inform client concisely

        _log(f"NER concept extraction finished. Found {ner_concepts_found} new concepts.", level=logging.INFO)

        # --- Step 2: Fallback Concept Detection using Tokens ---
        _log("Starting fallback concept detection using individual tokens...", level=logging.DEBUG)
        fallback_concepts_found = 0
        try:
            for token in doc:
                token_start, token_end, token_text, token_pos = token.idx, token.idx + len(token.text), token.text, token.pos_

                is_overlapping = any(overlaps(token_start, token_end, p_start, p_end) for p_start, p_end in processed_spans)
                if is_overlapping or token_pos not in self.FALLBACK_POS_TAGS:
                    continue

                # logger.debug(f"Processing non-overlapping token: '{token_text}' (POS: {token_pos})")
                ranked_fallback_matches = self._get_filtered_db_matches(token_text, entity_label="UNKNOWN")

                if ranked_fallback_matches:
                    best_fallback_match = ranked_fallback_matches[0]
                    fallback_cui, fallback_score = best_fallback_match['cui'], best_fallback_match['score']

                    # Strict fallback requires score >= exact match bonus
                    if fallback_score >= self.FALLBACK_SCORE_THRESHOLD:
                        if fallback_cui not in processed_cuis_in_phrase:
                            msg = f"[Fallback] Found: '{best_fallback_match['matched_term']}' (CUI={fallback_cui}, Score={fallback_score:.0f}) for token '{token_text}'"
                            _log(f"  {msg}") # Log detailed match to client
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
                            processed_spans.append((token_start, token_end)) # Mark span as processed
                            fallback_concepts_found += 1
                        # else: logger.debug(f"[Fallback] Skipping CUI {fallback_cui} for token '{token_text}' as already added.")
                    # else: logger.debug(f"[Fallback] Match for '{token_text}' score ({fallback_score:.2f}) below threshold.")
        except Exception as e:
             error_msg = f"Error during fallback token processing: {e}"
             self.logger.error(error_msg, exc_info=True) # Log full error internally
             _log(f"ERROR: Fallback processing failed.") # Inform client concisely

        _log(f"Fallback concept detection finished. Found {fallback_concepts_found} additional concepts.", level=logging.INFO)
        _log(f"Total concepts found: {len(final_concepts)}", level=logging.INFO)

        # --- Step 3: Relation Extraction ---
        extracted_relations = []
        parser_available = self.nlp and 'parser' in self.nlp.pipe_names

        if len(final_concepts) >= 2 and parser_available:
            _log("Starting relation extraction...", level=logging.DEBUG)
            try:
                # Sort concepts by start_char before passing
                final_concepts.sort(key=lambda x: x['start_char'])
                # Call the imported function from relation_extractor.py
                extracted_relations = extract_relations_hybrid(doc, final_concepts, log_func) # Pass log_func down
                _log(f"Relation extraction finished. Found {len(extracted_relations)} relations.", level=logging.INFO)
            except ImportError as e:
                 error_msg = f"Could not import relation extraction function: {e}"
                 self.logger.error(error_msg, exc_info=True)
                 _log(f"ERROR: {error_msg}")
            except Exception as e:
                error_msg = f"An error occurred during relation extraction: {e}"
                self.logger.error(error_msg, exc_info=True) # Log full error internally
                _log(f"ERROR: Relation extraction failed.") # Inform client concisely
        elif len(final_concepts) < 2:
            _log("Skipping relation extraction (less than 2 concepts found).")
        elif not parser_available:
            warn_msg = "Skipping relation extraction: parser component not available."
            _log(f"WARNING: {warn_msg}")

        # --- Step 4: Return combined results ---
        # Sort concepts again before returning for consistent final output
        final_concepts.sort(key=lambda x: x['start_char'])
        _log("Processing complete. Returning results.", level=logging.INFO)
        return {
            'concepts': final_concepts,
            'relations': extracted_relations
        }

# --- END OF FILE pipelines/spacy_rule_based/processor.py ---