"""
api/main.py - FastAPI Application Entrypoint for Agentic Search backend.

This module exposes the API:
- POST /run: Runs the agentic search workflow with tabular and document audit layers.
It also ensures CSV files are loaded into DuckDB tables before running.
"""

import os
import glob
import logging
import duckdb
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

from api.workflow import run_agentic_workflow

# Setup logger
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Agentic Search API Backend",
    description="FastAPI backend to execute agentic search on loan documentation and tabular records.",
    version="0.1.0"
)

class RunRequest(BaseModel):
    objective: str = Field(description="The given audit/search objective")
    ontology_folder: Optional[str] = Field(default=None, description="Path to the ontology folder")
    dataset_folder: Optional[str] = Field(default=None, description="Path to the dataset folder")
    doc_db_path: Optional[str] = Field(default=None, description="Path to the document/concepts DuckDB database")
    mappings_db_path: Optional[str] = Field(default=None, description="Path to the mappings/tabular DuckDB database")
    semantic_mapping_path: Optional[str] = Field(default=None, description="Path to the semantic mapping YAML file")
    output_md_path: Optional[str] = Field(default=None, description="Path to write the final markdown report")
    max_iterations: Optional[int] = Field(default=None, description="Maximum iterations for planning loop")
    top_n: Optional[int] = Field(default=None, description="Reranking concepts top_n limit")
    row_limit: Optional[int] = Field(default=None, description="Table query row limit limit")
    max_sql_iterations: Optional[int] = Field(default=None, description="Max self-fixing SQL iterations")
    lines_threshold: Optional[int] = Field(default=None, description="Lines threshold for context condensation")
    context_size_limit: Optional[int] = Field(default=None, description="Target size limit in words for condensation")

class RunResponse(BaseModel):
    status: str
    iterations: int
    confidence_score: int
    explanation: str
    report_path: str
    analysis: str

def load_csv_files_to_duckdb(dataset_folder: str, db_path: str) -> None:
    """
    Scans the dataset folder for CSV files and loads them into DuckDB tables.
    Normalized table names are constructed from file basenames.
    """
    logger.info(f"Loading CSV files from {dataset_folder} into DuckDB: {db_path}")
    if not os.path.exists(dataset_folder):
        logger.error(f"Dataset folder does not exist: {dataset_folder}")
        raise FileNotFoundError(f"Dataset folder '{dataset_folder}' not found.")

    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = duckdb.connect(db_path)
    try:
        csv_files = glob.glob(os.path.join(dataset_folder, "*.csv"))
        if not csv_files:
            logger.warning(f"No CSV files found in dataset folder: {dataset_folder}")
            return
            
        for csv_file in csv_files:
            base = os.path.basename(csv_file)
            # Create a clean table name: lowercase, replacing dash/dot with underscores
            table_name = os.path.splitext(base)[0].replace("-", "_").replace(".", "_").lower()
            logger.info(f"Loading CSV: {csv_file} -> Table: {table_name}")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} AS SELECT * FROM '{csv_file}'")
        logger.info("CSV files successfully loaded into DuckDB.")
    except Exception as e:
        logger.error(f"Error loading CSV files to DuckDB: {e}")
        raise e
    finally:
        conn.close()

@app.post("/run", response_model=RunResponse)
async def run_workflow(request: RunRequest):
    """
    Executes the agentic workflow using the document and tabular schemas.
    Reads tabular CSV datasets into DuckDB tables, runs the query/analysis/eval loop, and generates a report.
    """
    # 1. Resolve paths (use request overrides or environment variables or defaults)
    ontology_folder = request.ontology_folder or os.getenv("ONTOLOGY_FOLDER", "./ontology")
    dataset_folder = request.dataset_folder or os.getenv("DATASET_FOLDER", "./dataset")
    doc_db_path = request.doc_db_path or os.getenv("DOC_DB_PATH", "./wiki.db")
    mappings_db_path = request.mappings_db_path or os.getenv("MAPPINGS_DB_PATH", "./mappings.db")
    semantic_mapping_path = request.semantic_mapping_path or os.getenv("SEMANTIC_MAPPING_PATH", "./mapping.yaml")
    output_md_path = request.output_md_path or os.getenv("OUTPUT_MD_PATH", "./report.md")

    # Resolve loop variables and thresholds
    max_iterations = request.max_iterations or int(os.getenv("MAX_ITERATIONS", "10"))
    top_n = request.top_n or int(os.getenv("TOP_N", "100"))
    row_limit = request.row_limit or int(os.getenv("ROW_LIMIT", "100"))
    max_sql_iterations = request.max_sql_iterations or int(os.getenv("MAX_SQL_ITERATIONS", "5"))
    lines_threshold = request.lines_threshold or int(os.getenv("LINES_THRESHOLD", "500"))
    context_size_limit = request.context_size_limit or int(os.getenv("CONTEXT_SIZE_LIMIT", "10000"))

    logger.info("New /run request received.")
    logger.info(f"Parameters: ontology={ontology_folder}, dataset={dataset_folder}, doc_db={doc_db_path}, mappings_db={mappings_db_path}")

    # 2. First read tabular data from CSV files into DuckDB tables (mappings_db)
    try:
        load_csv_files_to_duckdb(dataset_folder, mappings_db_path)
    except Exception as e:
        logger.exception("Failed to load CSV files into DuckDB.")
        raise HTTPException(status_code=500, detail=f"Database initialization failed: {str(e)}")

    # 3. Execute the agentic workflow
    try:
        results = await run_agentic_workflow(
            objective=request.objective,
            ontology_folder=ontology_folder,
            dataset_folder=dataset_folder,
            doc_db_path=doc_db_path,
            mappings_db_path=mappings_db_path,
            semantic_mapping_path=semantic_mapping_path,
            output_md_path=output_md_path,
            max_iterations=max_iterations,
            top_n=top_n,
            row_limit=row_limit,
            max_sql_iterations=max_sql_iterations,
            lines_threshold=lines_threshold,
            context_size_limit=context_size_limit
        )
        return RunResponse(**results)
    except Exception as e:
        logger.exception("Agentic search workflow execution failed.")
        raise HTTPException(status_code=500, detail=f"Agentic workflow failed: {str(e)}")
