# scripts/align_annotations.py

import logging
from typing import List, Dict, Tuple, Any, Optional

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def align_annotations(
    raw_entities: List[Dict[str, Any]],
    tokens: List[str],
    token_spans: List[Tuple[int, int]]
) -> Optional[Dict[str, Any]]:
    """
    Aligns character-based entity annotations to token indices and generates BIO tags.

    Args:
        raw_entities: A list of dictionaries, where each dictionary represents
                      an entity parsed from the .ann file. Expected keys:
                      'id', 'label', 'start_char', 'end_char', 'text'.
        tokens: A list of token strings from process_text.py.
        token_spans: A list of (start_char, end_char) tuples from process_text.py,
                     corresponding to each token.

    Returns:
        A dictionary containing:
        {
            "processed_entities": List[Dict[str, Any]] - The entity list updated
                                  with 'start_token' and 'end_token' keys.
            "bio_tags": List[str] - The list of BIO tags (e.g., "B-DISEASE",
                                  "I-DISEASE", "O"), one per token.
        }
        Returns None if inputs are invalid or a critical error occurs.
    """
    if not isinstance(raw_entities, list) or not isinstance(tokens, list) or not isinstance(token_spans, list):
        logging.error("Invalid input types for alignment.")
        return None
    if len(tokens) != len(token_spans):
        logging.error(f"Mismatch between token count ({len(tokens)}) and token span count ({len(token_spans)}).")
        return None

    processed_entities: List[Dict[str, Any]] = []
    num_tokens = len(tokens)
    bio_tags: List[str] = ['O'] * num_tokens # Initialize all tags to Outside

    # --- Step 1: Determine token spans for each entity ---
    for entity in raw_entities:
        entity_start_char = entity.get("start_char")
        entity_end_char = entity.get("end_char")
        entity_label = entity.get("label")
        entity_id = entity.get("id")

        if entity_start_char is None or entity_end_char is None or entity_label is None or entity_id is None:
            logging.warning(f"Skipping entity due to missing required fields: {entity}")
            continue

        start_token_idx = -1
        end_token_idx = -1

        # Find tokens that overlap with the entity's character span
        overlapping_token_indices = []
        for i, (token_start, token_end) in enumerate(token_spans):
            # Basic overlap condition: token starts before entity ends AND token ends after entity starts
            if token_start < entity_end_char and token_end > entity_start_char:
                 overlapping_token_indices.append(i)

        if not overlapping_token_indices:
            # This might happen if annotation spans are slightly off or tokenizer behaves unexpectedly
            logging.warning(f"Entity '{entity.get('text', '')}' ({entity_id}) "
                            f"at chars [{entity_start_char}:{entity_end_char}] "
                            f"did not align with any tokens. Spans: {token_spans}")
            # Add the entity without token spans, or skip it
            # Let's add it but mark token spans as -1
            updated_entity = entity.copy()
            updated_entity["start_token"] = -1
            updated_entity["end_token"] = -1
            processed_entities.append(updated_entity)
            continue # Skip BIO tagging for this entity

        # Determine the start and end token indices from the overlapping tokens
        start_token_idx = min(overlapping_token_indices)
        end_token_idx = max(overlapping_token_indices) # Inclusive end token index

        # Store the token indices back into a new dictionary
        updated_entity = entity.copy()
        updated_entity["start_token"] = start_token_idx
        updated_entity["end_token"] = end_token_idx
        processed_entities.append(updated_entity)


    # --- Step 2: Generate BIO tags based on token spans ---
    # Sort entities by start token index primarily, and then end token index secondarily (desc).
    # This helps prioritize longer entities if they start at the same token, although simple overwrite below is dominant.
    # If strict priority rules needed, implement them here.
    processed_entities.sort(key=lambda x: (x.get('start_token', -1), -x.get('end_token', -1)))

    for entity in processed_entities:
        start_token = entity.get("start_token")
        end_token = entity.get("end_token")
        label = entity.get("label")

        # Only apply tags if valid token indices were found
        if start_token is not None and end_token is not None and start_token >= 0 and end_token >= 0:
            if start_token < num_tokens:
                # Check if current tag is 'O' or if this entity starts inside another
                # Simple overwrite: always apply B-tag
                bio_tags[start_token] = f"B-{label}"
            else:
                 logging.warning(f"Entity {entity['id']} start token index {start_token} out of bounds (num_tokens={num_tokens}).")
                 continue # Skip tagging this invalid entity

            # Apply I-tags for subsequent tokens within the span
            for i in range(start_token + 1, min(end_token + 1, num_tokens)): # Ensure index stays within bounds
                 # Simple overwrite: always apply I-tag
                 bio_tags[i] = f"I-{label}"

            if end_token >= num_tokens:
                 logging.warning(f"Entity {entity['id']} end token index {end_token} out of bounds (num_tokens={num_tokens}).")


    return {
        "processed_entities": processed_entities,
        "bio_tags": bio_tags
    }

# --- Example Usage ---
if __name__ == "__main__":
    # --- Test Case 1: Simple ---
    print("--- Test Case 1: Simple ---")
    raw_entities_1 = [
        {'id': 'T1', 'label': 'DISEASE', 'start_char': 17, 'end_char': 31, 'text': 'stage II NSCLC'}
    ]
    tokens_1 = ['Pt', 'presents', 'with', 'stage', 'II', 'NSCLC', '.']
    token_spans_1 = [(0, 2), (3, 11), (12, 16), (17, 22), (23, 25), (26, 31), (31, 32)]
    alignment_result_1 = align_annotations(raw_entities_1, tokens_1, token_spans_1)
    if alignment_result_1:
        import json
        print("Processed Entities:")
        print(json.dumps(alignment_result_1["processed_entities"], indent=2))
        print("BIO Tags:", alignment_result_1["bio_tags"])
        # Expected BIO: ['O', 'O', 'O', 'B-DISEASE', 'I-DISEASE', 'I-DISEASE', 'O']

    print("\n--- Test Case 2: Multiple Entities & Relation (aligner ignores relations) ---")
    raw_entities_2 = [
        {'id': 'T1', 'label': 'TREATMENT', 'start_char': 10, 'end_char': 15, 'text': 'chemo'},
        {'id': 'T2', 'label': 'DISEASE', 'start_char': 20, 'end_char': 31, 'text': 'lung cancer'}
    ]
    # Note: Relations from parse_raw are not used by this alignment script directly
    tokens_2 = ['Underwent', 'chemo', 'for', 'lung', 'cancer', '.']
    token_spans_2 = [(0, 9), (10, 15), (16, 19), (20, 24), (25, 31), (31, 32)]
    alignment_result_2 = align_annotations(raw_entities_2, tokens_2, token_spans_2)
    if alignment_result_2:
        import json
        print("Processed Entities:")
        print(json.dumps(alignment_result_2["processed_entities"], indent=2))
        print("BIO Tags:", alignment_result_2["bio_tags"])
        # Expected BIO: ['O', 'B-TREATMENT', 'O', 'B-DISEASE', 'I-DISEASE', 'O']

    print("\n--- Test Case 3: Slightly offset annotation ---")
    raw_entities_3 = [
        # Annotation slightly inside token boundaries
        {'id': 'T1', 'label': 'DRUG', 'start_char': 1, 'end_char': 6, 'text': 'aspir'}
    ]
    tokens_3 = ['Take', 'aspirin', 'daily', '.']
    token_spans_3 = [(0, 4), (5, 12), (13, 18), (18, 19)]
    alignment_result_3 = align_annotations(raw_entities_3, tokens_3, token_spans_3)
    if alignment_result_3:
        import json
        print("Processed Entities:")
        print(json.dumps(alignment_result_3["processed_entities"], indent=2))
        print("BIO Tags:", alignment_result_3["bio_tags"])
        # Expected: Token 'aspirin' (index 1) overlaps.
        # Expected BIO: ['O', 'B-DRUG', 'O', 'O']

    print("\n--- Test Case 4: Misaligned annotation ---")
    raw_entities_4 = [
        # Annotation completely outside any token span (e.g., only whitespace)
        {'id': 'T1', 'label': 'MODIFIER', 'start_char': 4, 'end_char': 5, 'text': ' '}
    ]
    tokens_4 = ['Take', 'aspirin', 'daily', '.']
    token_spans_4 = [(0, 4), (5, 12), (13, 18), (18, 19)] # Note gap between token 0 and 1
    alignment_result_4 = align_annotations(raw_entities_4, tokens_4, token_spans_4)
    if alignment_result_4:
        import json
        print("Processed Entities:")
        print(json.dumps(alignment_result_4["processed_entities"], indent=2))
        print("BIO Tags:", alignment_result_4["bio_tags"])
        # Expected: Warning logged, entity added with token spans -1, BIO remains all 'O'
        # Expected BIO: ['O', 'O', 'O', 'O']