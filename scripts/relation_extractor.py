# /app/scripts/relation_extractor.py

import spacy
from spacy.tokens import Doc, Span, Token
from typing import List, Dict, Tuple, Optional, Any, Set
import logging
import itertools

try:
    from db_utils import get_relations_for_cui_pair
except ImportError:
    logging.error("Failed to import 'get_relations_for_cui_pair'. KB lookup inactive.")
    def get_relations_for_cui_pair(*args, **kwargs) -> List: return []

logger = logging.getLogger(__name__)

# --- Configuration (Keep RELATION_LABELS, VERB_MAP, KB Config etc. as before) ---
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
# EXCLUDE_SEM_TYPES_FROM_RELATIONS = {"Conceptual Entity", "Classification", "Organism"} # Keep this commented for now


# --- Helper Functions --- (map_concepts_to_spans, _get_token_context remain the same)
def map_concepts_to_spans(doc: Doc, concepts: List[Dict[str, Any]]) -> Dict[str, List[Span]]:
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
        except Exception as e: logger.error(f"Error mapping concept {concept.get('cui', 'N/A')} to span: {e}", exc_info=True)
    return cui_to_spans_map

def _get_token_context(token: Token, window: int = 3) -> List[str]:
    doc = token.doc
    start = max(0, token.i - window)
    end = min(len(doc), token.i + window + 1)
    return [t.lemma_.lower() for t in doc[start:end]]

# --- Rule-Based Relation Extraction ---

def apply_dependency_rules(doc: Doc, span1: Span, span2: Span,
                          concept_details: Dict[str, Dict]) -> Optional[Tuple[str, str, str]]:
    if not span1 or not span2 or not span1.root or not span2.root: return None
    cui1 = span1.label_; cui2 = span2.label_
    root1 = span1.root; root2 = span2.root
    if root1 == root2: return None

    # --- Rule 1: Prepositional Phrase Attachment (Attempt 3) ---
    # Check if obj is pobj of prep, and prep's head is subj or subj's head (often a verb)
    for subj_span, obj_span, subj_cui, obj_cui in [(span1, span2, cui1, cui2), (span2, span1, cui2, cui1)]:
        subj_root = subj_span.root
        obj_root = obj_span.root
        if obj_root.dep_ == 'pobj' and obj_root.head.dep_ == 'prep':
            prep_token = obj_root.head
            prep_lemma = prep_token.lemma_.lower()
            modified_token = prep_token.head # The token the prep phrase modifies (e.g., 'cancer' in "cancer to the liver", 'pain' in "pain in abdomen")

            # Allow attachment if modified token is the subj_root OR if subj_root modifies it OR modified token is head of subj_root
            is_related_structurally = False
            if modified_token == subj_root:
                is_related_structurally = True
            elif subj_root.head == modified_token: # e.g. subj modifies the token (metastatic lung cancer)
                 is_related_structurally = True
            # Check if modified token is the head of the subject's root (common case where prep modifies verb)
            elif subj_root.head == modified_token:
                 is_related_structurally = True


            if is_related_structurally and prep_lemma in PREPOSITION_MAP:
                default_relation, context_map = PREPOSITION_MAP[prep_lemma]
                relation = default_relation
                context_lemmas = _get_token_context(modified_token) # Context around modified token
                for keyword, specific_relation in context_map.items():
                    if keyword in context_lemmas: relation = specific_relation; break

                if relation == RELATION_LABELS["HAS_HISTORY_OF"]:
                     logger.info(f"Rule Matched (Prep 'of history'): {subj_cui} ({subj_root.text}) --[{relation}]--> {obj_cui} ({obj_root.text})")
                     return subj_cui, relation, obj_cui
                else:
                    logger.info(f"Rule Matched (Prep): {subj_cui} ({subj_root.text}) --[{prep_lemma}:{relation}]--> {obj_cui} ({obj_root.text})")
                    return subj_cui, relation, obj_cui

    # --- Rule 2: Verb Mediated --- (Keep as is)
    for root_subj, root_obj, cui_subj, cui_obj in [(root1, root2, cui1, cui2), (root2, root1, cui2, cui1)]:
        verb = root_subj.head
        if verb.pos_ == 'VERB' and root_obj.head == verb:
            verb_lemma = verb.lemma_.lower()
            if verb_lemma in VERB_MAP:
                active_rel, passive_rel = VERB_MAP[verb_lemma]; subj_dep = root_subj.dep_; obj_dep = root_obj.dep_
                relation = active_rel
                if subj_dep.startswith('nsubj'):
                    if obj_dep in ['dobj', 'pobj', 'iobj', 'obj', 'attr', 'oprd']:
                         if verb_lemma == 'include':
                              logger.info(f"Rule Matched (Verb 'include'): {cui_obj} ({root_obj.text}) --[{relation}]--> {cui_subj} ({root_subj.text})")
                              return cui_obj, relation, cui_subj
                         else:
                              logger.info(f"Rule Matched (Verb Active): {cui_subj} ({root_subj.text}) --[{verb_lemma}:{relation}]--> {cui_obj} ({root_obj.text})")
                              return cui_subj, relation, cui_obj
                elif subj_dep.startswith('nsubjpass'):
                     relation = passive_rel
                     logger.info(f"Rule Matched (Verb Passive): {cui_subj} ({root_subj.text}) --[{verb_lemma}:{relation}]--> {cui_obj} ({root_obj.text})")
                     return cui_subj, relation, cui_obj

    # --- Rule 3: Direct Modifiers (Amod only) --- (Keep as is)
    for head_span, mod_span, head_cui, mod_cui in [(span1, span2, cui1, cui2), (span2, span1, cui2, cui1)]:
        head_root = head_span.root; mod_root = mod_span.root
        if mod_root.head == head_root:
            if mod_root.dep_ == 'amod':
                 relation = RELATION_LABELS["HAS_PROPERTY"]
                 logger.info(f"Rule Matched (Amod): {head_cui} ({head_span.text}) --[{relation}]--> {mod_cui} ({mod_span.text})")
                 return head_cui, relation, mod_cui

    # --- Rule 4: Conjunction (Simplified Logic) --- <<< MODIFIED >>>
    # If two roots share the same head and BOTH are conjuncts, assume association
    if root1.dep_ == 'conj' and root2.dep_ == 'conj' and root1.head == root2.head:
        # Basic check - assumes 'and' is the likely coordinator implicitly
        relation = RELATION_LABELS["ASSOCIATED_WITH"]
        logger.info(f"Rule Matched (Conj Simplified): {cui1} ({span1.text}) --[{relation}]--> {cui2} ({span2.text})")
        # Could check for 'cc' child of head between root1.i and root2.i if needed later
        return cui1, relation, cui2

    # --- Rule 5: Staging --- (Keep as is)
    disease_type = "Neoplastic Process"; stage_cui = "C0205390"
    for disease_span, stage_span, disease_cui, stage_cui_val in [(span1, span2, cui1, cui2), (span2, span1, cui2, cui1)]:
         if disease_type in concept_details.get(disease_cui, {}).get('sem_types', []) and stage_cui_val == stage_cui:
              if disease_span.end <= stage_span.start: # Allow non-immediate adjacency
                  # Check if next token after stage span is a number/numeral
                  next_token_idx = stage_span.end
                  if next_token_idx < len(doc):
                      next_token = doc[next_token_idx]
                      # Allow optional token like ':' or '-' between stage and number
                      if next_token.is_punct and next_token_idx + 1 < len(doc):
                          next_token = doc[next_token_idx + 1]

                      if next_token.like_num or next_token.pos_ == 'NUM' or next_token.text in ['I', 'II', 'III', 'IV', 'V']:
                           logger.info(f"Rule Matched (Stage): {disease_cui} ({disease_span.text}) --[{RELATION_LABELS['HAS_STAGE']}]--> {stage_cui_val} ({stage_span.text} {next_token.text})")
                           return disease_cui, RELATION_LABELS['HAS_STAGE'], stage_cui_val
    return None

# --- Knowledge Base Relation Lookup --- (Keep as is)
def select_best_kb_relation(kb_relations: List[Dict[str, str]], cui1: str, cui2: str) -> Optional[Dict[str, str]]:
    if not kb_relations: return None
    eligible_relations = [rel for rel in kb_relations if rel.get('rel') not in KB_REL_IGNORE and rel.get('rela') not in KB_REL_IGNORE]
    if not eligible_relations: return None
    def get_sort_key(relation):
        sab = relation.get('sab', ''); rela = relation.get('rela', ''); rel = relation.get('rel', '')
        sab_priority = KB_SAB_PRIORITY.index(sab) if sab in KB_SAB_PRIORITY else len(KB_SAB_PRIORITY)
        rela_priority = KB_RELA_PRIORITY.index(rela) if rela in KB_RELA_PRIORITY else (KB_RELA_PRIORITY.index(rel) if rel in KB_RELA_PRIORITY else len(KB_RELA_PRIORITY))
        has_rela = 0 if rela else 1
        return (sab_priority, rela_priority, has_rela)
    eligible_relations.sort(key=get_sort_key)
    return eligible_relations[0]

def lookup_kb_relation(cui1: str, cui2: str) -> Optional[Tuple[str, str, str, str]]:
    try: kb_relations_raw = get_relations_for_cui_pair(cui1, cui2, target_sabs=tuple(KB_SAB_PRIORITY), limit=20)
    except Exception as e: logger.error(f"Error calling get_relations_for_cui_pair: {e}", exc_info=True); return None
    best_relation_data = select_best_kb_relation(kb_relations_raw, cui1, cui2)
    if best_relation_data:
        relation_label = best_relation_data.get('rela') or best_relation_data.get('rel') or RELATION_LABELS["UNKNOWN"]
        subj = best_relation_data.get('cui1'); obj = best_relation_data.get('cui2'); sab = best_relation_data.get('sab')
        if subj and obj and relation_label and sab: logger.info(f"KB Found: {subj} --[{relation_label} ({sab})]--> {obj}"); return subj, relation_label, obj, sab
    return None

# --- Main Hybrid Relation Extraction Function --- (Keep as is)
def extract_relations_hybrid(doc: Doc, concepts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    relations = [];
    if len(concepts) < 2: return relations
    cui_to_spans_map = map_concepts_to_spans(doc, concepts);
    if not cui_to_spans_map: return relations
    concept_details = {c['cui']: c for c in concepts}

    eligible_cuis = []
    for cui, concept_dict in concept_details.items():
        if cui in EXCLUDE_CUIS_FROM_RELATIONS: continue
        # sem_types = set(concept_dict.get('sem_types', [])) # Keep filtering minimal for now
        # if sem_types.intersection(EXCLUDE_SEM_TYPES_FROM_RELATIONS): continue
        eligible_cuis.append(cui)

    if len(eligible_cuis) < 2: logger.info("Not enough eligible concepts (< 2) for relation extraction."); return relations

    logger.info(f"Attempting relation extraction between {len(eligible_cuis)} eligible concepts: {eligible_cuis}")
    processed_pairs = set()

    for cui1, cui2 in itertools.combinations(eligible_cuis, 2):
        pair_key = tuple(sorted((cui1, cui2)))
        if pair_key in processed_pairs: continue
        spans1 = cui_to_spans_map.get(cui1, []); spans2 = cui_to_spans_map.get(cui2, [])
        found_rule_relation = False
        for span1 in spans1:
            if found_rule_relation: break
            for span2 in spans2:
                if not span1 or not span2 or span1.start == span2.start: continue
                try:
                    rule_result = apply_dependency_rules(doc, span1, span2, concept_details)
                    if rule_result:
                        subj_cui, rel_label, obj_cui = rule_result
                        relations.append({'subject_cui': subj_cui, 'relation': rel_label, 'object_cui': obj_cui,
                                          'source': 'rule-based',
                                          'subj_text': concept_details.get(subj_cui, {}).get('text_span', '??'),
                                          'obj_text': concept_details.get(obj_cui, {}).get('text_span', '??')})
                        found_rule_relation = True; processed_pairs.add(pair_key); break
                except Exception as e: logger.error(f"Error applying rules between '{span1.text}' and '{span2.text}': {e}", exc_info=False)

        if not found_rule_relation:
            kb_result = lookup_kb_relation(cui1, cui2)
            if kb_result:
                subj_cui, rel_label, obj_cui, sab = kb_result
                relations.append({'subject_cui': subj_cui, 'relation': rel_label, 'object_cui': obj_cui,
                                  'source': f'kb-lookup ({sab})',
                                  'subj_text': concept_details.get(subj_cui, {}).get('text_span', '??'),
                                  'obj_text': concept_details.get(obj_cui, {}).get('text_span', '??')})
                processed_pairs.add(pair_key)

    logger.info(f"Finished relation extraction. Found {len(relations)} relations.")
    return relations

# --- Standalone Testing ---
if __name__ == '__main__':
    print("Relation Extractor module loaded. Run tests via process_phrase.py.")