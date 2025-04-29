# FILE: scripts/owl_generator.py
# (Create this new file with the following content)

import logging
from typing import List, Dict, Any
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, OWL, XSD # Import XSD for XML Schema Datatypes

logger = logging.getLogger(__name__) # Get logger instance (configured in main.py)

# --- Define Namespaces ---
# Best Practice: Use persistent URIs for your ontology terms if possible.
# For a thesis project, example.org is acceptable, but for real applications, use a domain you control.
MYNS = Namespace("http://example.org/oncology_processed_output#")

# Official NCIt namespace (useful if mapping CUIs directly)
# Check the current valid URI if using, it might change.
NCIT = Namespace("http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl#")

# OBO Relation Ontology (provides standard relation properties like location_of)
# Using standard relations improves interoperability.
RO = Namespace("http://purl.obolibrary.org/obo/")

# --- Default Relation Mapping ---
# Map relation categories from cTAKES (or your rules) to standard/custom OWL Object Properties.
# Keys should match the 'relation' field from your pipeline's output relations list.
# Values are the URIRefs for the corresponding OWL Object Property.
# PRIORITIZE standard relations (e.g., from RO) where possible.
DEFAULT_RELATION_MAP = {
    # --- Common cTAKES Relation Categories (Examples - Verify based on your cTAKES config) ---
    "locationOf": RO.RO_0001025,        # Standard 'location of' from OBO RO
    "findingSite": RO.RO_0002573,       # Standard 'has finding site' (inverse of locationOf in some contexts)
    "degreeOf": MYNS.hasDegree,         # Example custom property if no standard exists
    "severityOf": MYNS.hasSeverity,     # Example custom property
    "courseOf": MYNS.hasCourse,         # Example custom property
    "associatedWith": RO.RO_0002451,    # Standard 'associated with'
    "hasAssociatedMorphology": RO.RO_0002574, # Standard
    "indicatedBy": RO.RO_0002558,       # Standard 'indicated by'
    "treats": RO.RO_0002400,            # Standard 'treats'/'prevents' (check exact meaning needed)
    "contraindicates": RO.RO_0002406,   # Standard 'contraindicates'
    "causes": RO.RO_0002410,            # Standard 'causes'
    "locaionOf": RO.hasLocation,        # Standard 'location of' (case-sensitive check)

    # --- Add more mappings as needed based on your pipeline's output ---
    "UNKNOWN_RELATION": OWL.topObjectProperty # Map unknown to the most general property
}

def generate_owl(concepts: List[Dict[str, Any]], relations: List[Dict[str, Any]], relation_map: Dict[str, URIRef] = DEFAULT_RELATION_MAP) -> str:
    """
    Generates an OWL representation from extracted concepts and relations.

    Maps concepts/mentions to OWL Individuals, CUIs to OWL Classes, and
    relations to OWL Object Property assertions between Individuals.

    Args:
        concepts: List of concept dictionaries (output from pipeline, matching BaseProcessingPipeline format).
        relations: List of relation dictionaries (output from pipeline, matching BaseProcessingPipeline format).
        relation_map: Optional dictionary mapping relation category strings to rdflib URIRefs for Object Properties.

    Returns:
        A string containing the OWL ontology serialized in XML/RDF format ('pretty-xml').
        Returns an empty graph string or error message string on failure.
    """
    logger.info(f"Starting OWL generation for {len(concepts)} concepts and {len(relations)} relations.")
    g = Graph()

    # Bind namespaces for cleaner serialization
    g.bind("myns", MYNS)
    g.bind("ncit", NCIT) # Bind if using official NCIt URIs for classes
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("owl", OWL)
    g.bind("ro", RO)   # Bind if using Relation Ontology properties
    g.bind("xsd", XSD) # Bind for literal datatypes

    # --- Define Custom Data Properties (if not using existing ones) ---
    # These properties will annotate the mention Individuals.
    hasMentionID = MYNS.hasMentionID
    hasMatchedText = MYNS.hasMatchedText
    hasBeginChar = MYNS.hasBeginChar
    hasEndChar = MYNS.hasEndChar
    hasPolarity = MYNS.hasPolarity
    hasUncertainty = MYNS.hasUncertainty
    hasConditional = MYNS.hasConditional
    hasGeneric = MYNS.hasGeneric
    hasHistoryOf = MYNS.hasHistoryOf
    hasSource = MYNS.hasSource      # e.g., 'ctakes:DiseaseDisorderMention'
    hasCUI = MYNS.hasCUI            # Links mention individual to its primary CUI string
    hasAllCUIs = MYNS.hasAllCUIs    # Stores list of all CUIs found for mention
    # hasSemTypes = MYNS.hasSemTypes # Optionally add semantic types as property

    # Declare these as OWL Datatype Properties for formality
    data_properties = [
        hasMentionID, hasMatchedText, hasBeginChar, hasEndChar, hasPolarity,
        hasUncertainty, hasConditional, hasGeneric, hasHistoryOf, hasSource,
        hasCUI, hasAllCUIs # , hasSemTypes
    ]
    for prop in data_properties:
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        # Optionally add rdfs:label for clarity
        # g.add((prop, RDFS.label, Literal(prop.split('#')[-1])))


    # Declare Object Properties used in the relation_map for formality
    for rel_prop_uri in relation_map.values():
        g.add((rel_prop_uri, RDF.type, OWL.ObjectProperty))
        # Optionally add rdfs:label for clarity, especially for custom ones
        # if rel_prop_uri.startswith(str(MYNS)):
        #    g.add((rel_prop_uri, RDFS.label, Literal(rel_prop_uri.split('#')[-1])))


    # --- Process Concepts (Mentions -> Individuals, CUIs -> Classes) ---
    processed_cuis = set() # Keep track of CUIs already declared as classes
    mention_uri_map = {} # Map input mention_id to its generated OWL Individual URI

    for concept in concepts:
        try:
            # Extract key fields from the concept dictionary
            mention_id = concept.get('mention_id')
            cui = concept.get('cui')
            matched_term = concept.get('matched_term', '')
            source = concept.get('source', 'unknown')

            # Basic validation: Skip if essential IDs are missing
            if not mention_id or not cui:
                logger.warning(f"Skipping concept due to missing mention_id ('{mention_id}') or cui ('{cui}'): {concept}")
                continue

            # Create URI for the specific mention (OWL Individual)
            # Ensure mention_id is safe for use in URI (e.g., replace unsafe chars if necessary)
            safe_mention_id = str(mention_id).replace(":", "_").replace(" ", "_") # Basic sanitization
            mention_uri = MYNS[f"mention_{safe_mention_id}"]
            mention_uri_map[mention_id] = mention_uri # Store mapping for relation linking

            # Create URI for the concept class based on the CUI
            # Option 1: Use official NCIt namespace (preferred if CUIs are from NCIt)
            # cui_class_uri = NCIT[cui]
            # Option 2: Use your custom namespace (safer if CUI source varies or isn't purely NCIt)
            cui_class_uri = MYNS[cui]

            # --- Assert OWL Statements for the Mention Individual ---
            # 1. Declare the mention as an OWL Named Individual
            g.add((mention_uri, RDF.type, OWL.NamedIndividual))

            # 2. Assert the type of the individual (instance of the CUI class)
            g.add((mention_uri, RDF.type, cui_class_uri))

            # 3. Add descriptive label to the individual (using matched text)
            g.add((mention_uri, RDFS.label, Literal(f"{matched_term} (Mention: {safe_mention_id})")))

            # 4. Add data properties annotating the mention individual
            g.add((mention_uri, hasMentionID, Literal(mention_id))) # Store original ID
            g.add((mention_uri, hasMatchedText, Literal(matched_term)))
            g.add((mention_uri, hasBeginChar, Literal(concept.get('start_char'), datatype=XSD.integer)))
            g.add((mention_uri, hasEndChar, Literal(concept.get('end_char'), datatype=XSD.integer)))
            g.add((mention_uri, hasPolarity, Literal(concept.get('polarity', 0), datatype=XSD.integer)))
            g.add((mention_uri, hasUncertainty, Literal(concept.get('uncertainty', 0), datatype=XSD.integer)))
            g.add((mention_uri, hasConditional, Literal(concept.get('conditional', False), datatype=XSD.boolean)))
            g.add((mention_uri, hasGeneric, Literal(concept.get('generic', False), datatype=XSD.boolean)))
            g.add((mention_uri, hasHistoryOf, Literal(concept.get('historyOf', 0), datatype=XSD.integer)))
            g.add((mention_uri, hasSource, Literal(source)))
            g.add((mention_uri, hasCUI, Literal(cui))) # Link to primary CUI string

            # Add all CUIs as potentially multiple property assertions
            all_cuis = concept.get('all_cuis', [])
            if isinstance(all_cuis, list):
                for acui in all_cuis:
                    g.add((mention_uri, hasAllCUIs, Literal(acui)))

            # Optionally add semantic types
            # sem_types = concept.get('sem_types', [])
            # if isinstance(sem_types, list):
            #    for st in sem_types:
            #        g.add((mention_uri, hasSemTypes, Literal(st)))


            # --- Declare the CUI as an OWL Class (if not already done) ---
            if cui not in processed_cuis:
                g.add((cui_class_uri, RDF.type, OWL.Class))
                # Add a label to the class (using CUI for now, could fetch preferred term later)
                # A better label would be the preferred term for the CUI from UMLS/NCIt.
                g.add((cui_class_uri, RDFS.label, Literal(f"Concept {cui}")))
                # Optional: Add comment indicating it's a concept class
                g.add((cui_class_uri, RDFS.comment, Literal(f"Represents the clinical concept with CUI {cui}.")))
                processed_cuis.add(cui)

        except Exception as e:
             # Catch errors processing a single concept to avoid stopping entire generation
             logger.error(f"Error processing concept for OWL: {concept} - Error: {e}", exc_info=True)
             continue # Skip problematic concept


    # --- Process Relations (Object Properties between Mention Individuals) ---
    for relation in relations:
        try:
            # Extract key fields from the relation dictionary
            subj_mention_id = relation.get('subject_mention_id')
            obj_mention_id = relation.get('object_mention_id')
            rel_category = relation.get('relation') # e.g., 'locationOf'

            # Basic validation
            if not subj_mention_id or not obj_mention_id or not rel_category:
                logger.warning(f"Skipping relation due to missing fields: {relation}")
                continue

            # Get the corresponding OWL Object Property URI from the map
            rel_prop_uri = relation_map.get(rel_category)
            if not rel_prop_uri:
                # If category not in map, maybe use a default or log warning
                logger.warning(f"No OWL object property defined in relation_map for category: '{rel_category}'. Using owl:topObjectProperty. Relation: {relation}")
                rel_prop_uri = OWL.topObjectProperty # Fallback to most general property

            # Get the URIs for the subject and object mention individuals (created during concept processing)
            subj_uri = mention_uri_map.get(subj_mention_id)
            obj_uri = mention_uri_map.get(obj_mention_id)

            # Check if both subject and object mentions were successfully processed earlier
            if not subj_uri or not obj_uri:
                logger.warning(f"Could not find URIs for one or both mention IDs ('{subj_mention_id}', '{obj_mention_id}') involved in relation: {relation}. Skipping relation.")
                continue

            # --- Assert the OWL Object Property statement ---
            # Add the triple: (SubjectIndividual, PropertyURI, ObjectIndividual)
            g.add((subj_uri, rel_prop_uri, obj_uri))

        except Exception as e:
             # Catch errors processing a single relation
             logger.error(f"Error processing relation for OWL: {relation} - Error: {e}", exc_info=True)
             continue # Skip problematic relation


    # --- Serialize the final graph to an OWL/XML string ---
    try:
        # Use 'pretty-xml' format for best compatibility with Protege and readability
        # Other formats like 'turtle', 'n3', 'xml' are also available.
        owl_string = g.serialize(format='pretty-xml', encoding='utf-8').decode('utf-8')
        logger.info(f"OWL generation complete. Graph has {len(g)} triples.")
        return owl_string
    except Exception as e:
        logger.error(f"Error serializing OWL graph: {e}", exc_info=True)
        # Return an error message instead of crashing
        return f"<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"><error>Could not serialize OWL graph: {e}</error></rdf:RDF>"