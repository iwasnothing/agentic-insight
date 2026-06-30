# Changelog

All notable changes to this project will be documented in this file.

## [2026-06-29] - Add Mermaid Diagram Conversion in Markdown to PDF Converter

### Changed
- Modified [md_to_pdf.py](file:///Users/kahingleung/Downloads/agentic-insight/scripts/md_to_pdf.py) to parse Mermaid code blocks, request rendered PNG images from the `mermaid.ink` API, embed the images in the generated PDF, and clean up temporary files in a `finally` block.

### Added
- Added `test_convert_markdown_to_pdf_with_mermaid` in [test_md_to_pdf.py](file:///Users/kahingleung/Downloads/agentic-insight/tests/test_md_to_pdf.py) to verify parsing, API rendering, PDF embedding, and temporary image cleanup.

## [2026-06-29] - Update Article to Clarify Structured Ingestion and Explain CLI Steps

### Changed
- Modified [article.md](file:///Users/kahingleung/Downloads/agentic-insight/article.md) to replace traditional vector-search RAG references with structured document ingestion and semantic column mapping description.
- Detailed the roles of the two CLI steps: `create-data-mapping` (parsing ontologies via RDFLib, extracting CSV headers, and mapping columns using thread-pooled ADK agents) and `llm-wiki` (converting PDFs using `markitdown`, sliding-window chunking, performing LLM-based logical topic segmentation/classification, and reconciling concepts into DuckDB).
- Shortened overall article text to be strictly under 2,000 words (1,853 words).

## [2026-06-29] - Add Markdown to PDF Converter Script

### Added
- Created standalone Markdown to PDF conversion script [md_to_pdf.py](file:///Users/kahingleung/Downloads/agentic-insight/scripts/md_to_pdf.py).
- Created comprehensive unit tests [test_md_to_pdf.py](file:///Users/kahingleung/Downloads/agentic-insight/tests/test_md_to_pdf.py) verifying conversion correctness, output path determination, custom CSS integration, and PDF content.
- Added `markdown-pdf` dependency in [pyproject.toml](file:///Users/kahingleung/Downloads/agentic-insight/pyproject.toml).

## [2026-06-29] - Add Problem Statement Details and Why Agents Section

### Changed
- Updated `article.md` to include explicit subsections for **Problem Statement & Why It Matters** and **Why Agents are Uniquely Suited** to align with track requirements.

## [2026-06-29] - Restructure Article to Final Project Writeup

### Changed
- Restructured `article.md` to conform to the final project report structure:
  - Explicitly selected the **Agents for Business** track.
  - Added the **Project Development Journey** section documenting structural setup, ADK migration, `llm-wiki` pipeline creation, API harness design, and prompt constraint tuning.
  - Re-organized into Title, Subtitle, Problem Statement, Solution, and Architecture sections.
  - Ensured word count is strictly under 2,500 words (~2,080 words).

## [2026-06-29] - Add Introductory Article and Documentation

### Added
- Created `article.md` introducing the architecture and self-correcting agentic harness loop of the project, highlighting structured/unstructured hybrid data analysis and detailing the lending audit run in `report.md`.
- Supplemented `article.md` with a detailed explanation of the role of OWL Turtle ontologies and semantic mapping YAML files in translating domain concepts to database structures.

## [2026-06-29] - Restrict Planner Queries and Enforce SQL Generate Limit

### Changed
- Refined planning step prompts in `api/workflow.py` and `api/tools.py` (`doc_context_retrieval` and `tabular_data_retrieval`) to explicitly request the LLM to generate specific queries targeting clear parameters, rather than requesting all data in general.
- Updated `LlmAgent` instruction and prompts for SQL generation in `api/tools.py` to strictly enforce capping results to a maximum of 100 rows, not exceeding under any circumstances (using `LIMIT 100`).
- Appended specific rules to the SQL generation agent prompt:
  - Must use the table and columns in the DuckDB table schema ONLY, not to use other table or columns.
  - Must follow DuckDB SQL syntax.
  - Must use LIMIT to limit the row count with proper sorting (ORDER BY) to get top-n rows for analysis.
- Hardened SQL generation agent instructions and prompt templates in `api/tools.py` using generalized rules from `sql_error.log`:
  - Double-quote all dotted column names (e.g. `credit.policy`, `inq.last.6mths`, etc.) to prevent syntax/binder errors.
  - Require all non-aggregated fields referenced in aggregate queries (SELECT/ORDER BY/WHERE) to be listed in the GROUP BY clause.
  - Strictly forbid assumptions of columns not in the schema (e.g. `risk_category`, `underwriter_notes`).
  - Instruct the agent to output clean SQL only, without trailing braces/brackets like `}`, `]`, or `}]`.

### Added
- Added test cases `test_prompt_constraints` and `test_tabular_and_workflow_prompt_constraints` in `tests/test_api.py` to verify the modified planning and SQL generation prompt constraints.

## [2026-06-29] - Resolve SQL Generation Errors & Exclude loan_prediction_train

### Added
- Created `clean_and_filter_mapping` in `api/utils.py` to filter out `loan_prediction_train` tables and columns from loaded semantic mappings.
- Created schema-aware `sanitize_semantic_mapping` and `sanitize_semantic_mapping_file` in `api/utils.py` to verify and filter semantic mapping elements (mapped columns, SQL formulas) against the actual DuckDB schema.
- Added unit tests `test_clean_and_filter_mapping`, `test_get_duckdb_table_schemas_excludes_loan_prediction_train`, and `test_sanitize_semantic_mapping` in `tests/test_api.py`.
- Added logging of all failed dynamic SQL execution errors (query, timestamp, error, attempt number) to `sql_error.log`.

### Changed
- Excluded the `loan_prediction_train` table from DuckDB schemas in `api/tools.py`.
- Filtered semantic mapping context loaded in `api/tools.py` and `api/workflow.py` to prevent LLM planner/generator from using `loan_prediction_train` attributes.
- Added strict instructions and rules to SQL generator LLM configuration and prompts in `api/tools.py` to prevent querying `loan_prediction_train`, joining columns with incompatible data types, or querying columns not in the schema.
- Added rules to planning prompts in `api/workflow.py` to strictly forbid querying or referencing `loan_prediction_train`.
- Integrated semantic mapping file sanitization at the beginning of the agentic workflow (`run_agentic_workflow`) in `api/workflow.py`.

### Fixed
- Fixed recursive dictionary traversal in `sanitize_semantic_mapping` (in `api/utils.py`) to correctly handle multi-layered nested structures (e.g. `file_path -> class -> attributes`) in `mapping.yaml`.
- Added rules and instructions in `api/tools.py` enforcing double-quotes on dotted column names (e.g. `"log.annual.inc"`, `"int.rate"`) to prevent DuckDB binder errors.
- Fixed `test_sanitize_semantic_mapping` in `tests/test_api.py` to cover dotted column naming rules and recursive sanitization structures.
- Fixed missing import of standard library `re` module in `api/tools.py` which caused a NameError when matching candidate names.



## [2026-06-28] - Add Agentic Search FastAPI Backend


### Added
- Created `api/` directory implementing the agentic search FastAPI backend.
- Exposed the `/run` endpoint in `api/main.py` which executes the multi-step audit loop.
- Implemented recursive summarization `condense_summary` and self-contained BM25 document `reranking` in `api/utils.py`.
- Developed `doc_context_retrieval` and `tabular_data_retrieval` tools under `api/tools.py` using Google ADK LLM agents.
- Orchestrated the planning, execution, analysis, evaluation, conditional branch loop, and report writing in `api/workflow.py`.
- Wrote unit tests in `tests/test_api.py` covering all new functionality, and verified with pytest.

### Changed
- Added `fastapi` and `uvicorn` dependencies to `pyproject.toml`.

## [2026-06-27] - Add DuckDB Schema Inspector script

### Added
- Created `scripts/list_db.py`, a helper script that connects to a local DuckDB file path provided via command line arguments and prints the schema of non-internal tables (columns, types, constraints, and indexes).
- Created a unit test `tests/test_list_db.py` to verify the DB catalog inspector functions correctly against a temporary DuckDB database.

## [2026-06-25] - Add llm-wiki command and PDF mapping workflow

### Added
- Implemented recursive scanning of ontology folders for TTL file classes (Step 3).
- Added `--ontology-folder` / `--ontology-ttl` option aliases in `app/cli.py`.
- Added `"markitdown"` dependency to `pyproject.toml` to support PDF-to-Markdown conversion.
- Created `app/wiki.py` containing the core logic of the `llm-wiki` command, including:
  - PDF discovery and `markitdown` conversion.
  - Chunking with 10% overlap and storing ranges in `file_line_sequence` table.
  - LLM-based text segmentation and global line boundary mapping into `text_segments`.
  - LLM-based concept classification and attribute extraction using `SegmentClassification`.
  - Semantic reconciliation/fuzzy-matching and attributes/summary merging using `ConceptMatchDecision` in `concepts`.
  - Concept normalization rules and unique suffix naming resolution.
  - Verification check ensuring all segment mappings resolve to existing concepts.
  - Coverage report generation documenting concepts by type, multiple source concepts, and line coverage percentages per file.
- Registered the `llm-wiki` command under `app/cli.py`.
- Created unit and integration tests in `tests/test_wiki.py` verifying all core functions, mock execution of the complete pipeline, and CLI execution.
- Created `tests/conftest.py` to correctly resolve the project root directory in PYTHONPATH for tests.

## [2026-06-25] - Add generate-report command and detailed YAML mapping

### Added
- Implemented `generate-report` Click command in `app/cli.py` to extract mappings from DuckDB, filter them, calculate CSV coverage, and write to YAML.
- Implemented `test_cli_generate_report` in `tests/test_workflow.py` to verify the new CLI command with filtering and coverage.

### Changed
- Updated `get_all_mappings` in `app/db.py` to retrieve `sql_formula`, `explanation`, and `not_enough_information` from the database.
- Refactored YAML serialization in `app/cli.py` into a shared helper function `write_yaml_mapping`, which:
  - Excludes any attributes where `mapped_columns` is empty or `not_enough_information` is True.
  - Prunes classes and TTL files with no remaining mapped attributes.
  - Calculates the coverage percentage of dataset columns successfully mapped.
  - Prepends the coverage statistic as a comment header in the YAML output.
- Configured both `create-data-mapping` and `generate-report` commands to output the detailed and filtered YAML structure with coverage headers.
- Updated assertions in `test_get_all_mappings` and `test_cli_yaml_output` in `tests/test_workflow.py` to match the detailed format.

## [2026-06-25] - Fix custom LLM provider initialization

### Fixed
- Added `custom_llm_provider="openai"` keyword argument to `LiteLlm` initialization in `app/config.py` to prevent LiteLLM from raising `BadRequestError` (LLM Provider NOT provided) when resolving custom OpenAI-compatible endpoint models like `/models/Qwen/...`.
- Updated unit test `test_config_custom_endpoint` in `tests/test_workflow.py` to assert that `custom_llm_provider` is correctly configured on the model.

## [2026-06-25] - Fix structured output extraction

### Fixed
- Fixed issue where ADK runner-copy logic clears `event.output` (setting it to `None`) for non-partial events when `message_as_output` is enabled.
- Implemented manual text extraction from model events, removing thinking blocks (`thought`), cleaning potential markdown code blocks, and manually validating against `ClassMapping` schema using ADK's `validate_schema` helper.
- Removed temporary print debugging statements from `app/agent.py`.

### Removed
- Completely removed obsolete `google-antigravity` dependency from `pyproject.toml` and uninstalled it from the Python virtual environment.
- Added `google-adk[extensions]>=2.3.0` dependency to `pyproject.toml` to declare requirements explicitly.

## [2026-06-24] - Migration to google.adk LlmAgent and LiteLlm

### Added
- Integrated `google-adk[extensions]` dependency to support `LlmAgent` and `LiteLlm` execution.
- Added new unit test `test_run_mapping_agent_mocked` in `tests/test_workflow.py` verifying model execution using mock LiteLLM responses.

### Changed
- Refactored `app/config.py` to remove `get_agent_config` and implement `get_llm_model`, which instantiates `LiteLlm` for custom endpoints or returns `"gemini-3.5-flash"` for default runs.
- Refactored `app/agent.py` to use `google.adk.agents.LlmAgent` and `google.adk.runners.InMemoryRunner` within a per-task session context to execute the mapping queries.
- Updated `tests/test_workflow.py` configuration tests to assert against `get_llm_model` return attributes.
- Updated `AGENTS.md` specifications for `app/config.py` and `app/agent.py` to document the new ADK migration.

## [2026-06-24] - Environment Variable Setup
### Added
- Created `.env` file containing the environment variables for custom LLM URL and model ID.
- Added `.env` to `.gitignore` to prevent tracking environment files.
- Verified custom environment configuration loading via python-dotenv.

## [2026-06-24] - YAML Output Mapping Support

### Added
- Integrated `pyyaml` library into the workspace (`pyproject.toml` and `.venv`).
- Modified `app/ontology.py` to retain full absolute paths for Turtle ontology files in parsed class metadata.
- Modified `app/agent.py` to force-inject the correct source ontology file path into ClassMapping results, preventing LLM hallucinations.
- Implemented `get_all_mappings` function in `app/db.py` to retrieve and format all database mappings hierarchically.
- Updated click options in `app/cli.py` to support `--output-yaml` (defaulting to `mapping.yaml`) and serialize mappings to YAML with relativized paths at the end of the run.
- Added comprehensive unit and integration tests in `tests/test_workflow.py`.

## [2026-06-24]
### Added
- Downloaded the official EDM Council FIBO (Financial Industry Business Ontology) files.
- Created `ontology/` directory and moved all FIBO files inside it:
  - `ontology/AboutFIBOProd.ttl` (Turtle entry point for Production release)
  - `ontology/AboutFIBOProd.rdf` (RDF/XML entry point for Production release)
  - `ontology/AboutFIBODev.ttl` (Turtle entry point for Development release)
  - `ontology/AboutFIBODev.rdf` (RDF/XML entry point for Development release)
  - `ontology/prod.ttl.zip` (Complete Production release in Turtle serialization)
  - `ontology/prod.rdf.zip` (Complete Production release in RDF/XML serialization)
  - `ontology/dev.ttl.zip` (Complete Development release in Turtle serialization)
  - `ontology/dev.rdf.zip` (Complete Development release in RDF/XML serialization)
- Downloaded public Kaggle loan datasets matching FIBO concepts into `dataset/` folder:
  - `dataset/loan_prediction_train.csv` (Loan Prediction dataset including Loan Amount, Term, Credit History, Loan Status)
  - `dataset/loan_data.csv` (LendingClub dataset including FICO score, Interest Rate, Installment, DTI ratio, Purpose, etc.)
- Created a Python virtual environment (`.venv`) for the project.
- Created a `.gitignore` file to ignore `.venv/` and typical temporary python/system files.

## [2026-06-24] - Continued
### Added
- Created blueprint for Google ADK Agent Workflow Engine in `AGENTS.md`.
- Configured project dependencies inside `pyproject.toml`.
- Implemented configuration module `app/config.py` with custom OpenAI-compatible endpoint settings for the ADK agent.
- Implemented recursive Turtle parser module `app/ontology.py` using RDFLib to scan and parse LOAN ontologies.
- Implemented dataset parser module `app/dataset.py` to extract headers from all CSV files.
- Implemented storage manager module `app/db.py` to handle DuckDB schema, transaction writing, and resume checkpoints.
- Implemented agent execution module `app/agent.py` defining response schemas and thread-safe ADK calls.
- Implemented externalized prompt template `app/prompt/mapping_prompt.txt`.
- Implemented CLI runner module `app/cli.py` exposing the Click `create-data-mapping` command.
- Created comprehensive test suite in `tests/test_workflow.py` and verified using pytest.
- Performed verification via end-to-end simulated integration script.

### Fixed
- Fixed missing `URIRef` import and conditional expression check in `app/ontology.py` (L83) that was causing static analysis issues.
- Added a unit test `test_parse_ontology_classes` in `tests/test_workflow.py` to verify class parsing and blank node exclusion.

