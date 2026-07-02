# Google ADK Agent Workflow Engine Blueprint

This blueprint outlines the design, architecture, and schema definitions for the multi-threaded agent workflow engine to be implemented under `app/`.

---

## 1. Architecture Overview

The engine acts as a pipeline that:
1. Identifies the `LOAN` subfolder in the specified ontology folder.
2. Parses all TTL files recursively using RDFLib to extract OWL Classes and their attributes (object/data properties).
3. Reads column headers from all CSV files inside the dataset folder.
4. Initializes a thread pool of worker threads.
5. In each thread, executes a Google ADK Agent (configured for a custom OpenAI-compatible endpoint) to map an OWL Class and its attributes to the dataset columns.
6. Skip classes that have already been mapped (enabling checkpoint/resume support).
7. Serializes the mapping to structured JSON, writes them into a DuckDB database from the main thread, and generates a structured YAML mapping file at the end of the workflow.

```mermaid
graph TD
    A[CLI Invocation] --> B[config.py: Load Environment]
    A --> C[ontology.py: Parse TTLs recursively under LOAN]
    A --> D[dataset.py: Read headers from all CSVs]
    C --> E[db.py: Query completed classes for checkpoint]
    E --> F[Filter Classes to Process]
    F --> G[ThreadPoolExecutor]
    G -->|Worker Thread| H[agent.py: Run ADK Agent]
    H -->|API Call| I[Custom OpenAI Endpoint]
    I -->|Structured Output JSON| H
    H -->|Return JSON| G
    G -->|Main Thread Queue| J[db.py: Write to DuckDB]
```

---

## 2. Configuration & Environment Variables

All configuration is externalized. A `.env` file should contain the parameters:

```env
# Custom OpenAI compatible endpoint configuration
CUSTOM_LLM_URL=https://api.openai.com/v1   # The target API base URL
CUSTOM_LLM_MODEL=gpt-4o                   # The model name/identifier
CUSTOM_LLM_API_KEY=sk-xxxx...             # Authentication key
```

---

## 3. JSON Output Schema (Pydantic Model)

The agent is configured to enforce structured output matching the following schemas:

```python
from pydantic import BaseModel, Field
from typing import List, Optional

class AttributeMapping(BaseModel):
    attribute_name: str = Field(
        description="The local name or URI of the ontology attribute (property)"
    )
    mapped_columns: List[str] = Field(
        description="The list of dataset columns matching or contributing to this attribute"
    )
    sql_formula: Optional[str] = Field(
        description="DuckDB SQL expression/formula to compute the attribute value from mapped_columns. E.g., 'ApplicantIncome + CoapplicantIncome' or 'log.annual.inc * 12'. Null if 1-to-1 or not applicable."
    )
    not_enough_information: bool = Field(
        description="Flag set to True if there is not enough information in the dataset columns to map to this ontology attribute"
    )
    explanation: Optional[str] = Field(
        description="Detailed explanation justifying the mapping or why there is not enough information"
    )

class ClassMapping(BaseModel):
    class_name: str = Field(description="The local name of the ontology class")
    class_uri: str = Field(description="The full URI of the ontology class")
    source_ttl_file: str = Field(description="The source TTL file containing the class definition")
    attribute_mappings: List[AttributeMapping] = Field(
        description="List of attribute-level mappings for this class"
    )
```

---

## 4. DuckDB Database Schema

To store the results and facilitate checkpoints, we use a single DuckDB database with two tables:

### Table `processed_classes`
Maintains a checkpoint of completed classes to support resuming.
- `class_uri` VARCHAR (PRIMARY KEY)
- `class_name` VARCHAR
- `processed_at` TIMESTAMP

### Table `attribute_mappings`
Stores individual mapped attributes.
- `class_name` VARCHAR
- `class_uri` VARCHAR
- `source_ttl_file` VARCHAR
- `attribute_name` VARCHAR
- `mapped_columns` VARCHAR (serialized as JSON list or comma-separated)
- `sql_formula` VARCHAR
- `not_enough_information` BOOLEAN
- `explanation` VARCHAR
- `created_at` TIMESTAMP

### Table `file_line_sequence`
Stores the generated start and end line ranges for chunking markdown files.
- `filename` VARCHAR
- `start_line_no` INTEGER
- `end_line_no` INTEGER

### Table `text_segments`
Stores logical segments extracted from markdown chunks.
- `segment_id` VARCHAR (PRIMARY KEY)
- `segment_text` VARCHAR
- `filename` VARCHAR
- `segment_start_line_no_global` INTEGER
- `segment_end_line_no_global` INTEGER

### Table `concepts`
Stores reconciled unique concepts and their attributes.
- `concept_type` VARCHAR
- `rationale` VARCHAR
- `concept_name` VARCHAR (PRIMARY KEY)
- `attributes_and_values` VARCHAR (JSON string)
- `summary` VARCHAR

### Table `concept_segment_mapping`
Maps segments to their reconciled concepts.
- `segment_id` VARCHAR
- `concept_name` VARCHAR

---

## 5. File Structure Under `app/`

The codebase is organized modularly under `app/`:

```
app/
├── __init__.py
├── cli.py             # CLI commands (create-data-mapping, generate-report, llm-wiki)
├── config.py          # Environment configuration & ADK initialization
├── ontology.py        # RDFLib recursive TTL parser
├── dataset.py         # CSV column headers reader
├── db.py              # DuckDB reader & writer
├── agent.py           # Google ADK agent setup & mapping execution
├── wiki.py            # PDF conversion, segmentation, classification, reconciliation logic
└── prompt/
    └── mapping_prompt.txt # Externalized prompt template
```
---

## 6. Detailed Module Specifications

### `app/config.py`
- Responsible for loading environment variables and configuring the `google.adk` model settings.
- Initializes a `google.adk.models.lite_llm.LiteLlm` model wrapper configured with the custom OpenAI-compatible endpoint.
- Short function: `get_llm_model()` returning the configured `LiteLlm` model instance, or defaults to the model string `"gemini-3.5-flash"`.

### `app/ontology.py`
- Recursively searches `ontology_folder` to find the directory named `LOAN` (case-insensitive).
- Uses `rdflib.Graph` to parse all `.ttl` files recursively under the `LOAN` folder.
- Extracts `owl:Class` resources and searches for properties related to those classes (e.g. properties appearing in class restrictions, domain definitions, or properties defined in the same namespace).

### `app/dataset.py`
- Finds all `.csv` files inside `dataset_folder`.
- Reads the first row of each CSV to extract column headers and maps columns to their respective source file.

### `app/db.py`
- Manages connection and transactions for DuckDB.
- Handles thread-safe writes: since multiple threads return results to the main thread, the main thread writes sequentially.
- Implements `get_processed_classes(db_path)` and `write_mappings(db_path, mapping_result)`.
- Implements `get_all_mappings(db_path)` to query and structure all saved mappings hierarchically with details (mapped columns, SQL formula, explanation).

### `app/agent.py`
- Reads the external prompt from `app/prompt/mapping_prompt.txt`.
- Sets up the `LlmAgent` with the configured model wrapper and `output_schema` (Pydantic model).
- Runs the agent using `InMemoryRunner` in a unique session per worker task.
- Submits structured mapping requests to the endpoint, parsing and cleaning the structured JSON output from the model's events (`event.content.parts`), and manually calling `validate_schema` to ensure type-safe structured dictionary outputs.
- Sanitizes structured outputs, overriding the source TTL file name with the actual path.

### `app/cli.py`
- Implements Click command: `create-data-mapping`.
- Implements Click command: `generate-report` to generate or regenerate the detailed mapping YAML from an existing DuckDB database.
- Implements Click command: `llm-wiki` to process, segment, classify, and reconcile PDF content to ontology schemas.
- Coordinates CLI arguments for database reading, path relativization, and writing the final detailed YAML report format.
- Implements the multi-threaded orchestration loop via `ThreadPoolExecutor` and uses checkpoints to resume execution.
- Extracts all database mappings at the end of the workflow, relativizes the TTL file paths, and dumps them to the YAML target path.

### `app/wiki.py`
- Recursively scans input directories for PDF files.
- Uses `markitdown` to convert PDFs into markdown files stored in the output directory.
- Chunks markdown files into line ranges with a 10% overlap, storing ranges in `file_line_sequence`.
- Performs LLM-based logical segmentation on chunks, calculating global line boundaries and storing segments in `text_segments`.
- Classifies segments to exactly one ontology concept type using structured `SegmentClassification` schema.
- Dedupes/reconciles new candidates against existing database concepts, performing semantic fuzzy matching and merging attributes/summaries using structured `ConceptMatchDecision`.
- Normalizes concept names according to lowercase/punctuation stripping rules and suffix resolution.
- Validates that all mapped concept references exist.
- Computes and writes a detailed coverage report showing concepts by type and percentage line coverage per file.

---

## 7. FastAPI Backend & Agentic Search API Under `api/`

The API backend is organized modularly under `api/`:

```
api/
├── main.py        # FastAPI app & endpoint setup
├── utils.py       # Summarization (condense_summary) & BM25 ranking (reranking)
├── tools.py       # Retrieval agent tools (doc_context_retrieval & tabular_data_retrieval)
└── workflow.py    # Core workflow orchestrator (Steps 1-6)
```

### `api/main.py`
- Exposes `POST /run` as a Server-Sent Events (SSE) streaming endpoint that yields real-time progress details (planning steps, sub-queries with results, analysis chunks, and final reports).
- Serves a premium dashboard testing UI directly at `GET /` and hosts static assets (CSS, HTML, JS) under the `/static` mount.
- Triggers loading of tabular CSV data into DuckDB tables dynamically before starting the search.

### `api/utils.py`
- Implements self-contained BM25 text document selection & reranking.
- Implements recursive chunk summarization using Google ADK LLM Agents to adhere to model context boundaries.

### `api/tools.py`
- Implements document context retrieval agent using query planning, parallel database fetching and selection (using a thread pool of size 5 to execute sub-queries in parallel), segment joins, and summary generation.
- Implements tabular data retrieval agent utilizing query planning, OLAP data cube definition, text generation, and parallelized self-correcting DuckDB SQL generation and execution (using a thread pool of size 5 to execute sub-queries in parallel), saving result tables to temp CSVs, and executing analytical syntheses.
- Enforces strict query results row count limits (default 100 rows) by instructing the LLM to output capped SQL queries with sorting, and programmatically verifies the presence of `LIMIT` using regex, appending `LIMIT {row_limit}` if omitted.

### `api/workflow.py`
- Co-ordinates the complete 6 steps: Planning, Execution, Analysis, Evaluation, Conditional Branch looping (until confidence >= 90 or iterations count reached), and writing of markdown analysis reports.
- Implemented as an asynchronous generator that yields structured progress updates, planning details, query outcomes, analysis tokens, evaluation outcomes, and report outputs.

---

## 8. ADK Skills & MCP Integration (Migration)

We have migrated the prompts of the retrieval and workflow orchestrator agents to ADK Skills and integrated a local DuckDB MCP server.

### File Structure for Custom Customizations & Skills
```
skills/
├── duckdb-skill/
│   └── SKILL.md       # Generic local DuckDB querying instructions
├── doc-retrieval-skill/
│   └── SKILL.md       # Target guidelines for concept-based document context retrieval
├── tabular-retrieval-skill/
│   └── SKILL.md       # SQL query generation, data cube definitions, and data repairing rules
└── workflow-skill/
    └── SKILL.md       # Workflow planning, comprehensive analysis, and QA audit rules

mcp/
└── duckdb_server.py   # FastMCP DuckDB server exposing table discovery & SQL operations
```

### Components Configured with Skills and MCP Toolset:
- **FastMCP Server (`mcp/duckdb_server.py`)**: Runs locally via stdio connection using `sys.executable`. Provides thread-safe isolated DB query executions for DuckDB operations: `list_tables`, `describe_table`, `run_sql`, `execute_sql`, `select_table`, `insert_table`, `update_table`.
- **Retrieval Tools (`api/tools.py`)**:
  - `execute_doc_sub_query`: Spawns a dedicated local DuckDB MCP server and loads `duckdb-skill` + `doc-retrieval-skill`.
  - `doc_context_retrieval`: Planner and analyst agents load `duckdb-skill` + `doc-retrieval-skill` with the MCP server.
  - `execute_tabular_sub_query`: Instantiates a single MCP server connection per thread task, loading `duckdb-skill` + `tabular-retrieval-skill`.
  - `tabular_data_retrieval`: Planner and analyst agents load `duckdb-skill` + `tabular-retrieval-skill` with the MCP server.
- **Workflow Core Orchestrator (`api/workflow.py`)**:
  - `run_agentic_workflow`: Planner, analyst, and evaluator agents load the `workflow-skill` at function startup.

---

## 9. Security Specifications

### 9.1 Bearer Token Authentication
To prevent unauthorized network or process interaction with the local DuckDB MCP server, it requires bearer token authentication:
- **Server Authentication**: Initialized with `AuthSettings` and `StaticTokenVerifier` (matching the `MCP_BEARER_TOKEN` environment variable). The FastMCP server validates `Authorization: Bearer <token>` in headers automatically when run in HTTP/SSE transport modes.
- **Process Authentication**: The server loads the environment configuration at startup. If `MCP_BEARER_TOKEN` is missing or empty, execution fails immediately to prevent unauthenticated access.
- **Client Configuration & Ephemeral Tokens**: To avoid plain-text storage of static keys on disk, retrieval agents in [api/tools.py](file:///Users/kahingleung/Downloads/agentic-insight/api/tools.py) generate a cryptographically secure random token (`EPHEMERAL_MCP_BEARER_TOKEN = secrets.token_hex(32)`) at startup and pass it to the spawned stdio child subprocesses via environment variables.

### 9.2 SQL Injection Check & Prevention
After SQL generation and before database query execution, a dedicated validation check `check_sql_injection` is executed:
- **Comments Removal**: Single-line (`--`) and multi-line (`/* ... */`) comments are stripped to prevent comment-based logic modification.
- **DML/DDL Restrictions**: Queries are parsed using `duckdb.extract_statements`. Only a single query of statement type `SELECT` is permitted. Stacked queries (using `;`) and modifying commands (`DROP`, `DELETE`, `UPDATE`, `INSERT`, etc.) are blocked.
- **Command/Function Sanity Checking**: DuckDB file-reading functions (`read_csv`, `read_parquet`, `read_json`, `read_ndjson`, `read_blob`, `read_text`, `parquet_scan`, `scan_parquet`, `glob`, `getenv`, `system`, `query_directory`, `write_csv`) and system catalog schemas (`information_schema`, `sqlite_master`, `duckdb_`, `pg_`) are rejected.
- **File/URL References Restriction**: Quoted string literals representing files (ending with `.csv`, `.parquet`, etc.) or network protocols (`http://`, `https://`, `s3://`) are forbidden.
- **Self-fixing Loop Integration**: If validation fails, it throws a `ValueError` which is captured by the query repair loop, logging the security infraction to `sql_error.log` and prompting the agent to correct the SQL statement in subsequent attempts.

### 9.3 Engine-Level Sandboxing & Read-Only Access
The MCP database connection is hardened directly at the engine level in [mcp/duckdb_server.py](file:///Users/kahingleung/Downloads/agentic-insight/mcp/duckdb_server.py):
- **DuckDB Sandboxing**: The database engine is locked immediately on connection by executing:
  ```python
  c.execute("SET enable_external_access = false;")
  c.execute("SET lock_configuration = true;")
  ```
  This prevents the engine from loading extensions, reading/writing local files, or initiating network requests from raw SQL, even if query checks are bypassed.
- **Read-Only Mode**: The MCP server respects `DB_READ_ONLY` from the environment. Retrieval tools pass `DB_READ_ONLY=true` to spawn the server in a read-only state, preventing any database mutation.



