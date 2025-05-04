# FILE: pipelines/ctakes_based/processor.py
# (Corrected Version - Fixed get_semantic_types_for_cui call and return type)

import os
import logging
from typing import Any, Dict, List, Optional, Callable, Tuple # Added Tuple
import requests
from lxml import etree # Using lxml for robust XML parsing

from pipelines.base_pipeline import BaseProcessingPipeline
from scripts.db_utils import get_semantic_types_for_cui # Ensure this function exists and works

# Configure logging for this module
logger = logging.getLogger(__name__)

# Define XML namespaces used by cTAKES XMI
# Adjust these based on inspecting YOUR actual XMI output from cTAKES 6.0.0
NS_MAP = {
    'xmi': 'http://www.omg.org/XMI',
    'cas': 'http:///uima/cas.ecore',
    'tcas': 'http:///uima/tcas.ecore',
    'textsem': 'http:///org/apache/ctakes/typesystem/type/textsem.ecore',
    'refsem': 'http:///org/apache/ctakes/typesystem/type/refsem.ecore',
    'relation': 'http:///org/apache/ctakes/typesystem/type/relation.ecore',
    'syntax': 'http:///org/apache/ctakes/typesystem/type/syntax.ecore',
    'fs': 'http:///uima/examples/SourceDocumentInformation.ecore',
    # Add others if present in your output
}

class CTakesPipeline(BaseProcessingPipeline):
    """
    Processing pipeline that interacts with the internal cTAKES Wrapper API service.
    Sends text to the wrapper, receives XMI, parses it, and returns JSON + raw XMI.
    """
    def __init__(self):
        # Get the URL of the internal wrapper API service from environment variables
        self.ctakes_url = os.getenv("CTAKES_URL") # e.g., http://ctakes_wrapper:8081/process
        # Removed target_ontology_sab as it's not used in get_semantic_types_for_cui

        if not self.ctakes_url:
            logger.critical("CTAKES_URL environment variable not set. CTakesPipeline cannot function.")
            raise ValueError("CTAKES_URL environment variable is not set.")

        logger.info(f"CTakesPipeline initialized. Target Wrapper API URL: {self.ctakes_url}")
        # Use a requests session for potential connection pooling
        self.session = requests.Session()
        # No authentication needed for the call TO the wrapper API itself
        # Authentication (API Key) is handled INSIDE the wrapper service when it calls cTAKES

        # Set default headers for interacting with the wrapper API
        self.session.headers.update({
            'Content-Type': 'text/plain; charset=utf-8', # Sending raw text
            'Accept': 'application/xml' # Expecting raw XMI back
        })

    def _log(self, message: str, log_func: Optional[Callable[[str], None]] = None):
        """Helper to log both locally and potentially via callback."""
        logger.info(message)
        if log_func:
            try:
                log_func(f"[CTakesPipeline] {str(message)}")
            except Exception as e:
                logger.error(f"Error calling log_func: {e}", exc_info=False)

    # --- MODIFIED RETURN TYPE ---
    def process(self, phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Tuple[Dict[str, List[Dict[str, Any]]], str]:
        """
        Processes the input phrase by calling the internal cTAKES Wrapper API service.

        Args:
            phrase: The input text phrase.
            log_func: Optional callback function for streaming logs.

        Returns:
            A tuple containing:
            1. A dictionary with 'concepts' and 'relations' lists.
            2. The raw XMI content as a string.
            Returns ({'concepts': [], 'relations': []}, "") on error.
        """
        self._log(f"Processing phrase via Wrapper API (first 50 chars): '{phrase[:50]}...'", log_func)
        final_concepts: List[Dict[str, Any]] = []
        final_relations: List[Dict[str, Any]] = []
        xmi_content_str: str = "" # Initialize raw XMI string

        if not phrase or not phrase.strip():
            self._log("Input phrase is empty or whitespace only. Skipping cTAKES wrapper call.", log_func)
            return {'concepts': [], 'relations': []}, ""

        try:
            self._log(f"Sending phrase to cTAKES Wrapper API at {self.ctakes_url}", log_func)
            response = self.session.post(
                self.ctakes_url,
                data=phrase.encode('utf-8'), # Send raw text encoded as bytes
                timeout=120 # Increase timeout significantly, as wrapper waits for cTAKES file processing
            )
            self._log(f"Received response from Wrapper API (Status: {response.status_code}, Encoding: {response.encoding})", log_func)

            # Check for successful response from the WRAPPER API
            response.raise_for_status() # Raise HTTPError for 4xx/5xx from the wrapper

            # --- XMI Parsing ---
            xmi_content_bytes = response.content # Wrapper returns raw XMI bytes
            if not xmi_content_bytes:
                 self._log("Received empty XMI content from Wrapper API.", log_func)
                 return {'concepts': [], 'relations': []}, ""

            # --- Store raw XMI as string ---
            try:
                # Attempt to decode using response encoding, fallback to utf-8
                encoding = response.encoding if response.encoding else 'utf-8'
                xmi_content_str = xmi_content_bytes.decode(encoding)
            except UnicodeDecodeError:
                self._log(f"Warning: Could not decode XMI using {encoding}, trying utf-8 with errors ignored.", log_func)
                xmi_content_str = xmi_content_bytes.decode('utf-8', errors='ignore')
            except Exception as decode_err:
                 self._log(f"Error decoding XMI content: {decode_err}. Proceeding with empty XMI string.", log_func)
                 xmi_content_str = "" # Ensure it's an empty string on error

            # --- Parse XMI for concepts/relations ---
            try:
                # Use the bytes directly for lxml parsing
                root = etree.fromstring(xmi_content_bytes)
                self._log("Successfully parsed XMI response from Wrapper API.", log_func)
            except etree.XMLSyntaxError as xml_err:
                 self._log(f"Failed to parse XMI response received from wrapper: {xml_err}", log_func)
                 # Return empty results but include the potentially malformed XMI string for debugging
                 return {'concepts': [], 'relations': []}, xmi_content_str

            # Get Sofa string
            sofa_element = root.find('.//cas:Sofa', namespaces=NS_MAP)
            if sofa_element is None or 'sofaString' not in sofa_element.attrib:
                self._log("Critical error: cas:Sofa element/sofaString attribute not found in received XMI.", log_func)
                return {'concepts': [], 'relations': []}, xmi_content_str
            sofa_string = sofa_element.attrib['sofaString']

            # --- Efficiently store UmlsConcepts by their xmi:id ---
            xmi_id_attr = f'{{{NS_MAP["xmi"]}}}id'
            umls_concepts_by_id = {
                uc.attrib.get(xmi_id_attr): uc
                for uc in root.xpath('.//refsem:UmlsConcept', namespaces=NS_MAP)
                if uc.attrib.get(xmi_id_attr) is not None
            }
            if not umls_concepts_by_id:
                 self._log("Warning: No refsem:UmlsConcept elements found in received XMI.", log_func)

            # --- Concept Extraction ---
            mention_tags_to_process = [
                'textsem:DiseaseDisorderMention', 'textsem:SignSymptomMention',
                'textsem:ProcedureMention', 'textsem:AnatomicalSiteMention',
                'textsem:MedicationMention', 'textsem:LabMention',
            ]
            processed_mention_ids = set()
            for tag in mention_tags_to_process:
                elements = root.xpath(f'.//{tag}', namespaces=NS_MAP)
                for element in elements:
                    try:
                        mention_id = element.attrib.get(xmi_id_attr)
                        if not mention_id or mention_id in processed_mention_ids: continue
                        begin = int(element.attrib.get('begin', -1))
                        end = int(element.attrib.get('end', -1))
                        if begin < 0 or end <= begin or end > len(sofa_string): continue
                        matched_term = sofa_string[begin:end]
                        polarity = int(element.attrib.get('polarity', 0))
                        uncertainty = int(element.attrib.get('uncertainty', 0))
                        conditional = element.attrib.get('conditional', 'false').lower() == 'true'
                        generic = element.attrib.get('generic', 'false').lower() == 'true'
                        historyOf = int(element.attrib.get('historyOf', 0))
                        cuis = []
                        primary_cui = None
                        concept_ids_str = element.attrib.get('ontologyConceptArr', '')
                        if concept_ids_str:
                            concept_ids = concept_ids_str.split()
                            for concept_id in concept_ids:
                                umls_concept_element = umls_concepts_by_id.get(concept_id)
                                if umls_concept_element is not None:
                                    cui = umls_concept_element.attrib.get('cui')
                                    if cui and cui.startswith('C') and cui[1:].isdigit():
                                        cuis.append(cui)
                                        if primary_cui is None: primary_cui = cui
                        if not primary_cui: continue

                        sem_types = []
                        try:
                            # --- CORRECTED CALL: Removed second argument ---
                            sem_types = get_semantic_types_for_cui(primary_cui)
                        except TypeError as type_err: # Catch the specific error we saw
                            self._log(f"DB Call TypeError for CUI {primary_cui}: {type_err}. Check db_utils.get_semantic_types_for_cui signature.", log_func)
                        except Exception as db_err:
                            self._log(f"DB Error fetching types for CUI {primary_cui}: {db_err}", log_func)

                        concept_dict = {
                            'mention_id': mention_id, 'cui': primary_cui, 'all_cuis': cuis,
                            'matched_term': matched_term, 'text_span': f"{begin}-{end}",
                            'start_char': begin, 'end_char': end, 'sem_types': sem_types,
                            'score': None, 'source': f"ctakes:{tag.split(':')[1]}",
                            'polarity': polarity, 'uncertainty': uncertainty, 'conditional': conditional,
                            'generic': generic, 'historyOf': historyOf,
                        }
                        final_concepts.append(concept_dict)
                        processed_mention_ids.add(mention_id)
                    except (ValueError, TypeError, KeyError, IndexError) as el_err:
                        current_id = element.attrib.get(xmi_id_attr, 'N/A')
                        self._log(f"Error parsing attributes for mention element (ID: {current_id}): {el_err}", log_func)
                        continue
                    except Exception as gen_el_err:
                        current_id = element.attrib.get(xmi_id_attr, 'N/A')
                        self._log(f"Unexpected error processing mention element (ID: {current_id}): {gen_el_err}", log_func)
                        continue

            final_concepts.sort(key=lambda x: x['start_char'])
            self._log(f"Extracted {len(final_concepts)} concepts from received XMI.", log_func)

            # --- Relation Extraction ---
            relation_elements = root.xpath('.//relation:LocationOfTextRelation', namespaces=NS_MAP)
            if relation_elements:
                self._log(f"Found {len(relation_elements)} relation elements in received XMI.", log_func)
                concepts_by_mention_id = {c['mention_id']: c for c in final_concepts}
                for rel_element in relation_elements:
                    try:
                        rel_category = rel_element.attrib.get('category', 'UNKNOWN_RELATION')
                        rel_id = rel_element.attrib.get(xmi_id_attr, 'N/A')

                                                # Get arg1 and arg2 attributes directly from the relation element
                        arg1_ref = rel_element.attrib.get('arg1')
                        arg2_ref = rel_element.attrib.get('arg2')

                        subject_relation_arg = None
                        object_relation_arg = None

                        if arg1_ref and arg2_ref:
                            # Find the corresponding RelationArgument elements using their xmi:id
                            arg1_element = root.xpath(f'.//relation:RelationArgument[@xmi:id="{arg1_ref}"]', namespaces=NS_MAP)
                            arg2_element = root.xpath(f'.//relation:RelationArgument[@xmi:id="{arg2_ref}"]', namespaces=NS_MAP)

                            if arg1_element and arg2_element:
                                # Get the 'argument' attribute which points to the actual concept mention's xmi:id
                                subject_mention_id = arg1_element[0].attrib.get('argument')
                                object_mention_id = arg2_element[0].attrib.get('argument')
                            else:
                                self._log(f"Could not find RelationArgument elements for relation (ID: {rel_id}) args: {arg1_ref}, {arg2_ref}. Skipping.", log_func)
                                continue
                        else:
                             self._log(f"Relation (ID: {rel_id}) in XMI missing arg1 or arg2 attribute. Skipping.", log_func)
                             continue

                        if subject_mention_id and object_mention_id:
                             if subject_mention_id in concepts_by_mention_id and object_mention_id in concepts_by_mention_id:
                                 relation_dict = {
                                     'subject_mention_id': subject_mention_id, 'object_mention_id': object_mention_id,
                                     'relation': rel_category, 'source': f'ctakes:relation:{rel_category}',
                                 }
                                 final_relations.append(relation_dict)
                             else:
                                 self._log(f"Relation (ID: {rel_id}) links to non-extracted mentions. Skipping.", log_func)
                        else:
                             self._log(f"Could not extract arguments for relation (ID: {rel_id}).", log_func)
                    except Exception as rel_err:
                        rel_id_err = rel_element.attrib.get(xmi_id_attr, 'N/A')
                        self._log(f"Error processing relation element (ID: {rel_id_err}): {rel_err}", log_func)
                        continue
            else:
                 self._log("No relation elements found in received XMI.", log_func)

            self._log(f"Extracted {len(final_relations)} relations from received XMI.", log_func)

        # --- Error Handling ---
        except requests.exceptions.Timeout:
            self._log("Request to cTAKES Wrapper API timed out.", log_func)
            return {'concepts': [], 'relations': []}, "" # Return empty tuple
        except requests.exceptions.HTTPError as http_err:
            self._log(f"HTTP error calling Wrapper API: {http_err.response.status_code} - {http_err.response.reason}", log_func)
            try:
                 error_body = http_err.response.text
                 self._log(f"Wrapper API Error Response Body (first 500 chars): {error_body[:500]}", log_func)
                 xmi_content_str = f"Error from Wrapper: {error_body}" # Include error in XMI string if possible
            except Exception: pass
            return {'concepts': [], 'relations': []}, xmi_content_str # Return empty tuple + error string
        except requests.exceptions.RequestException as req_err:
            self._log(f"Network error calling Wrapper API: {req_err}", log_func)
            return {'concepts': [], 'relations': []}, "" # Return empty tuple
        except ImportError as imp_err:
             logger.critical(f"Import error, likely missing db_utils or dependency: {imp_err}", exc_info=True)
             self._log(f"Server configuration error: {imp_err}", log_func)
             return {'concepts': [], 'relations': []}, "" # Return empty tuple
        except Exception as e:
            logger.exception("An unexpected error occurred in CTakesPipeline.process:")
            self._log(f"An unexpected processing error occurred: {e}", log_func)
            return {'concepts': [], 'relations': []}, xmi_content_str # Return empty tuple + potentially partial XMI

        # --- CORRECTED RETURN: Return tuple (dict, str) ---
        json_results = {'concepts': final_concepts, 'relations': final_relations}
        return json_results, xmi_content_str