# FILE: pipelines/base_pipeline.py
# (Replace the content of your existing file with this)

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Callable, Tuple # Added Tuple

class BaseProcessingPipeline(ABC):
    """
    Abstract Base Class for all oncology phrase processing pipelines.
    Defines the interface for processing a phrase and returning concepts/relations.
    """

    @abstractmethod
    # --- MODIFIED RETURN TYPE HINT ---
    def process(self, phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
    # --- END MODIFICATION ---
        """
        Processes a single input phrase to extract concepts and relations.

        Args:
            phrase: The input text phrase from the user.
            log_func: An optional callback function to send real-time log messages
                      back to the caller (e.g., for WebSocket streaming).
                      The callback should accept a single string argument.

        Returns:
            A tuple containing:
            1. A dictionary with 'concepts' and 'relations' lists.
               Expected structure:
               {
                   'concepts': [
                       {
                           'mention_id': str,     # Unique ID for this specific mention (e.g., from XMI)
                           'cui': str,            # Primary UMLS CUI identified
                           'all_cuis': List[str], # Optional: All CUIs linked to the mention
                           'matched_term': str,   # The actual text span matched
                           'text_span': str,      # String representation of span (e.g., "10-25")
                           'start_char': int,     # Start character offset
                           'end_char': int,       # End character offset
                           'sem_types': List[str],# List of semantic types (TUI or Name)
                           'score': Optional[float], # Confidence score if available
                           'source': str,         # Origin of the concept (e.g., 'ctakes:DiseaseDisorderMention')
                           'polarity': int,       # Contextual polarity (e.g., 1=positive, -1=negated, 0=unknown)
                           'uncertainty': int,    # Contextual uncertainty (e.g., 1=uncertain, 0=certain)
                           'conditional': bool,   # Contextual conditional flag
                           'generic': bool,       # Contextual generic flag
                           'historyOf': int       # Contextual historyOf flag (e.g., 1=history, 0=current)
                       },
                       # ... more concepts sorted by start_char
                   ],
                   'relations': [
                       {
                           'subject_mention_id': str, # mention_id of the subject concept
                           'object_mention_id': str,  # mention_id of the object concept
                           'relation': str,           # Type/Category of the relation (e.g., 'locationOf')
                           'source': str              # Origin of the relation (e.g., 'ctakes:relation')
                           # Optional: 'subj_text': str, 'obj_text': str
                       },
                       # ... more relations
                   ]
               }
               Implementations MUST return a dict containing at least 'concepts' and 'relations' keys,
               even if the lists are empty. Concepts should ideally be sorted by 'start_char'.
            2. A string containing the raw output from the underlying tool (e.g., XMI from cTAKES),
               or an empty string if not applicable or if an error occurred before generating it.
        """
        pass

    # Optional: Add other common methods or initialization if needed by all pipelines
    # def __init__(self, config=None):
    #     pass

# --- END OF pipelines/base_pipeline.py ---