---
name: duckdb-skill
description: Use DuckDB for local SQL queries and lightweight analytics.
---

# DuckDB Skill

Use this skill when you need to run queries, inspect tables, describe tables, or do database modifications on the local DuckDB database.

## Rules
1. Inspect the schema first if the table is unknown.
2. Prefer read-only queries (e.g. SELECT) unless explicitly instructed to update or insert.
3. Use the local DuckDB MCP tools (like `list_tables`, `describe_table`, `execute_sql`, `run_sql`, `select_table`, `insert_table`, `update_table`) for execution.
4. Explain results briefly and clearly.
