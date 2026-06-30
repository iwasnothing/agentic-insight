"""
tests/test_list_db.py - Unit tests for scripts/list_db.py.
"""

import os
import tempfile
import pytest
import duckdb
from scripts.list_db import list_database_schema

def test_list_database_schema(capsys):
    """
    Verify that list_database_schema correctly connects, queries,
    and prints tables, columns, types, constraints, and indexes.
    """
    # Create a temporary database file
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        
        # Populate the database with sample tables, constraints, and an index
        conn = duckdb.connect(db_path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR, age INTEGER)")
        conn.execute("CREATE TABLE posts (id INTEGER, title VARCHAR, user_id INTEGER, FOREIGN KEY(user_id) REFERENCES users(id))")
        conn.execute("CREATE INDEX idx_users_name ON users (name)")
        conn.close()
        
        # Run list_database_schema and capture output
        list_database_schema(db_path)
        
        captured = capsys.readouterr()
        output = captured.out
        
        # Verify tables are listed
        assert "Table: main.users" in output
        assert "Table: main.posts" in output
        
        # Verify columns and types
        assert "id" in output
        assert "INTEGER" in output
        assert "name" in output
        assert "VARCHAR" in output
        
        # Verify indexes are listed
        assert "idx_users_name" in output
        
        # Verify constraints
        assert "PRIMARY KEY" in output
