# --- START OF FILE pipelines/base_pipeline.py ---

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, Callable

class BaseProcessingPipeline(ABC):
    """
    Abstract Base Class for all oncology phrase processing pipelines.
    Defines the interface for processing a phrase and returning concepts/relations.
    """

    @abstractmethod
    def process(self, phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Processes a single input phrase to extract concepts and relations.

        Args:
            phrase: The input text phrase from the user.
            log_func: An optional callback function to send real-time log messages
                      back to the caller (e.g., for WebSocket streaming).
                      The callback should accept a single string argument.

        Returns:
            A dictionary containing two keys:
            - 'concepts': A list of dictionaries, where each dictionary represents
                          an extracted concept (e.g., {'cui': 'C123', 'matched_term': '...', ...}).
            - 'relations': A list of dictionaries, where each dictionary represents
                           an extracted relationship between concepts
                           (e.g., {'subject_cui': 'C123', 'object_cui': 'C456', 'relation': '...', ...}).
        """
        pass

    # Optional: Add other common methods or initialization if needed by all pipelines
    # def __init__(self, config=None):
    #     pass

# --- END OF FILE pipelines/base_pipeline.py ---