"""
api/utils.py - Utility functions for the Agentic Search backend.

This module implements:
1. BM25 ranking and reranking for filtering list of concepts.
2. Recursive chunk-based summarization (condense_summary) to respect model context size constraints.
"""

import os
import re
import math
import logging
import uuid
from typing import List, Dict, Any
from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from app.config import get_llm_model

logger = logging.getLogger(__name__)

# Pydantic schema for structured summarization output
class ChunkSummary(BaseModel):
    summary: str = Field(description="The condensed and deduplicated summary of the text chunk.")

def bm25_rank(query: str, corpus: List[str], top_n: int = 100) -> List[str]:
    """
    Ranks the documents (list of strings) against the query string using the BM25 algorithm.

    Args:
        query (str): The search query.
        corpus (List[str]): A list of document strings to rank.
        top_n (int): The maximum number of ranked documents to return.

    Returns:
        List[str]: The top_n ranked documents.
    """
    if not corpus:
        return []

    def tokenize(text: str) -> List[str]:
        return re.findall(r"\w+", text.lower())

    query_tokens = tokenize(query)
    if not query_tokens:
        return corpus[:top_n]

    doc_tokens = [tokenize(doc) for doc in corpus]
    doc_lens = [len(tokens) for tokens in doc_tokens]
    avg_doc_len = sum(doc_lens) / len(corpus) if corpus else 1.0

    # Document frequency
    df = {}
    for token in query_tokens:
        df[token] = sum(1 for tokens in doc_tokens if token in tokens)

    # BM25 parameters
    k1 = 1.5
    b = 0.75
    N = len(corpus)

    # Inverse Document Frequency (IDF)
    idf = {}
    for token, f in df.items():
        # Smooth formula to avoid log of non-positive numbers
        idf[token] = math.log((N - f + 0.5) / (f + 0.5) + 1.0)

    # Calculate BM25 scores
    scores = []
    for i, tokens in enumerate(doc_tokens):
        score = 0.0
        doc_len = doc_lens[i]
        tf = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1

        for token in query_tokens:
            if token in tf:
                freq = tf[token]
                numerator = idf[token] * freq * (k1 + 1)
                denominator = freq + k1 * (1.0 - b + b * (doc_len / avg_doc_len))
                score += numerator / denominator
        scores.append((score, i))

    # Sort documents by score descending
    scores.sort(key=lambda x: x[0], reverse=True)
    return [corpus[idx] for _, idx in scores[:top_n]]

def reranking(query_string: str, list_of_string: List[str], top_n: int = None) -> List[str]:
    """
    Performs BM25 search over a list of strings if the count exceeds top-n threshold.

    Args:
        query_string (str): The search query.
        list_of_string (List[str]): List of documents/strings.
        top_n (int, optional): The threshold limit, reads from TOP_N env variable or defaults to 100.

    Returns:
        List[str]: Filtered list of strings.
    """
    if top_n is None:
        top_n = int(os.getenv("TOP_N", "100"))

    logger.debug(f"Reranking list of size {len(list_of_string)} against query: '{query_string}' (top_n={top_n})")
    if len(list_of_string) <= top_n:
        return list_of_string

    return bm25_rank(query_string, list_of_string, top_n)

async def condense_summary(
    context: str,
    model: Any = None,
    lines_threshold: int = None,
    context_size_limit: int = None
) -> str:
    """
    Recursively condenses a large context by dividing it into chunks and summarizing them.

    Args:
        context (str): The text content to condense.
        model (Any, optional): Configured LLM model instance.
        lines_threshold (int, optional): Number of lines threshold, defaults to LINES_THRESHOLD env (default 500).
        context_size_limit (int, optional): Target size limit in words, defaults to CONTEXT_SIZE_LIMIT env (default 10000).

    Returns:
        str: Condensed summary context.
    """
    if lines_threshold is None:
        lines_threshold = int(os.getenv("LINES_THRESHOLD", "500"))
    if context_size_limit is None:
        context_size_limit = int(os.getenv("CONTEXT_SIZE_LIMIT", "10000"))

    lines = context.splitlines()
    if len(lines) < lines_threshold:
        logger.debug(f"Context lines ({len(lines)}) < threshold ({lines_threshold}). Returning original context.")
        return context

    logger.info(f"Context lines ({len(lines)}) >= threshold ({lines_threshold}). Summarizing in chunks...")
    if model is None:
        model = get_llm_model()

    # Split into 500-line chunks
    chunks = []
    chunk_size = 500
    for i in range(0, len(lines), chunk_size):
        chunk_lines = lines[i : i + chunk_size]
        chunks.append("\n".join(chunk_lines))

    # Summarization word limit per chunk
    num_chunks = len(chunks)
    chunk_word_limit = max(50, int(context_size_limit / num_chunks))
    logger.info(f"Split context into {num_chunks} chunks. Chunk word limit: {chunk_word_limit} words.")

    chunk_summaries = []
    for idx, chunk in enumerate(chunks):
        logger.info(f"Summarizing chunk {idx + 1}/{num_chunks}")
        prompt = f"""You are a precise data architect. 
Deduplicate similar contents and summarize the following text. 
The summary MUST be concise and strictly under {chunk_word_limit} words.

Text to summarize:
{chunk}
"""
        agent = LlmAgent(
            model=model,
            name=f"chunk_summarizer_{idx}",
            instruction="You are a data architect summarizing context under strict constraints.",
            output_schema=ChunkSummary
        )
        runner = InMemoryRunner(agent=agent)
        session_id = f"session_{uuid.uuid4().hex}"
        await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)
        
        new_message = types.Content(parts=[types.Part(text=prompt)])
        events = []
        async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
            events.append(event)
            if event.error_message:
                raise ValueError(f"LLM summarization failed: {event.error_message}")

        model_text = ""
        for event in reversed(events):
            if event.content and event.content.role == 'model' and event.content.parts:
                model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                break

        if not model_text.strip() and events and events[-1].output:
            summary_obj = events[-1].output
            summary_text = summary_obj.summary
        else:
            from app.agent import clean_json_text
            cleaned_json = clean_json_text(model_text)
            from google.adk.utils._schema_utils import validate_schema
            result_dict = validate_schema(ChunkSummary, cleaned_json)
            summary_text = result_dict.get("summary", "")

        chunk_summaries.append(summary_text)

    combined_summary = "\n\n".join(chunk_summaries)
    
    # Recursively condense the combined summary
    return await condense_summary(combined_summary, model, lines_threshold, context_size_limit)


def clean_and_filter_mapping(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cleans and filters semantic mapping data using the schema-aware sanitization function.
    """
    db_path = os.getenv("MAPPINGS_DB_PATH", "./mappings.db")
    return sanitize_semantic_mapping(data, db_path)


def sanitize_semantic_mapping(mapping_data: Dict[str, Any], db_path: str) -> Dict[str, Any]:
    """
    Validates and sanitizes the semantic mapping against the actual DuckDB database schema.
    It removes columns from mapped_columns that do not exist, and invalidates (sets to None)
    any SQL formulas that refer to nonexistent columns or tables.

    Args:
        mapping_data (Dict[str, Any]): The raw loaded semantic mapping.
        db_path (str): Path to the DuckDB database.

    Returns:
        Dict[str, Any]: The schema-compliant sanitized semantic mapping.
    """
    if not isinstance(mapping_data, dict):
        return mapping_data

    import duckdb

    # Connect and query the schema
    conn = duckdb.connect(db_path)
    try:
        # Exclude processed metadata and loan_prediction_train (as per request)
        tables = conn.execute("""
            SELECT table_name, column_name 
            FROM information_schema.columns 
            WHERE table_schema = 'main' 
              AND table_name NOT IN ('processed_classes', 'attribute_mappings', 'file_line_sequence', 'text_segments', 'concepts', 'concept_segment_mapping', 'loan_prediction_train')
        """).fetchall()
    except Exception as e:
        logger.error(f"Error fetching schema for sanitization: {e}")
        return mapping_data
    finally:
        conn.close()

    # Create maps for lookup
    valid_tables = set()
    valid_columns = set()
    for t_name, col_name in tables:
        valid_tables.add(t_name.lower())
        valid_columns.add(col_name.lower())

    # Standard SQL keywords and built-in functions to ignore
    sql_keywords = {
        'coalesce', 'null', 'sum', 'count', 'avg', 'min', 'max', 'select', 'from',
        'join', 'on', 'where', 'group', 'by', 'order', 'limit', 'and', 'or', 'not',
        'in', 'is', 'as', 'cast', 'case', 'when', 'then', 'else', 'end', 'like', 'ilike'
    }

    def sanitize_dict_recursive(d: Any) -> Any:
        if not isinstance(d, dict):
            return d

        if "attributes" in d and isinstance(d["attributes"], dict):
            cleaned_attributes = {}
            for attr_name, attr_content in d["attributes"].items():
                if not isinstance(attr_content, dict):
                    cleaned_attributes[attr_name] = attr_content
                    continue

                # Strip explanation
                attr_content.pop("explanation", None)

                mapped_cols = attr_content.get("mapped_columns", [])
                # Filter mapped columns: keep only those present in the actual schema
                filtered_cols = [col for col in mapped_cols if col.lower() in valid_columns]

                if not filtered_cols:
                    attr_content["mapped_columns"] = []
                    attr_content["sql_formula"] = None
                    attr_content["not_enough_information"] = True
                else:
                    attr_content["mapped_columns"] = filtered_cols
                    # Check sql_formula
                    sql_formula = attr_content.get("sql_formula")
                    if sql_formula:
                        # Clean out string literals to avoid matching table/column names inside them
                        formula_clean = re.sub(r"'[^']*'", "", sql_formula)
                        formula_clean = re.sub(r'"[^"]*"', "", formula_clean)
                        
                        # Extract alphanumeric identifiers, including dot notation (e.g. table_name.col_name)
                        tokens = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_\.]*\b', formula_clean)
                        
                        formula_valid = True
                        for token in tokens:
                            token_lower = token.lower()
                            # If it is a keyword, skip
                            if token_lower in sql_keywords:
                                continue
                            
                            # If the entire token is a valid column or valid table, it's valid
                            if token_lower in valid_columns or token_lower in valid_tables:
                                continue
                            
                            # Handle dot notation (e.g. loan_data.installment or loan_data.log.annual.inc)
                            if '.' in token_lower:
                                parts = token_lower.split('.')
                                # If table is invalid, formula is invalid
                                if parts[0] not in valid_tables:
                                    formula_valid = False
                                    break
                                # Re-join the rest to see if it's a valid column (e.g. log.annual.inc)
                                col_part = '.'.join(parts[1:])
                                if col_part not in valid_columns:
                                    formula_valid = False
                                    break
                            else:
                                # It is a single identifier, but not a valid column or table name
                                formula_valid = False
                                break
                        
                        if not formula_valid:
                            logger.info(f"Invalidating sql_formula due to schema mismatch: '{sql_formula}'")
                            attr_content["sql_formula"] = None

                cleaned_attributes[attr_name] = attr_content
            
            d["attributes"] = cleaned_attributes
            return d

        return {k: sanitize_dict_recursive(v) for k, v in d.items()}

    return sanitize_dict_recursive(mapping_data)



def sanitize_semantic_mapping_file(file_path: str, db_path: str):
    """
    Loads, sanitizes, and writes back the semantic mapping YAML file.
    
    Args:
        file_path (str): Path to the semantic mapping YAML file.
        db_path (str): Path to the DuckDB database.
    """
    if not os.path.exists(file_path):
        logger.warning(f"Semantic mapping file not found at: {file_path}")
        return
        
    import yaml
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            
        sanitized_data = sanitize_semantic_mapping(data, db_path)
        
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(sanitized_data, f, sort_keys=False, default_flow_style=False)
            
        logger.info(f"Successfully sanitized and saved semantic mapping file: {file_path}")
    except Exception as e:
        logger.error(f"Error sanitizing semantic mapping file {file_path}: {e}")
        raise e


def check_sql_injection(sql: str) -> None:
    """
    Checks the generated SQL query for potential SQL injection or disallowed modification statements.
    Only allows a single, read-only SELECT statement.
    """
    if not sql or not sql.strip():
        raise ValueError("SQL query is empty.")

    logger.debug(f"Validating SQL for potential injection: {sql}")

    # 1. Strip comments to prevent comment-based bypasses
    sql_clean = re.sub(r"--.*$", "", sql, flags=re.MULTILINE)
    sql_clean = re.sub(r"/\*.*?\*/", "", sql_clean, flags=re.DOTALL).strip()

    # 2. Check for disallowed functions/keywords
    disallowed_funcs = [
        "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
        "read_ndjson", "read_ndjson_auto", "read_blob", "read_text", "parquet_scan",
        "scan_parquet", "glob", "getenv", "system", "query_directory", "write_csv"
    ]
    disallowed_funcs_pattern = re.compile(
        r"\b(" + "|".join(disallowed_funcs) + r")\b",
        re.IGNORECASE
    )
    if disallowed_funcs_pattern.search(sql_clean):
        matched = disallowed_funcs_pattern.search(sql_clean).group(0)
        raise ValueError(f"Disallowed function call detected in SQL query: '{matched}'")

    # 3. Check for system tables or metadata access
    disallowed_system_patterns = [
        r"\binformation_schema\b",
        r"\bsqlite_master\b",
        r"\bduckdb_[a-zA-Z0-9_]+\b",
        r"\bpg_[a-zA-Z0-9_]+\b",
    ]
    disallowed_system_pattern = re.compile(
        "|".join(disallowed_system_patterns),
        re.IGNORECASE
    )
    if disallowed_system_pattern.search(sql_clean):
        matched = disallowed_system_pattern.search(sql_clean).group(0)
        raise ValueError(f"Disallowed system table or catalog access detected in SQL query: '{matched}'")

    # 4. Check for file-like strings or URLs in string literals
    file_pattern = re.compile(
        r"(['\"])(?:(?!\1).)*?(?:\.(?:csv|parquet|json|ndjson|db|sqlite|txt|tsv)|https?://|s3://)(?:(?!\1).)*?\1",
        re.IGNORECASE
    )
    if file_pattern.search(sql_clean):
        matched = file_pattern.search(sql_clean).group(0)
        raise ValueError(f"Disallowed file path or external URL literal detected in SQL query: {matched}")

    # 5. Extract and validate statement using DuckDB's parser directly on the module
    import duckdb
    try:
        statements = duckdb.extract_statements(sql_clean)
    except Exception as parse_err:
        raise ValueError(f"SQL statement parsing failed: {parse_err}")

    if len(statements) == 0:
        raise ValueError("No SQL statements found.")
    
    if len(statements) > 1:
        raise ValueError("Multiple SQL statements detected (stacked queries are forbidden).")

    stmt = statements[0]
    if stmt.type != duckdb.StatementType.SELECT:
        raise ValueError(f"Disallowed SQL statement type: {stmt.type.name}. Only SELECT queries are permitted.")



