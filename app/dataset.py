"""
app/dataset.py - CSV dataset column extractor module.

This module reads all CSV files from the specified dataset folder and extracts
their column headers to enable ontology mapping.
"""

import os
import csv
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

def get_all_csv_columns(dataset_folder: str) -> Dict[str, List[str]]:
    """
    Scans the dataset folder for CSV files and extracts the headers for all files.

    Args:
        dataset_folder (str): The folder containing CSV datasets.

    Returns:
        Dict[str, List[str]]: A dictionary mapping filename to lists of columns.
    """
    logger.info(f"Scanning dataset folder: {dataset_folder}")
    csv_columns = {}
    
    if not os.path.exists(dataset_folder):
        logger.warning(f"Dataset folder does not exist: {dataset_folder}")
        return csv_columns

    for file in os.listdir(dataset_folder):
        if file.endswith(".csv"):
            file_path = os.path.join(dataset_folder, file)
            logger.info(f"Reading columns from CSV file: {file_path}")
            try:
                with open(file_path, mode="r", newline="", encoding="utf-8-sig") as f:
                    reader = csv.reader(f)
                    header = next(reader)
                    # Clean up header whitespace/nulls
                    cleaned_header = [col.strip() for col in header if col.strip()]
                    csv_columns[file] = cleaned_header
                    logger.info(f"Extracted {len(cleaned_header)} columns from {file}")
            except Exception as e:
                logger.error(f"Error reading CSV header from {file_path}: {e}")
                
    return csv_columns

def format_columns_for_prompt(csv_columns: Dict[str, List[str]]) -> str:
    """
    Formats the CSV columns dictionary into a user-friendly string for LLM prompts.

    Args:
        csv_columns (Dict[str, List[str]]): Dictionary mapping CSV files to columns.

    Returns:
        str: Formatted string of columns grouped by CSV file.
    """
    formatted = []
    for file, columns in csv_columns.items():
        formatted.append(f"Dataset: {file}")
        for col in columns:
            formatted.append(f"  - {col}")
    return "\n".join(formatted)
