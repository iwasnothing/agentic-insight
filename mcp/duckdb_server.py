"""
mcp/duckdb_server.py - Local FastMCP server for DuckDB database operations.

This module exposes tools to list tables, describe tables, run arbitrary SQL,
and perform generic insert/update/select operations.
It connects to the database path specified in the DB_PATH environment variable,
or defaults to mappings.db.
"""

import os
import logging
import duckdb
from typing import List, Dict, Any, Optional
from mcp.server.fastmcp import FastMCP
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import TokenVerifier, AccessToken
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("duckdb_mcp_server")

# Load environment variables
load_dotenv()

# Verify that MCP_BEARER_TOKEN is configured in the environment
EXPECTED_TOKEN = os.environ.get("MCP_BEARER_TOKEN")
if not EXPECTED_TOKEN:
    logger.error("MCP_BEARER_TOKEN environment variable is not set. MCP server requires authentication.")
    raise ValueError("MCP_BEARER_TOKEN environment variable is not set.")

class StaticTokenVerifier(TokenVerifier):
    """
    Token verifier that checks for a static bearer token from the environment.
    """
    async def verify_token(self, token: str) -> AccessToken | None:
        """
        Verify the static bearer token.
        """
        if token == EXPECTED_TOKEN:
            return AccessToken(
                token=token,
                client_id="default-client",
                scopes=["all"],
                expires_at=None
            )
        return None

# Configure authentication settings
auth_settings = AuthSettings(
    issuer_url="http://localhost",
    resource_server_url="http://localhost"
)

DB_PATH = os.environ.get("DB_PATH", "mappings.db")
DB_READ_ONLY = os.environ.get("DB_READ_ONLY", "false").lower() in ("true", "1", "yes")
logger.info(f"Initializing DuckDB FastMCP Server. DB_PATH={DB_PATH}, DB_READ_ONLY={DB_READ_ONLY}")

mcp = FastMCP(
    "duckdb-local",
    auth=auth_settings,
    token_verifier=StaticTokenVerifier()
)


def conn() -> duckdb.DuckDBPyConnection:
    """
    Establish a connection to the configured DuckDB database.
    """
    logger.debug(f"Connecting to DuckDB database: {DB_PATH} (read_only={DB_READ_ONLY})")
    try:
        c = duckdb.connect(DB_PATH, read_only=DB_READ_ONLY)
        # Apply sandboxing: disable external access and lock configuration settings
        c.execute("SET enable_external_access = false;")
        c.execute("SET lock_configuration = true;")
        return c
    except Exception as e:
        logger.exception("Failed to connect or configure DuckDB database")
        raise e

@mcp.tool()
def list_tables() -> Dict[str, List[str]]:
    """
    List all tables available in the DuckDB main schema.
    """
    logger.info("mcp.list_tables() called")
    c = conn()
    try:
        rows = c.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()
        tables = [r[0] for r in rows]
        logger.info(f"Found tables: {tables}")
        return {"tables": tables}
    except Exception as e:
        logger.exception("Failed to list tables")
        raise e
    finally:
        c.close()

@mcp.tool()
def describe_table(table_name: str) -> Dict[str, Any]:
    """
    Get column names, data types, and schemas for a specific table.
    """
    logger.info(f"mcp.describe_table(table_name='{table_name}') called")
    c = conn()
    try:
        rows = c.execute(f"DESCRIBE {table_name}").fetchall()
        schema = [list(r) for r in rows]
        return {"schema": schema}
    except Exception as e:
        logger.exception(f"Failed to describe table '{table_name}'")
        raise e
    finally:
        c.close()

@mcp.tool()
def execute_sql(sql: str) -> Dict[str, Any]:
    """
    Execute arbitrary SQL query on the DuckDB database and return results.
    """
    logger.info(f"mcp.execute_sql() called with query:\n{sql}")
    c = conn()
    try:
        df = c.execute(sql).fetchdf()
        columns = list(df.columns)
        rows = df.to_dict(orient="records")
        logger.info(f"Query succeeded. Returned {len(rows)} rows.")
        return {"columns": columns, "rows": rows}
    except Exception as e:
        logger.exception(f"Failed to execute SQL: {sql}")
        raise e
    finally:
        c.close()

@mcp.tool()
def run_sql(sql: str) -> Dict[str, Any]:
    """
    Alias for execute_sql. Execute arbitrary SQL query on the DuckDB database.
    """
    return execute_sql(sql)

@mcp.tool()
def select_table(
    table_name: str, 
    columns: Optional[List[str]] = None, 
    condition: Optional[str] = None, 
    limit: int = 100
) -> Dict[str, Any]:
    """
    Select rows from a specific table, applying column projection, optional WHERE condition, and row limit.
    """
    logger.info(f"mcp.select_table(table_name='{table_name}', columns={columns}, condition='{condition}', limit={limit}) called")
    c = conn()
    try:
        cols = "*"
        if columns:
            cols = ", ".join([f'"{col}"' if "." in col else col for col in columns])
        
        sql = f"SELECT {cols} FROM {table_name}"
        if condition:
            sql += f" WHERE {condition}"
        sql += f" LIMIT {limit}"
        
        logger.debug(f"Generated SQL: {sql}")
        df = c.execute(sql).fetchdf()
        return {"columns": list(df.columns), "rows": df.to_dict(orient="records")}
    except Exception as e:
        logger.exception(f"Failed to run select query on table '{table_name}'")
        raise e
    finally:
        c.close()

@mcp.tool()
def insert_table(table_name: str, data: Dict[str, Any]) -> Dict[str, str]:
    """
    Insert a new row into the specified table using key-value pairs.
    """
    logger.info(f"mcp.insert_table(table_name='{table_name}', data={data}) called")
    c = conn()
    try:
        cols = ", ".join([f'"{k}"' if "." in k else k for k in data.keys()])
        placeholders = ", ".join(["?"] * len(data))
        sql = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"
        
        logger.debug(f"Executing SQL: {sql} with values {list(data.values())}")
        c.execute(sql, list(data.values()))
        return {"status": "success"}
    except Exception as e:
        logger.exception(f"Failed to insert into table '{table_name}'")
        raise e
    finally:
        c.close()

@mcp.tool()
def update_table(table_name: str, data: Dict[str, Any], condition: str) -> Dict[str, str]:
    """
    Update matching rows in the specified table with new values, subject to the condition.
    """
    logger.info(f"mcp.update_table(table_name='{table_name}', data={data}, condition='{condition}') called")
    c = conn()
    try:
        set_clause = ", ".join([f'"{k}" = ?' if "." in k else f"{k} = ?" for k in data.keys()])
        sql = f"UPDATE {table_name} SET {set_clause}"
        if condition:
            sql += f" WHERE {condition}"
            
        logger.debug(f"Executing SQL: {sql} with values {list(data.values())}")
        c.execute(sql, list(data.values()))
        return {"status": "success"}
    except Exception as e:
        logger.exception(f"Failed to update table '{table_name}'")
        raise e
    finally:
        c.close()

if __name__ == "__main__":
    mcp.run()
