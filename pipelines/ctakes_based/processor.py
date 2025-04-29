# FILE: pipelines/ctakes_based/processor.py
# (Updated Version - Removed Basic Auth Logic)

import os
import logging
from typing import Any, Dict, List, Optional, Callable
import requests
# Removed: from requests.auth import HTTPBasicAuth
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
    Sends text to the wrapper, receives XMI, and parses it.
    """
    def __init__(self):
        # Get the URL of the internal wrapper API service from environment variables
        self.ctakes_url = os.getenv("CTAKES_URL") # e.g., http://ctakes_wrapper:8081/process
        self.target_ontology_sab = os.getenv("TARGET_ONTOLOGY_SAB", "NCI")

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

    def process(self, phrase: str, log_func: Optional[Callable[[str], None]] = None) -> Dict[str, List[Dict[str, Any]]]:
        """
        Processes the input phrase by calling the internal cTAKES Wrapper API service.

        Args:
            phrase: The input text phrase.
            log_func: Optional callback function for streaming logs.

        Returns:
            A dictionary containing 'concepts' and 'relations' lists per BaseProcessingPipeline interface.
        """
        self._log(f"Processing phrase via Wrapper API (first 50 chars): '{phrase[:50]}...'", log_func)
        final_concepts: List[Dict[str, Any]] = []
        final_relations: List[Dict[str, Any]] = []

        if not phrase or not phrase.strip():
            self._log("Input phrase is empty or whitespace only. Skipping cTAKES wrapper call.", log_func)
            return {'concepts': [], 'relations': []}

        try:
            self._log(f"Sending phrase to cTAKES Wrapper API at {self.ctakes_url}", log_func)
            response = self.session.post(
                self.ctakes_url,
                data=phrase.encode('utf-8'), # Send raw text encoded as bytes
                # Removed: auth=self.auth, # No auth needed for wrapper API call
                timeout=120 # Increase timeout significantly, as wrapper waits for cTAKES file processing
            )
            self._log(f"Received response from Wrapper API (Status: {response.status_code}, Encoding: {response.encoding})", log_func)

            # Check for successful response from the WRAPPER API
            response.raise_for_status() # Raise HTTPError for 4xx/5xx from the wrapper

            # --- XMI Parsing (remains the same) ---
            xmi_content = response.content # Wrapper returns raw XMI bytes
            if not xmi_content:
                 self._log("Received empty XMI content from Wrapper API.", log_func)
                 return {'concepts': [], 'relations': []}

            try:
                root = etree.fromstring(xmi_content)
                self._log("Successfully parsed XMI response from Wrapper API.", log_func)
            except etree.XMLSyntaxError as xml_err:
                 self._log(f"Failed to parse XMI response received from wrapper: {xml_err}", log_func)
                 return {'concepts': [], 'relations': []}

            # Get Sofa string (remains the same)
            sofa_element = root.find('.//cas:Sofa', namespaces=NS_MAP)
            if sofa_element is None or 'sofaString' not in sofa_element.attrib:
                self._log("Critical error: cas:Sofa element/sofaString attribute not found in received XMI.", log_func)
                return {'concepts': [], 'relations': []}
            sofa_string = sofa_element.attrib['sofaString']

            # --- Efficiently store UmlsConcepts by their xmi:id (remains the same) ---
            xmi_id_attr = f'{{{NS_MAP["xmi"]}}}id'
            umls_concepts_by_id = {
                uc.attrib.get(xmi_id_attr): uc
                for uc in root.xpath('.//refsem:UmlsConcept', namespaces=NS_MAP)
                if uc.attrib.get(xmi_id_attr) is not None
            }
            if not umls_concepts_by_id:
                 self._log("Warning: No refsem:UmlsConcept elements found in received XMI.", log_func)

            # --- Concept Extraction (remains the same logic) ---
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
                            sem_types = get_semantic_types_for_cui(primary_cui, self.target_ontology_sab)
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

            # --- Relation Extraction (remains the same logic) ---
            # This logic still depends on whether the cTAKES pipeline run by the wrapper
            # was configured to produce relation annotations in the XMI.
            relation_elements = root.xpath('.//relation:BinaryTextRelation', namespaces=NS_MAP)
            if relation_elements:
                self._log(f"Found {len(relation_elements)} relation elements in received XMI.", log_func)
                concepts_by_mention_id = {c['mention_id']: c for c in final_concepts}
                for rel_element in relation_elements:
                    try:
                        rel_category = rel_element.attrib.get('category', 'UNKNOWN_RELATION')
                        rel_id = rel_element.attrib.get(xmi_id_attr, 'N/A')
                        args = rel_element.xpath('./relation:arguments/relation:RelationArgument', namespaces=NS_MAP)
                        subject_mention_id = None
                        object_mention_id = None
                        if len(args) == 2:
                            target1_id = args[0].attrib.get('target')
                            target2_id = args[1].attrib.get('target')
                            if target1_id and target2_id:
                                subject_mention_id = target1_id
                                object_mention_id = target2_id
                        else:
                             self._log(f"Relation (ID: {rel_id}) in XMI doesn't have 2 arguments. Skipping.", log_func)
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

        # --- Error Handling (remains largely the same, targets Wrapper API interaction) ---
        except requests.exceptions.Timeout:
            self._log("Request to cTAKES Wrapper API timed out.", log_func)
            return {'concepts': [], 'relations': []}
        except requests.exceptions.HTTPError as http_err:
            # Handle HTTP errors returned BY THE WRAPPER API (e.g., 500 if cTAKES failed internally)
            self._log(f"HTTP error calling Wrapper API: {http_err.response.status_code} - {http_err.response.reason}", log_func)
            try:
                 error_body = http_err.response.text
                 self._log(f"Wrapper API Error Response Body (first 500 chars): {error_body[:500]}", log_func)
            except Exception: pass
            return {'concepts': [], 'relations': []}
        except requests.exceptions.RequestException as req_err:
            # Handle network errors connecting TO the wrapper API
            self._log(f"Network error calling Wrapper API: {req_err}", log_func)
            return {'concepts': [], 'relations': []}
        except ImportError as imp_err:
             logger.critical(f"Import error, likely missing db_utils or dependency: {imp_err}", exc_info=True)
             self._log(f"Server configuration error: {imp_err}", log_func)
             return {'concepts': [], 'relations': []}
        except Exception as e:
            # Catch-all for unexpected errors in THIS processor (e.g., XMI parsing, DB call)
            logger.exception("An unexpected error occurred in CTakesPipeline.process:")
            self._log(f"An unexpected processing error occurred: {e}", log_func)
            return {'concepts': [], 'relations': []}

        # Return the final structured results
        return {'concepts': final_concepts, 'relations': final_relations}