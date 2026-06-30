"""
app/db.py - DuckDB storage management module.

This module initializes the tables in the database, queries completed checkpoints,
and persists mapped ontology classes and attributes.
"""

import logging
import json
import datetime
from typing import Set, Dict, Any
import duckdb

logger = logging.getLogger(__name__)

def init_db(db_path: str) -> None:
    """
    Initializes the database schema if the tables do not exist.

    Args:
        db_path (str): Path to the DuckDB file.
    """
    logger.info(f"Initializing database: {db_path}")
    conn = duckdb.connect(db_path)
    try:
        # Create table for completed checkpoint tracker
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_classes (
                class_uri VARCHAR PRIMARY KEY,
                class_name VARCHAR,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Create table for attribute level mappings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS attribute_mappings (
                class_name VARCHAR,
                class_uri VARCHAR,
                source_ttl_file VARCHAR,
                attribute_name VARCHAR,
                mapped_columns VARCHAR,
                sql_formula VARCHAR,
                not_enough_information BOOLEAN,
                explanation VARCHAR,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        logger.info("Database tables initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database schema: {e}")
        raise e
    finally:
        conn.close()

def get_processed_classes(db_path: str) -> Set[str]:
    """
    Queries the database for URIs of already processed classes.

    Args:
        db_path (str): Path to the DuckDB file.

    Returns:
        Set[str]: A set of processed class URIs.
    """
    logger.debug(f"Querying processed classes from database: {db_path}")
    conn = duckdb.connect(db_path)
    processed = set()
    try:
        results = conn.execute("SELECT class_uri FROM processed_classes").fetchall()
        processed = {row[0] for row in results}
        logger.info(f"Loaded {len(processed)} previously processed classes from checkpoint.")
    except Exception as e:
        logger.error(f"Error querying processed classes: {e}")
    finally:
        conn.close()
    return processed

def save_class_mapping(db_path: str, mapping: Dict[str, Any]) -> None:
    """
    Saves a completed class mapping and its attribute details inside a database transaction.

    Args:
        db_path (str): Path to the DuckDB file.
        mapping (Dict[str, Any]): Dictionary matching the ClassMapping Pydantic model.
    """
    class_name = mapping["class_name"]
    class_uri = mapping["class_uri"]
    source_ttl_file = mapping["source_ttl_file"]
    
    logger.info(f"Saving mapping results to DuckDB for class: {class_name} ({class_uri})")
    
    conn = duckdb.connect(db_path)
    try:
        # Begin transaction
        conn.execute("BEGIN TRANSACTION")
        
        # 1. Insert into processed_classes
        now = datetime.datetime.now()
        conn.execute(
            "INSERT INTO processed_classes (class_uri, class_name, processed_at) VALUES (?, ?, ?)",
            [class_uri, class_name, now]
        )
        
        # 2. Insert attribute mappings
        for attr_map in mapping.get("attribute_mappings", []):
            mapped_cols_str = json.dumps(attr_map["mapped_columns"])
            conn.execute(
                """
                INSERT INTO attribute_mappings (
                    class_name, class_uri, source_ttl_file, attribute_name,
                    mapped_columns, sql_formula, not_enough_information, explanation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    class_name,
                    class_uri,
                    source_ttl_file,
                    attr_map["attribute_name"],
                    mapped_cols_str,
                    attr_map.get("sql_formula"),
                    attr_map["not_enough_information"],
                    attr_map.get("explanation"),
                    now
                ]
            )
            
        conn.execute("COMMIT")
        logger.info(f"Successfully committed transaction for {class_name}")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.error(f"Error saving class mapping to DB (rolled back): {e}")
        raise e
    finally:
        conn.close()


def get_all_mappings(db_path: str) -> Dict[str, Any]:
    """
    Retrieves all attribute mappings from the database, grouped hierarchically as:
    ttl_file_path -> class_name -> attributes -> attribute_name -> {mapped_columns, sql_formula, explanation, not_enough_information}

    Args:
        db_path (str): Path to the DuckDB file.

    Returns:
        Dict[str, Any]: A nested dictionary containing all stored mappings with details.
    """
    logger.info(f"Retrieving all mappings from database: {db_path}")
    conn = duckdb.connect(db_path)
    mappings = {}
    try:
        # Check if attribute_mappings table exists
        table_exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'attribute_mappings'"
        ).fetchone()[0]
        
        if not table_exists:
            logger.warning("Table 'attribute_mappings' does not exist in database.")
            return mappings

        results = conn.execute("""
            SELECT source_ttl_file, class_name, attribute_name, mapped_columns, sql_formula, explanation, not_enough_information
            FROM attribute_mappings
            ORDER BY source_ttl_file, class_name, attribute_name
        """).fetchall()

        for row in results:
            source_file = row[0]
            class_name = row[1]
            attr_name = row[2]
            mapped_cols_json = row[3]
            sql_formula = row[4]
            explanation = row[5]
            not_enough_info = bool(row[6])

            try:
                mapped_cols = json.loads(mapped_cols_json)
            except Exception as ex:
                logger.error(f"Failed to parse mapped columns JSON for {class_name}.{attr_name}: {ex}")
                mapped_cols = []

            if source_file not in mappings:
                mappings[source_file] = {}
            if class_name not in mappings[source_file]:
                mappings[source_file][class_name] = {"attributes": {}}

            mappings[source_file][class_name]["attributes"][attr_name] = {
                "mapped_columns": mapped_cols,
                "sql_formula": sql_formula,
                "explanation": explanation,
                "not_enough_information": not_enough_info
            }

        logger.info(f"Successfully retrieved mappings for {len(mappings)} TTL files.")
    except Exception as e:
        logger.error(f"Error querying all mappings: {e}")
        raise e
    finally:
        conn.close()
    return mappings

