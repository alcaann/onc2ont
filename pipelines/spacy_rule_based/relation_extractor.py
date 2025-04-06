# --- START OF UPDATED pipelines/spacy_rule_based/relation_extractor.py ---

import spacy
from spacy.tokens import Doc, Span, Token
from typing import List, Dict, Tuple, Optional, Any, Set, Callable # Added Callable
import logging
import itertools

# Corrected import assuming 'scripts' is in PYTHONPATH
try:
    # Make sure scripts/__init__.py exists
    from scripts.db_utils import get_relations_for_cui_pair
except ImportError:
    logging.error("Failed to import 'get_relations_for_cui_pair' from scripts.db_utils. KB lookup inactive.")
    # Define a dummy function if import fails
    def get_relations_for_cui_pair(*args, **kwargs) -> List: return []

# Module-level logger for internal errors/debug
logger = logging.getLogger(__name__)

# --- Configuration --- (Keep RELATION_LABELS, PREPOSITION_MAP, VERB_MAP, KB Config etc. as before) ---
RELATION_LABELS = {
    "FINDS": "FINDS", "HAS_FINDING": "HAS_FINDING", "HAS_LOCATION": "HAS_LOCATION",
    "LOCATED_AT": "LOCATED_AT", "METASTASIZED_TO": "METASTASIZED_TO", "ASSOCIATED_WITH": "ASSOCIATED_WITH",
    "TREATS": "TREATS", "TREATED_BY": "TREATED_BY", "CAUSES": "CAUSES", "CAUSED_BY": "CAUSED_BY",
    "HAS_STAGE": "HAS_STAGE", "HAS_PROPERTY": "HAS_PROPERTY", "PROCEDURE_FOR": "PROCEDURE_FOR",
    "HAS_HISTORY_OF": "HAS_HISTORY_OF", "PART_OF": "PART_OF", "UNKNOWN": "UNKNOWN_RELATION"
}

PREPOSITION_MAP = {
    "to": (RELATION_LABELS["LOCATED_AT"], {"metastatic": RELATION_LABELS["METASTASIZED_TO"], "spread": RELATION_LABELS["METASTASIZED_TO"]}),
    "in": (RELATION_LABELS["LOCATED_AT"], {}),
    "on": (RELATION_LABELS["LOCATED_AT"], {}),
    "of": (RELATION_LABELS["HAS_FINDING"], {"history": RELATION_LABELS["HAS_HISTORY_OF"]}),
    "with": (RELATION_LABELS["ASSOCIATED_WITH"], {}),
    "due to": (RELATION_LABELS["CAUSED_BY"], {}),
    "for": (RELATION_LABELS["TREATS"], {"biopsy": RELATION_LABELS["PROCEDURE_FOR"], "diagnosis": RELATION_LABELS["PROCEDURE_FOR"]}),
}

VERB_MAP = {
    "treat": (RELATION_LABELS["TREATS"], RELATION_LABELS["TREATED_BY"]),
    "show": (RELATION_LABELS["FINDS"], RELATION_LABELS["FINDS"]),
    "find": (RELATION_LABELS["FINDS"], RELATION_LABELS["FINDS"]),
    "reveal": (RELATION_LABELS["FINDS"], RELATION_LABELS["FINDS"]),
    "indicate": (RELATION_LABELS["HAS_FINDING"], RELATION_LABELS["HAS_FINDING"]),
    "diagnose": (RELATION_LABELS["HAS_FINDING"], RELATION_LABELS["HAS_FINDING"]),
    "cause": (RELATION_LABELS["CAUSES"], RELATION_LABELS["CAUSED_BY"]),
    "associate": (RELATION_LABELS["ASSOCIATED_WITH"], RELATION_LABELS["ASSOCIATED_WITH"]),
    "metastasize": (RELATION_LABELS["METASTASIZED_TO"], RELATION_LABELS["METASTASIZED_TO"]),
    "include": (RELATION_LABELS["PART_OF"], RELATION_LABELS["PART_OF"]),
}

KB_SAB_PRIORITY = ['NCI', 'SNOMEDCT_US']
KB_RELA_PRIORITY = ['metastasis_to', 'disease_has_finding', 'finding_of_disease', 'disease_has_primary_anatomic_site',
                    'disease_has_associated_morphology', 'may_treat', 'may_be_treated_by', 'causative_agent_of',
                    'has_finding_site', 'has_assoc_finding', 'associated_with']
KB_REL_IGNORE = {'RB', 'RN', 'SY', 'RL', 'SUBSET_MEMBER', 'mapped_from', 'mapped_to'}

EXCLUDE_CUIS_FROM_RELATIONS = {"C1705908", "C0030705"} # Patient CUIs
# EXCLUDE_SEM_TYPES_FROM_RELATIONS = {"Conceptual Entity", "Classification", "Organism"}


# --- Helper Functions --- (map_concepts_to_spans, _get_token_context remain mostly the same)
def map_concepts_to_spans(doc: Doc, concepts: List[Dict[str, Any]]) -> Dict[str, List[Span]]:
    # (Keep implementation as before, logging errors internally)
    cui_to_spans_map: Dict[str, List[Span]] = {}
    processed_indices: Set[int] = set()
    for idx, concept in enumerate(concepts):
        if idx in processed_indices: continue
        try:
            span = doc.char_span(concept['start_char'], concept['end_char'], label=concept['cui'], alignment_mode="expand")
            if span:
                cui = concept['cui']
                if cui not in cui_to_spans_map: cui_to_spans_map[cui] = []
                if span not in cui_to_spans_map[cui]: cui_to_spans_map[cui].append(span)
                processed_indices.add(idx)
            # else: logger.warning(f"Could not create span for concept {concept.get('cui')}: '{concept.get('text_span')}' at [{concept.get('start_char')},{concept.get('end_char')})")
        except Exception as e: logger.error(f"Error mapping concept {concept.get('cui', 'N/A')} to span: {e}", exc_info=True)
    return cui_to_spans_map

def _get_token_context(token: Token, window: int = 3) -> List[str]:
    # (Keep implementation as before)
    doc = token.doc
    start = max(0, token.i - window)
    end = min(len(doc), token.i + window + 1)
    return [t.lemma_.lower() for t in doc[start:end]]

# --- Rule-Based Relation Extraction ---

def apply_dependency_rules(doc: Doc, span1: Span, span2: Span,
                           concept_details: Dict[str, Dict],
                           log_func: Optional[Callable[[str], None]] = None) -> Optional[Tuple[str, str, str]]:
    """Applies dependency rules to find relations between two concept spans."""
    def _log(msg: str): # Local helper for consistency
        if log_func: log_func(f"  [Rule] {msg}")
        logger.info(f"[Rule] {msg}") # Log rule matches to backend too

    if not span1 or not span2 or not span1.root or not span2.root: return None
    cui1 = span1.label_; cui2 = span2.label_
    root1 = span1.root; root2 = span2.root
    if root1 == root2: return None

    # --- Rule 1: Prepositional Phrase Attachment ---
    for subj_span, obj_span, subj_cui, obj_cui in [(span1, span2, cui1, cui2), (span2, span1, cui2, cui1)]:
        subj_root = subj_span.root; obj_root = obj_span.root
        if obj_root.dep_ == 'pobj' and obj_root.head.dep_ == 'prep':
            prep_token = obj_root.head; prep_lemma = prep_token.lemma_.lower()
            modified_token = prep_token.head
            is_related = (modified_token == subj_root or subj_root.head == modified_token or subj_root.head == modified_token) # Simplified check
            if is_related and prep_lemma in PREPOSITION_MAP:
                default_relation, context_map = PREPOSITION_MAP[prep_lemma]; relation = default_relation
                context_lemmas = _get_token_context(modified_token)
                for keyword, specific_relation in context_map.items():
                    if keyword in context_lemmas: relation = specific_relation; break
                _log(f"Prep Match ({prep_lemma}): {subj_cui} ({subj_root.text}) --[{relation}]--> {obj_cui} ({obj_root.text})")
                return subj_cui, relation, obj_cui

    # --- Rule 2: Verb Mediated ---
    for root_subj, root_obj, cui_subj, cui_obj in [(root1, root2, cui1, cui2), (root2, root1, cui2, cui1)]:
        verb = root_subj.head
        if verb.pos_ == 'VERB' and root_obj.head == verb:
            verb_lemma = verb.lemma_.lower()
            if verb_lemma in VERB_MAP:
                active_rel, passive_rel = VERB_MAP[verb_lemma]; subj_dep = root_subj.dep_; obj_dep = root_obj.dep_
                relation = passive_rel if subj_dep.startswith('nsubjpass') else active_rel
                if subj_dep.startswith(('nsubj', 'nsubjpass')) and obj_dep in ['dobj', 'pobj', 'iobj', 'obj', 'attr', 'oprd', 'agent']: # Added agent for passive
                     log_subj, log_obj, log_rel = cui_subj, cui_obj, relation
                     if verb_lemma == 'include': log_subj, log_obj = cui_obj, cui_subj # Handle include direction
                     _log(f"Verb Match ({verb_lemma}/{subj_dep}): {log_subj} ({root_subj.text}) --[{log_rel}]--> {log_obj} ({root_obj.text})")
                     return log_subj, log_rel, log_obj

    # --- Rule 3: Direct Modifiers (Amod only) ---
    for head_span, mod_span, head_cui, mod_cui in [(span1, span2, cui1, cui2), (span2, span1, cui2, cui1)]:
        head_root = head_span.root; mod_root = mod_span.root
        if mod_root.head == head_root and mod_root.dep_ == 'amod':
             relation = RELATION_LABELS["HAS_PROPERTY"]
             _log(f"Amod Match: {head_cui} ({head_span.text}) --[{relation}]--> {mod_cui} ({mod_span.text})")
             return head_cui, relation, mod_cui

    # --- Rule 4: Conjunction (Simplified Logic) ---
    if root1.dep_ == 'conj' and root2.dep_ == 'conj' and root1.head == root2.head:
        relation = RELATION_LABELS["ASSOCIATED_WITH"]
        _log(f"Conj Match: {cui1} ({span1.text}) --[{relation}]--> {cui2} ({span2.text})")
        return cui1, relation, cui2

    # --- Rule 5: Staging ---
    disease_type = "Neoplastic Process"; stage_cui_label = "C0205390" # Stage CUI
    for disease_span, stage_span, disease_cui, stage_cui_val in [(span1, span2, cui1, cui2), (span2, span1, cui2, cui1)]:
        is_disease = disease_type in concept_details.get(disease_cui, {}).get('sem_types', [])
        is_stage_concept = stage_cui_val == stage_cui_label
        if is_disease and is_stage_concept and disease_span.end <= stage_span.start:
            next_token_idx = stage_span.end; next_token = None
            if next_token_idx < len(doc): next_token = doc[next_token_idx]
            if next_token and next_token.is_punct and next_token_idx + 1 < len(doc): next_token = doc[next_token_idx + 1] # Skip one punctuation
            if next_token and (next_token.like_num or next_token.pos_ == 'NUM' or next_token.text in ['I', 'II', 'III', 'IV', 'V']):
                 relation = RELATION_LABELS['HAS_STAGE']
                 _log(f"Stage Match: {disease_cui} ({disease_span.text}) --[{relation}]--> {stage_cui_val} ({stage_span.text} {next_token.text})")
                 return disease_cui, relation, stage_cui_val
    return None

# --- Knowledge Base Relation Lookup ---

def select_best_kb_relation(kb_relations: List[Dict[str, str]], cui1: str, cui2: str) -> Optional[Dict[str, str]]:
    # (Keep implementation as before, no client logging needed here)
    if not kb_relations: return None
    eligible_relations = [rel for rel in kb_relations if rel.get('rel') not in KB_REL_IGNORE and rel.get('rela') not in KB_REL_IGNORE]
    if not eligible_relations: return None
    def get_sort_key(relation):
        sab = relation.get('sab', ''); rela = relation.get('rela', ''); rel = relation.get('rel', '')
        sab_priority = KB_SAB_PRIORITY.index(sab) if sab in KB_SAB_PRIORITY else len(KB_SAB_PRIORITY)
        rela_priority = KB_RELA_PRIORITY.index(rela) if rela in KB_RELA_PRIORITY else (KB_RELA_PRIORITY.index(rel) if rel in KB_RELA_PRIORITY else len(KB_RELA_PRIORITY))
        has_rela = 0 if rela else 1 # Prioritize relations with RELA
        return (sab_priority, rela_priority, has_rela)
    eligible_relations.sort(key=get_sort_key)
    return eligible_relations[0]

def lookup_kb_relation(cui1: str, cui2: str,
                       log_func: Optional[Callable[[str], None]] = None) -> Optional[Tuple[str, str, str, str]]:
    """Looks up relations in MRREL and selects the best one."""
    def _log(msg: str): # Local helper
        if log_func: log_func(f"  [KB] {msg}")
        logger.info(f"[KB] {msg}") # Log KB matches to backend too

    try:
        target_sabs = tuple(KB_SAB_PRIORITY)
        # _log(f"Querying MRREL for ({cui1}, {cui2}) in SABs: {target_sabs}") # Debug log
        kb_relations_raw = get_relations_for_cui_pair(cui1, cui2, target_sabs=target_sabs, limit=20)
    except Exception as e:
        logger.error(f"Error calling get_relations_for_cui_pair for ({cui1}, {cui2}): {e}", exc_info=True)
        return None

    best_relation_data = select_best_kb_relation(kb_relations_raw, cui1, cui2)
    if best_relation_data:
        relation_label = best_relation_data.get('rela') or best_relation_data.get('rel') or RELATION_LABELS["UNKNOWN"]
        subj = best_relation_data.get('cui1'); obj = best_relation_data.get('cui2'); sab = best_relation_data.get('sab')
        if subj and obj and relation_label and sab:
            _log(f"Found: {subj} --[{relation_label} ({sab})]--> {obj}")
            return subj, relation_label, obj, sab
    # else: _log(f"No suitable KB relation found for ({cui1}, {cui2})") # Optional debug log

    return None

# --- Main Hybrid Relation Extraction Function ---

def extract_relations_hybrid(doc: Doc, concepts: List[Dict[str, Any]],
                              log_func: Optional[Callable[[str], None]] = None) -> List[Dict[str, Any]]:
    """
    Orchestrates relation extraction using rules and KB lookup.

    Args:
        doc: The processed spaCy Doc object.
        concepts: List of extracted concept dictionaries.
        log_func: Optional callback for client-side logging.

    Returns:
        List of relation dictionaries.
    """
    # --- Log Helper ---
    def _log_helper(msg: str, level: int = logging.INFO):
        if log_func: log_func(msg)
        # Log to backend logger as well (using module logger here)
        logger.log(level, msg)
    # ---

    relations: List[Dict[str, Any]] = []
    if len(concepts) < 2:
        _log_helper("Skipping relation extraction (less than 2 concepts).", logging.DEBUG)
        return relations

    _log_helper("Mapping concepts to spans...", level=logging.DEBUG)
    cui_to_spans_map = map_concepts_to_spans(doc, concepts)
    if not cui_to_spans_map:
        _log_helper("No concepts could be mapped to spans. Skipping relation extraction.", logging.WARNING)
        return relations
    concept_details = {c['cui']: c for c in concepts} # Map CUI to full concept dict for easy access

    # --- Filter concepts eligible for relations ---
    eligible_cuis = []
    for cui, concept_dict in concept_details.items():
        if cui in EXCLUDE_CUIS_FROM_RELATIONS:
            # _log_helper(f"Excluding CUI {cui} ({concept_dict.get('text_span')}) from relations.", level=logging.DEBUG)
            continue
        # Add semantic type filtering here if needed later
        eligible_cuis.append(cui)

    if len(eligible_cuis) < 2:
        _log_helper(f"Not enough eligible concepts ({len(eligible_cuis)}) for relation extraction.", level=logging.INFO)
        return relations

    _log_helper(f"Attempting relation extraction between {len(eligible_cuis)} eligible concepts: {eligible_cuis}", level=logging.INFO)
    processed_pairs: Set[Tuple[str, str]] = set()

    # --- Iterate through pairs of eligible concepts ---
    for cui1, cui2 in itertools.combinations(eligible_cuis, 2):
        pair_key = tuple(sorted((cui1, cui2)))
        if pair_key in processed_pairs: continue

        spans1 = cui_to_spans_map.get(cui1, [])
        spans2 = cui_to_spans_map.get(cui2, [])
        if not spans1 or not spans2: continue # Skip if one CUI didn't map to spans

        found_rule_relation = False
        # --- Apply Rules ---
        for span1 in spans1:
            if found_rule_relation: break
            for span2 in spans2:
                if not span1 or not span2 or span1.start == span2.start: continue # Basic sanity check
                try:
                    # Pass log_func down to rules function
                    rule_result = apply_dependency_rules(doc, span1, span2, concept_details, log_func)
                    if rule_result:
                        subj_cui, rel_label, obj_cui = rule_result
                        relations.append({
                            'subject_cui': subj_cui,
                            'relation': rel_label,
                            'object_cui': obj_cui,
                            'source': 'rule-based',
                            'subj_text': concept_details.get(subj_cui, {}).get('text_span', '??'), # Add text spans for context
                            'obj_text': concept_details.get(obj_cui, {}).get('text_span', '??')
                        })
                        found_rule_relation = True
                        processed_pairs.add(pair_key)
                        break # Found relation for this pair via rules, move to next pair
                except Exception as e:
                    # Log internal error, don't crash relation extraction
                    logger.error(f"Error applying rules between '{span1.text}' ({cui1}) and '{span2.text}' ({cui2}): {e}", exc_info=False) # Keep trace minimal

        # --- Fallback to KB Lookup ---
        if not found_rule_relation:
            # Pass log_func down to KB lookup function
            kb_result = lookup_kb_relation(cui1, cui2, log_func)
            if kb_result:
                subj_cui, rel_label, obj_cui, sab = kb_result
                relations.append({
                    'subject_cui': subj_cui,
                    'relation': rel_label,
                    'object_cui': obj_cui,
                    'source': f'kb-lookup ({sab})', # Indicate source vocabulary
                    'subj_text': concept_details.get(subj_cui, {}).get('text_span', '??'),
                    'obj_text': concept_details.get(obj_cui, {}).get('text_span', '??')
                })
                processed_pairs.add(pair_key)

    _log_helper(f"Finished relation extraction. Found {len(relations)} relations.", level=logging.INFO)
    return relations

# --- Standalone Testing ---
if __name__ == '__main__':
    # Can add basic test cases here if needed
    print("Relation Extractor module (pipelines/spacy_rule_based/relation_extractor.py) loaded.")
    print("Run integration tests via the main API.")

# --- END OF UPDATED pipelines/spacy_rule_based/relation_extractor.py ---