"""
app/ontology.py - RDFLib ontology parsing module.

This module recursively finds the LOAN folder in the ontology directory,
parses all Turtle (.ttl) files recursively, and extracts OWL classes, their
properties (domains, restrictions), labels, and definitions.
"""

import os
import logging
from typing import Dict, List, Any
from rdflib import Graph, RDF, OWL, RDFS, Namespace, URIRef

logger = logging.getLogger(__name__)

SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")
CMNS_AV = Namespace("https://www.omg.org/spec/Commons/AnnotationVocabulary/")

def find_loan_folder(base_folder: str) -> str:
    """
    Recursively search for a folder named 'LOAN' (case-insensitive) under base_folder.

    Args:
        base_folder (str): The parent directory to search in.

    Returns:
        str: The path to the LOAN directory.

    Raises:
        FileNotFoundError: If no LOAN folder is found.
    """
    # Normalize path
    base_folder_abs = os.path.abspath(base_folder)
    logger.info(f"Searching for LOAN folder recursively inside: {base_folder}")
    
    # If the provided folder itself is the LOAN folder, return it directly
    if os.path.basename(base_folder_abs).upper() == "LOAN":
        logger.info(f"Provided folder is the LOAN folder: {base_folder_abs}")
        return base_folder_abs

    for root, dirs, _ in os.walk(base_folder_abs):
        for d in dirs:
            if d.upper() == "LOAN":
                loan_path = os.path.join(root, d)
                logger.info(f"Found LOAN folder at: {loan_path}")
                return loan_path
    raise FileNotFoundError(f"LOAN folder not found under {base_folder}")


def get_ttl_files(loan_folder: str) -> List[str]:
    """
    Finds all .ttl files recursively under the given LOAN folder.

    Args:
        loan_folder (str): Path to the LOAN folder.

    Returns:
        List[str]: List of absolute paths to TTL files.
    """
    ttl_files = []
    for root, _, files in os.walk(loan_folder):
        for file in files:
            if file.endswith(".ttl"):
                ttl_files.append(os.path.abspath(os.path.join(root, file)))
    logger.info(f"Found {len(ttl_files)} TTL files in LOAN folder.")
    return ttl_files

def parse_ontology_classes(ttl_files: List[str]) -> List[Dict[str, Any]]:
    """
    Parses all TTL files and extracts OWL classes and their associated properties.

    Args:
        ttl_files (List[str]): List of paths to TTL files.

    Returns:
        List[Dict[str, Any]]: A list of dictionaries representing each parsed class.
    """
    classes_info = []

    for file_path in ttl_files:
        logger.info(f"Parsing TTL file: {file_path}")
        g = Graph()
        try:
            g.parse(file_path, format="turtle")
        except Exception as e:
            logger.error(f"Failed to parse {file_path}: {e}")
            continue

        # Extract all classes in the graph
        for class_uri in g.subjects(RDF.type, OWL.Class):
            if isinstance(class_uri, URIRef):
                if str(class_uri).startswith("http"):
                    # Extract local name and metadata
                    local_name = str(class_uri).split("/")[-1].split("#")[-1]
                    
                    # Labels and definitions
                    label = g.value(class_uri, RDFS.label)
                    definition = g.value(class_uri, SKOS.definition) or g.value(class_uri, CMNS_AV.definition)
                    
                    # Get associated properties
                    properties = set()
                    
                    # 1. Properties having domain = class_uri
                    for prop in g.subjects(RDFS.domain, class_uri):
                        prop_name = str(prop).split("/")[-1].split("#")[-1]
                        properties.add(prop_name)
                        
                    # 2. Properties referenced in restrictions of the class
                    for sub in g.objects(class_uri, RDFS.subClassOf):
                        on_prop = g.value(sub, OWL.onProperty)
                        if on_prop:
                            prop_name = str(on_prop).split("/")[-1].split("#")[-1]
                            properties.add(prop_name)

                    classes_info.append({
                        "class_name": local_name,
                        "class_uri": str(class_uri),
                        "label": str(label) if label else local_name,
                        "definition": str(definition) if definition else "",
                        "source_file": file_path,
                        "properties": sorted(list(properties))
                    })
                    
    logger.info(f"Successfully extracted {len(classes_info)} classes from ontology.")
    return classes_info
