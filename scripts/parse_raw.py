# scripts/parse_raw.py

import pathlib
import logging
from typing import Dict, List, Optional, Any

# Configure logging
# You might want to move this configuration to your main script later
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def parse_raw_files(phrase_id: str, data_dir: str = "data/raw") -> Optional[Dict[str, Any]]:
    """
    Parses the corresponding .txt and .ann files for a given phrase ID
    from the specified raw data directory.

    Assumes BRAT-like standoff annotation format for .ann files:
    T<id>\t<Label> <StartChar> <EndChar>\t<TextSpan>
    R<id>\t<Label> Arg1:<T_id1> Arg2:<T_id2> ...

    Args:
        phrase_id: The identifier for the phrase (e.g., "phrase_001").
        data_dir: The path to the directory containing the raw .txt and .ann files.

    Returns:
        A dictionary containing the parsed data:
        {
            "phrase_id": str,
            "text": str,
            "entities": list[dict] # Each dict: {'id', 'label', 'start_char', 'end_char', 'text'}
            "relations": list[dict] # Each dict: {'id', 'label', 'args': {'Arg1': 'T1', 'Arg2': 'T2', ...}}
        }
        Returns None if the required .txt file is not found or if critical
        parsing errors occur in the .ann file. Logs warnings for recoverable issues.
    """
    base_path = pathlib.Path(data_dir)
    txt_file = base_path / f"{phrase_id}.txt"
    ann_file = base_path / f"{phrase_id}.ann"

    parsed_data: Dict[str, Any] = {
        "phrase_id": phrase_id,
        "text": None,
        "entities": [],
        "relations": []
    }

    # --- Read .txt file ---
    try:
        parsed_data["text"] = txt_file.read_text(encoding='utf-8')
        logging.info(f"Successfully read text file: {txt_file}")
    except FileNotFoundError:
        logging.error(f"Required text file not found: {txt_file}")
        return None # Text file is essential
    except Exception as e:
        logging.error(f"Error reading text file {txt_file}: {e}")
        return None

    # --- Read and parse .ann file ---
    if not ann_file.exists():
        logging.warning(f"Annotation file not found: {ann_file}. Proceeding with text only.")
        # If annotations are optional, we can return the data with empty lists
        return parsed_data # Return data with only text if .ann is missing but optional

    try:
        with open(ann_file, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                line_num = i + 1
                line = line.strip()
                if not line:
                    continue # Skip empty lines

                parts = line.split('\t')
                if not parts:
                    logging.warning(f"Skipping empty or invalid line {line_num} in {ann_file}")
                    continue

                identifier = parts[0]

                # --- Parse Entities (T lines) ---
                if identifier.startswith('T'):
                    if len(parts) != 3:
                         logging.warning(f"Skipping malformed entity line {line_num} in {ann_file} (expected 3 tab-separated parts): {line}")
                         continue
                    entity_id = identifier
                    label_span_part = parts[1]
                    entity_text = parts[2]

                    # Parse label and character spans
                    label_span_parts = label_span_part.split()
                    if len(label_span_parts) < 3: # Need at least Label, Start, End
                        logging.warning(f"Skipping malformed entity label/span part line {line_num} in {ann_file}: '{label_span_part}'")
                        continue

                    label = label_span_parts[0]
                    try:
                        # Assuming simple space separation. Start is second element, end is third.
                        # Note: BRAT can have discontinuous spans (e.g., "10 15;20 25"),
                        # this simple parser only handles continuous spans.
                        start_char = int(label_span_parts[1])
                        end_char = int(label_span_parts[2])

                        # Optional: Sanity check if the span text matches the source text
                        # extracted_text = parsed_data["text"][start_char:end_char]
                        # if extracted_text != entity_text:
                        #     logging.warning(f"Text mismatch line {line_num} in {ann_file}: Ann='{entity_text}', Actual='{extracted_text}' Span=({start_char}:{end_char})")

                    except (ValueError, IndexError) as e:
                         logging.warning(f"Error parsing entity span integers line {line_num} in {ann_file}: {line} - {e}")
                         continue

                    parsed_data["entities"].append({
                        "id": entity_id,       # e.g., "T1"
                        "label": label,        # e.g., "DISEASE"
                        "start_char": start_char,
                        "end_char": end_char,
                        "text": entity_text     # e.g., "stage II NSCLC"
                    })

                # --- Parse Relations (R lines) ---
                elif identifier.startswith('R'):
                    if len(parts) != 2:
                         logging.warning(f"Skipping malformed relation line {line_num} in {ann_file} (expected 2 tab-separated parts): {line}")
                         continue
                    relation_id = identifier
                    details_part = parts[1] # e.g., "TREATS Arg1:T1 Arg2:T2"

                    # Parse label and arguments
                    detail_parts = details_part.split()
                    if len(detail_parts) < 3: # Need at least Label Arg1 Arg2
                        logging.warning(f"Skipping malformed relation details line {line_num} in {ann_file} (expected Label ArgN:T_id ...): '{details_part}'")
                        continue

                    relation_label = detail_parts[0]
                    args = {}
                    valid_args = True
                    for arg_part in detail_parts[1:]:
                        arg_name_val = arg_part.split(':')
                        if len(arg_name_val) == 2:
                            # Key: Arg1, Arg2, etc. Value: T1, T2, etc.
                            args[arg_name_val[0]] = arg_name_val[1]
                        else:
                            logging.warning(f"Skipping malformed relation argument '{arg_part}' on line {line_num} in {ann_file}")
                            valid_args = False # Mark potential issue but might still parse others

                    if not args or not valid_args: # Ensure we parsed at least the expected arguments correctly
                        logging.warning(f"Relation line {line_num} in {ann_file} parsed with issues or no valid arguments: {line}")
                        # Decide whether to skip entirely or keep partially parsed args
                        # continue # Uncomment to skip if any arg is malformed

                    parsed_data["relations"].append({
                        "id": relation_id,         # e.g., "R1"
                        "label": relation_label,   # e.g., "TREATS"
                        "args": args               # e.g., {"Arg1": "T1", "Arg2": "T2"}
                    })

                # Add handlers for other BRAT types (E - Events, A - Attributes, N - Normalizations) if needed later
                # elif identifier.startswith('E'):
                #    pass
                # elif identifier.startswith('A'):
                #     pass

    except Exception as e:
        logging.error(f"Critical error reading or parsing annotation file {ann_file}: {e}", exc_info=True)
        # Depending on strictness, you might want to return None here
        # return None
        pass # Or continue with potentially partial annotations if some lines failed

    logging.info(f"Finished parsing for {phrase_id}. Found {len(parsed_data['entities'])} entities and {len(parsed_data['relations'])} relations.")
    return parsed_data

# --- Example Usage ---
if __name__ == "__main__":
    # Create dummy files in ./data/raw relative to this script for testing
    # Assumes you run this script directly from the 'scripts' directory
    # Or adjust the path accordingly
    script_dir = pathlib.Path(__file__).parent
    project_root = script_dir.parent
    test_data_dir = project_root / "data" / "raw"
    test_data_dir.mkdir(parents=True, exist_ok=True)

    # ---- Test Case 1: Simple Entity ----
    test_phrase_id_1 = "phrase_001"
    txt_path_1 = test_data_dir / f"{test_phrase_id_1}.txt"
    ann_path_1 = test_data_dir / f"{test_phrase_id_1}.ann"

    if not txt_path_1.exists():
        txt_path_1.write_text("Pt presents with stage II NSCLC.", encoding='utf-8')
        print(f"Created dummy file: {txt_path_1}")
    if not ann_path_1.exists():
         ann_path_1.write_text("T1\tDISEASE\t17\t31\tstage II NSCLC", encoding='utf-8') # Corrected tab separation
         print(f"Created dummy file: {ann_path_1}")

    # ---- Test Case 2: Entity and Relation ----
    test_phrase_id_2 = "phrase_002"
    txt_path_2 = test_data_dir / f"{test_phrase_id_2}.txt"
    ann_path_2 = test_data_dir / f"{test_phrase_id_2}.ann"
    if not txt_path_2.exists():
        txt_path_2.write_text("Underwent chemo for lung cancer.", encoding='utf-8')
        print(f"Created dummy file: {txt_path_2}")
    if not ann_path_2.exists():
        # Ensure using TABS between main sections, spaces within label/span section if needed
        ann_content = "T1\tTREATMENT\t10\t15\tchemo\n" \
                      "T2\tDISEASE\t20\t31\tlung cancer\n" \
                      "R1\tTREATS\tArg1:T1 Arg2:T2"
        ann_path_2.write_text(ann_content, encoding='utf-8')
        print(f"Created dummy file: {ann_path_2}")


    # --- Run the parser ---
    print(f"\n--- Parsing {test_phrase_id_1} ---")
    parsed_result_1 = parse_raw_files(test_phrase_id_1, str(test_data_dir)) # Pass dir as string
    if parsed_result_1:
        import json
        print(json.dumps(parsed_result_1, indent=2))
    else:
        print(f"Parsing failed for {test_phrase_id_1}.")

    print(f"\n--- Parsing {test_phrase_id_2} ---")
    parsed_result_2 = parse_raw_files(test_phrase_id_2, str(test_data_dir))
    if parsed_result_2:
        import json
        print(json.dumps(parsed_result_2, indent=2))
    else:
        print(f"Parsing failed for {test_phrase_id_2}.")

    print("\n--- Testing Non-existent file ---")
    parsed_result_none = parse_raw_files("non_existent_id", str(test_data_dir))
    if parsed_result_none is None:
        print("Correctly returned None for non-existent text file.")
    else:
        print("Test failed: Should have returned None for missing text file.")