"""
scripts/list_db.py - DuckDB Schema Inspector.

This script connects to a local DuckDB database file provided via command line
arguments, reads the system catalog tables, and lists all non-internal tables,
along with their columns, data types, indexes, and primary key/unique constraints.
"""

import sys
import os
import argparse
import logging
import duckdb

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def list_database_schema(db_path: str) -> None:
    """
    Connects to the DuckDB database at db_path and prints its schema information.
    
    This retrieves tables, column names/types, constraints, and indexes.
    
    Args:
        db_path: Path to the DuckDB file.
    """
    logger.info(f"Connecting to DuckDB database at '{db_path}' in read-only mode...")
    
    # Connect to the database in read-only mode to prevent any modification locks
    try:
        conn = duckdb.connect(db_path, read_only=True)
    except Exception as e:
        logger.error(f"Failed to connect to DuckDB database at '{db_path}': {e}")
        sys.exit(1)
        
    try:
        # Get all non-internal tables
        tables_query = """
            SELECT schema_name, table_name, estimated_size, column_count
            FROM duckdb_tables()
            WHERE NOT internal
            ORDER BY schema_name, table_name;
        """
        logger.debug("Executing query to retrieve tables")
        tables = conn.execute(tables_query).fetchall()
        
        if not tables:
            print("No user tables found in the database.")
            return

        print(f"\nDatabase Schema for: {os.path.abspath(db_path)}")
        print("=" * 80)
        
        for schema_name, table_name, est_size, col_count in tables:
            full_table_name = f"{schema_name}.{table_name}"
            print(f"\nTable: {full_table_name} (Estimated rows: {est_size}, Columns: {col_count})")
            print("-" * 80)
            
            # Get columns for this table
            columns_query = """
                SELECT column_name, data_type, is_nullable, column_default
                FROM duckdb_columns()
                WHERE schema_name = ? AND table_name = ?
                ORDER BY column_index;
            """
            logger.debug(f"Retrieving columns for table {full_table_name}")
            columns = conn.execute(columns_query, [schema_name, table_name]).fetchall()
            
            print("  Columns:")
            for col_name, data_type, is_nullable, default_val in columns:
                null_str = "NULL" if is_nullable == "YES" or is_nullable is True or is_nullable == 1 else "NOT NULL"
                default_str = f" DEFAULT {default_val}" if default_val else ""
                print(f"    - {col_name:<30} {data_type:<15} {null_str}{default_str}")
                
            # Get constraints for this table
            constraints_query = """
                SELECT constraint_type, constraint_column_names, constraint_text
                FROM duckdb_constraints()
                WHERE schema_name = ? AND table_name = ?;
            """
            logger.debug(f"Retrieving constraints for table {full_table_name}")
            constraints = conn.execute(constraints_query, [schema_name, table_name]).fetchall()
            
            if constraints:
                print("  Constraints:")
                for c_type, c_cols, c_text in constraints:
                    cols_str = ", ".join(c_cols) if isinstance(c_cols, list) else str(c_cols)
                    print(f"    - {c_type}: {c_text} (on columns: {cols_str})")
            
            # Get indexes for this table
            indexes_query = """
                SELECT index_name, is_unique, is_primary, sql
                FROM duckdb_indexes()
                WHERE schema_name = ? AND table_name = ?;
            """
            logger.debug(f"Retrieving indexes for table {full_table_name}")
            indexes = conn.execute(indexes_query, [schema_name, table_name]).fetchall()
            
            if indexes:
                print("  Indexes:")
                for idx_name, is_uniq, is_prim, sql_def in indexes:
                    uniq_str = "UNIQUE " if is_uniq else ""
                    prim_str = "PRIMARY KEY " if is_prim else ""
                    def_str = f" (SQL: {sql_def})" if sql_def else ""
                    print(f"    - {idx_name} ({uniq_str}{prim_str}index){def_str}")
            else:
                print("  Indexes: None")
                
        print("=" * 80)
        
    except Exception as e:
        logger.error(f"Error querying database schema: {e}")
        sys.exit(1)
    finally:
        conn.close()

def main() -> None:
    """
    Main entry point for the list_db CLI.
    """
    parser = argparse.ArgumentParser(
        description="Connects to a DuckDB database file and lists all tables, columns, types, and indexes."
    )
    parser.add_argument(
        "db_path",
        help="Path to the DuckDB database file."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging output."
    )
    
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
        
    if not os.path.exists(args.db_path):
        logger.error(f"Database file not found: {args.db_path}")
        sys.exit(1)
        
    list_database_schema(args.db_path)

if __name__ == "__main__":
    main()
