# scripts/process_text.py

import spacy
import logging
from typing import List, Tuple, Dict, Any, Optional

# Configure logging (optional, but good practice)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Load the spaCy model ---
# Loading the model can be time-consuming, so it's often better to load it
# once if you're processing many texts. For this self-contained script,
# we load it globally or pass it as an argument. Loading globally here for simplicity.
# Consider using a more specialized model like 'en_core_sci_sm' from scispaCy
# later if dealing with complex clinical/scientific text.
NLP_MODEL_NAME = "en_core_web_sm"
try:
    # Using disable=['parser', 'ner'] can speed things up if you only need tokenization.
    # Add 'tagger' back if you need Part-of-Speech tags later.
    nlp = spacy.load(NLP_MODEL_NAME, disable=['parser', 'ner', 'lemmatizer'])
    logging.info(f"spaCy model '{NLP_MODEL_NAME}' loaded successfully.")
except OSError:
    logging.error(f"spaCy model '{NLP_MODEL_NAME}' not found. ")
    logging.error(f"Please install it by running: python -m spacy download {NLP_MODEL_NAME}")
    # Depending on your setup, you might want to exit or raise an exception here
    nlp = None # Set to None to handle failure gracefully in the function

def process_text_spacy(text: str) -> Optional[Dict[str, Any]]:
    """
    Tokenizes the input text using spaCy and returns tokens and their character spans.

    Args:
        text: The raw input text string.

    Returns:
        A dictionary containing:
        {
            "tokens": List[str] - The list of token texts.
            "token_spans": List[Tuple[int, int]] - List of (start_char, end_char) tuples
                                                  corresponding to each token.
        }
        Returns None if the spaCy model failed to load or if input text is invalid.
    """
    if nlp is None:
        logging.error("spaCy model is not loaded. Cannot process text.")
        return None
    if not isinstance(text, str):
        logging.error(f"Invalid input: expected a string, got {type(text)}")
        return None
    if not text:
        logging.warning("Input text is empty.")
        return {"tokens": [], "token_spans": []} # Return empty lists for empty input

    try:
        doc = nlp(text)

        tokens: List[str] = []
        token_spans: List[Tuple[int, int]] = []

        for token in doc:
            tokens.append(token.text)
            start_char = token.idx
            end_char = start_char + len(token.text)
            token_spans.append((start_char, end_char))

        # Optional: Sanity check - ensure number of tokens matches number of spans
        assert len(tokens) == len(token_spans), "Mismatch between token count and span count!"

        return {
            "tokens": tokens,
            "token_spans": token_spans
        }

    except Exception as e:
        logging.error(f"Error processing text with spaCy: {e}", exc_info=True)
        return None

# --- Example Usage ---
if __name__ == "__main__":
    if nlp: # Only run examples if the model loaded
        test_text_1 = "Pt presents with stage II NSCLC."
        test_text_2 = "Underwent chemo for lung cancer (non-small cell)." # Test parentheses, hyphen

        print(f"\n--- Processing Text 1 ---")
        print(f"Input: '{test_text_1}'")
        processed_1 = process_text_spacy(test_text_1)
        if processed_1:
            print("Tokens:", processed_1["tokens"])
            print("Spans: ", processed_1["token_spans"])
            # Verify spans:
            for i, span in enumerate(processed_1["token_spans"]):
                start, end = span
                print(f"  Token {i}: '{processed_1['tokens'][i]}' -> Text Slice: '{test_text_1[start:end]}'")


        print(f"\n--- Processing Text 2 ---")
        print(f"Input: '{test_text_2}'")
        processed_2 = process_text_spacy(test_text_2)
        if processed_2:
            print("Tokens:", processed_2["tokens"])
            print("Spans: ", processed_2["token_spans"])
            # Verify spans:
            for i, span in enumerate(processed_2["token_spans"]):
                start, end = span
                print(f"  Token {i}: '{processed_2['tokens'][i]}' -> Text Slice: '{test_text_2[start:end]}'")

        print(f"\n--- Processing Empty Text ---")
        processed_empty = process_text_spacy("")
        if processed_empty:
            print("Tokens:", processed_empty["tokens"])
            print("Spans: ", processed_empty["token_spans"])

        print(f"\n--- Processing None ---")
        processed_none = process_text_spacy(None) # type: ignore Intentionally incorrect type for test
        if processed_none is None:
            print("Correctly handled None input.")

    else:
        print("\nSkipping examples because spaCy model failed to load.")
        print(f"Please ensure '{NLP_MODEL_NAME}' is installed:")
        print(f"  python -m spacy download {NLP_MODEL_NAME}")