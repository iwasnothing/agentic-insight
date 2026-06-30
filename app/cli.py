"""
app/cli.py - CLI entry point for the Google ADK workflow engine.

This module exposes the 'create-data-mapping' command, parses arguments,
manages worker threads, implements checkpoint/resume support, and writes results to DuckDB.
"""

import click
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.db import init_db, get_processed_classes, save_class_mapping, get_all_mappings
from app.ontology import find_loan_folder, get_ttl_files, parse_ontology_classes
from app.dataset import get_all_csv_columns, format_columns_for_prompt
from app.agent import run_mapping_agent

# Setup logger
logger = logging.getLogger(__name__)

@click.group()
def cli():
    """Google ADK Agent Workflow Engine CLI."""
    pass

@cli.command(name="create-data-mapping")
@click.option("--ontology-folder", required=True, type=click.Path(exists=True, file_okay=False), help="Path to ontology folder.")
@click.option("--dataset-folder", required=True, type=click.Path(exists=True, file_okay=False), help="Path to dataset folder.")
@click.option("--thread-count", default=4, type=int, show_default=True, help="Number of concurrent worker threads.")
@click.option("--db-path", required=True, type=click.Path(), help="Path to the DuckDB file.")
@click.option("--output-yaml", default="mapping.yaml", show_default=True, type=click.Path(), help="Path to write the final YAML mapping file.")
def create_data_mapping(ontology_folder: str, dataset_folder: str, thread_count: int, db_path: str, output_yaml: str):
    """
    Parses ontology files in the LOAN directory and maps classes/attributes
    to CSV columns using Google ADK Agent multi-threaded execution.
    """
    logger.info("Starting create-data-mapping workflow...")
    
    # 1. Initialize DuckDB schema
    try:
        init_db(db_path)
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)

    # 2. Get already completed classes for checkpoint support
    completed_class_uris = get_processed_classes(db_path)

    # 3. Locate LOAN directory and extract classes
    try:
        loan_folder = find_loan_folder(ontology_folder)
        ttl_files = get_ttl_files(loan_folder)
        all_classes = parse_ontology_classes(ttl_files)
    except Exception as e:
        logger.error(f"Error parsing ontology: {e}")
        sys.exit(1)

    if not all_classes:
        logger.warning("No OWL classes found under LOAN directory.")
        return

    # 4. Extract CSV dataset columns
    csv_columns = get_all_csv_columns(dataset_folder)
    if not csv_columns:
        logger.error("No CSV datasets found to map.")
        sys.exit(1)
        
    formatted_columns = format_columns_for_prompt(csv_columns)

    # 5. Filter classes using checkpoints
    classes_to_process = []
    for cls in all_classes:
        if cls["class_uri"] in completed_class_uris:
            logger.info(f"Skipping class '{cls['class_name']}' - already processed in previous run.")
        else:
            classes_to_process.append(cls)

    total_to_process = len(classes_to_process)
    logger.info(f"Total classes found: {len(all_classes)}. To process: {total_to_process}")

    if total_to_process == 0:
        logger.info("All classes have been mapped successfully. Workflow finished.")
        return

    # 6. Process in worker threads and write from the main thread
    logger.info(f"Spawning ThreadPoolExecutor with {thread_count} threads...")
    
    success_count = 0
    failure_count = 0
    
    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        # Submit mapping jobs
        futures = {
            executor.submit(run_mapping_agent, cls, formatted_columns): cls
            for cls in classes_to_process
        }
        
        for future in as_completed(futures):
            cls_info = futures[future]
            class_name = cls_info["class_name"]
            
            try:
                # Wait for thread to finish and return json output
                mapping_result = future.result()
                
                # Main thread writes result to DuckDB
                save_class_mapping(db_path, mapping_result)
                success_count += 1
                logger.info(f"Successfully processed and stored mapping for {class_name} ({success_count}/{total_to_process})")
            except Exception as e:
                failure_count += 1
                logger.error(f"Failed to process mapping for {class_name}: {e}")

    logger.info(f"Workflow finished. Successes: {success_count}, Failures: {failure_count}")

    # 7. Generate YAML output
    try:
        write_yaml_mapping(db_path, dataset_folder, output_yaml)
    except Exception:
        sys.exit(1)

def write_yaml_mapping(db_path: str, dataset_folder: str, output_yaml: str) -> None:
    """
    Retrieves all mappings from the database, filters out empty or insufficient ones,
    calculates CSV column coverage, and serializes the mappings into a detailed YAML report file.
    """
    logger.info(f"Generating YAML mapping output to: {output_yaml}")
    try:
        import yaml

        # 1. Retrieve total unique CSV columns from dataset folder
        csv_columns = get_all_csv_columns(dataset_folder)
        total_csv_columns = set()
        for cols in csv_columns.values():
            total_csv_columns.update(cols)
        total_count = len(total_csv_columns)

        # 2. Retrieve all mappings (cumulative, including resumed ones)
        raw_mappings = get_all_mappings(db_path)

        # 3. Filter and structure the mappings (exclude empty/insufficient/not-applicable ones)
        filtered_mappings = {}
        mapped_csv_columns = set()
        cwd = os.getcwd()

        for ttl_path, class_map in raw_mappings.items():
            # Process keys to make TTL paths relative to current working directory
            if os.path.isabs(ttl_path) and ttl_path.startswith(cwd):
                rel_path = os.path.relpath(ttl_path, cwd)
            else:
                rel_path = ttl_path

            filtered_classes = {}
            for class_name, class_info in class_map.items():
                filtered_attributes = {}
                attributes = class_info.get("attributes", {})

                for attr_name, attr_info in attributes.items():
                    mapped_cols = attr_info.get("mapped_columns", [])
                    not_enough_info = attr_info.get("not_enough_information", False)

                    # Exclude empty lists, not enough information, or not applicable
                    if len(mapped_cols) == 0 or not_enough_info:
                        continue

                    filtered_attributes[attr_name] = {
                        "mapped_columns": mapped_cols,
                        "sql_formula": attr_info.get("sql_formula"),
                        "explanation": attr_info.get("explanation")
                    }
                    # Add to mapped CSV columns set
                    mapped_csv_columns.update(mapped_cols)

                if filtered_attributes:
                    filtered_classes[class_name] = {
                        "attributes": filtered_attributes
                    }

            if filtered_classes:
                filtered_mappings[rel_path] = filtered_classes

        # 4. Calculate coverage percentage (matching only existing CSV columns)
        mapped_exist = mapped_csv_columns.intersection(total_csv_columns)
        coverage_pct = 0.0
        if total_count > 0:
            coverage_pct = (len(mapped_exist) / total_count) * 100.0

        # Log coverage information
        stats_str = f"Coverage: {coverage_pct:.2f}% ({len(mapped_exist)} out of {total_count} CSV columns successfully mapped)"
        logger.info(stats_str)

        # 5. Write to file with the coverage stats as a header comment
        yaml_content = yaml.safe_dump(filtered_mappings, sort_keys=False, default_flow_style=False)
        with open(output_yaml, "w", encoding="utf-8") as f:
            f.write(f"# {stats_str}\n")
            f.write(yaml_content)

        logger.info(f"Successfully wrote YAML mapping to: {output_yaml}")
    except Exception as e:
        logger.error(f"Failed to generate YAML mapping: {e}")
        raise e

@cli.command(name="generate-report")
@click.option("--db-path", required=True, type=click.Path(exists=True, file_okay=True, dir_okay=False), help="Path to the DuckDB file.")
@click.option("--dataset-folder", required=True, type=click.Path(exists=True, file_okay=False), help="Path to dataset folder.")
@click.option("--output-yaml", default="mapping.yaml", show_default=True, type=click.Path(), help="Path to write the final YAML mapping file.")
def generate_report(db_path: str, dataset_folder: str, output_yaml: str):
    """
    Generates the mapping.yaml file from the DuckDB database.
    """
    logger.info("Starting generate-report command...")
    try:
        write_yaml_mapping(db_path, dataset_folder, output_yaml)
    except Exception as e:
        logger.error(f"Failed to generate report: {e}")
        sys.exit(1)

@cli.command(name="llm-wiki")
@click.option("--input-folder", required=True, type=click.Path(exists=True, file_okay=False), help="Path to input folder.")
@click.option("--output-folder", required=True, type=click.Path(), help="Path to output folder.")
@click.option("--ontology-folder", "--ontology-ttl", "ontology_ttl", required=True, type=click.Path(exists=True), help="Path to ontology folder or file.")
@click.option("--chunk-line-count", default=1000, type=int, show_default=True, help="Chunk line count.")
@click.option("--db-path", required=True, type=click.Path(), help="Path to the DuckDB file.")
def llm_wiki(input_folder: str, output_folder: str, ontology_ttl: str, chunk_line_count: int, db_path: str):
    """
    Converts PDFs to Markdown, segments and classifies content using LLM, reconciles concepts, and generates coverage.
    """
    logger.info("Starting llm-wiki command...")
    import asyncio
    from app.wiki import execute_wiki_workflow
    try:
        asyncio.run(execute_wiki_workflow(input_folder, output_folder, ontology_ttl, chunk_line_count, db_path))
    except Exception as e:
        logger.error(f"Error executing llm-wiki: {e}")
        sys.exit(1)

if __name__ == "__main__":
    cli()
