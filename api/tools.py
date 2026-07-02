"""
api/tools.py - Implementation of document and tabular context retrieval tools using Google ADK LLM Agents.

This module provides:
1. doc_context_retrieval: query decomposition, concept searching with BM25 reranking, text segment retrieval, and LLM-based analysis.
2. tabular_data_retrieval: query plan generation, data cube definitions, robust DuckDB SQL generation/fixing, CSV export, and analysis.
"""

import os
import re
import csv
import logging
import uuid
import yaml
import duckdb
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

import sys
from pathlib import Path
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.genai import types

from app.config import get_llm_model
from app.ontology import find_loan_folder, get_ttl_files, parse_ontology_classes
from api.utils import condense_summary, reranking, clean_and_filter_mapping, check_sql_injection

import secrets

logger = logging.getLogger(__name__)

# Ephemeral bearer token generated once on startup for local subprocess execution
EPHEMERAL_MCP_BEARER_TOKEN = secrets.token_hex(32)

def get_mcp_connection_params(db_path: str, read_only: bool = True) -> StdioConnectionParams:
    """
    Constructs StdioConnectionParams for spawning the DuckDB MCP server.
    Configures database path, read-only mode, and passes the ephemeral MCP bearer token.
    """
    project_root = Path(__file__).parent.parent
    env = {
        **os.environ,
        "DB_PATH": db_path,
        "DB_READ_ONLY": "true" if read_only else "false",
        "MCP_BEARER_TOKEN": EPHEMERAL_MCP_BEARER_TOKEN
    }
    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[str(project_root / "mcp" / "duckdb_server.py")],
            env=env
        )
    )

# --- Pydantic schemas for doc_context_retrieval ---

class SubQuery(BaseModel):
    concept_type: str = Field(description="The name of the ontology concept type (e.g. Loan, Collateral, etc.)")
    sub_query_string: str = Field(description="The search string to search for this concept type")

class DocRetrievalPlan(BaseModel):
    sub_queries: List[SubQuery] = Field(description="Decomposed sub-queries for document search")

class SelectedConcepts(BaseModel):
    concept_names: List[str] = Field(description="List of exact concept names that are related to the query string")

# --- Pydantic schemas for tabular_data_retrieval ---

class SQLPlanItem(BaseModel):
    description: str = Field(description="A brief description of what this sub-query retrieves")
    justification: str = Field(description="Justification of why this sub-query is necessary to answer the user query")

class TabularQueryPlan(BaseModel):
    sub_queries: List[SQLPlanItem] = Field(description="List of SQL sub-queries in the plan")

class DataCubeDefinition(BaseModel):
    measure: str = Field(description="The measure of the data cube (e.g. SUM(loan_amount), COUNT(*))")
    dimensions: List[str] = Field(description="The dimensions / grouping keys (e.g. loan_status, credit_history)")
    filtering_conditions: str = Field(description="Filtering conditions (e.g. loan_amount > 100000)")
    sort_order: str = Field(description="Ordering columns and direction")
    aggregation_functions: List[str] = Field(description="Aggregation function names used (e.g. SUM, COUNT, AVG)")

class GeneratedSQL(BaseModel):
    sql_query: str = Field(description="The generated valid DuckDB SQL query string")


# --- Helper Functions ---

def get_duckdb_table_schemas(db_path: str) -> str:
    """
    Fetches the schema of user/data tables in DuckDB to supply to LLM for SQL generation.
    """
    logger.debug(f"Fetching table schemas from DuckDB: {db_path}")
    conn = duckdb.connect(db_path)
    try:
        tables = conn.execute("""
            SELECT table_name, column_name, data_type 
            FROM information_schema.columns 
            WHERE table_schema = 'main' 
              AND table_name NOT IN ('processed_classes', 'attribute_mappings', 'file_line_sequence', 'text_segments', 'concepts', 'concept_segment_mapping', 'loan_prediction_train')
            ORDER BY table_name, ordinal_position
        """).fetchall()
        
        schema_dict = {}
        for t_name, col_name, d_type in tables:
            if t_name not in schema_dict:
                schema_dict[t_name] = []
            schema_dict[t_name].append(f"{col_name} ({d_type})")
            
        schema_str = ""
        for t_name, cols in schema_dict.items():
            schema_str += f"Table: {t_name}\nColumns:\n"
            for col in cols:
                schema_str += f"  - {col}\n"
            schema_str += "\n"
        return schema_str
    except Exception as e:
        logger.error(f"Error fetching table schemas: {e}")
        return ""
    finally:
        conn.close()

def list_concept_types(ontology_folder: str) -> str:
    """
    Finds and parses all ontology classes recursively under ontology LOAN folder.
    """
    logger.info(f"Listing concept types from ontology folder: {ontology_folder}")
    try:
        loan_folder = find_loan_folder(ontology_folder)
        ttl_files = get_ttl_files(loan_folder)
        classes = parse_ontology_classes(ttl_files)
        
        concept_types_text = ""
        for c in classes:
            concept_types_text += f"- Concept Type: {c['class_name']}\n  Description: {c.get('definition') or c.get('label') or ''}\n"
        return concept_types_text
    except Exception as e:
        logger.error(f"Error listing concept types: {e}")
        raise e


# --- Doc Context Retrieval Function ---

async def execute_doc_sub_query(
    sq: Dict[str, Any],
    doc_db_path: str,
    top_n: int,
    model: Any
) -> Optional[str]:
    """
    Executes a single document sub-query, querying and selecting concepts, and returning the YAML result.
    """
    concept_type = sq.get("concept_type")
    sub_query_string = sq.get("sub_query_string")
    logger.info(f"Processing sub-query: type='{concept_type}', query='{sub_query_string}'")

    # Query duckdb
    conn = duckdb.connect(doc_db_path)
    try:
        rows = conn.execute(
            "SELECT concept_name, summary FROM concepts WHERE LOWER(concept_type) = LOWER(?)",
            [concept_type]
        ).fetchall()
    except Exception as db_err:
        logger.error(f"Error querying concepts: {db_err}")
        rows = []
    finally:
        conn.close()

    if not rows:
        logger.info(f"No concepts found in database for concept_type '{concept_type}'")
        return None

    # Check if list is longer than top_n (default 100)
    list_of_strings = [f"Name: {name} | Summary: {summary}" for name, summary in rows]
    concept_map = {name: (name, summary) for name, summary in rows}

    if len(list_of_strings) > top_n:
        logger.info(f"Concepts count ({len(list_of_strings)}) > {top_n}, reranking...")
        reranked = reranking(sub_query_string, list_of_strings, top_n)
        # Reconstruct elements
        selected_candidates = []
        for item in reranked:
            # Extract name
            match = re.match(r"Name:\s*([^|]+)\|", item)
            if match:
                name_extracted = match.group(1).strip()
                if name_extracted in concept_map:
                    selected_candidates.append(concept_map[name_extracted])
    else:
        selected_candidates = rows

    # Feed candidates to LLM to select all related concepts
    candidate_list_text = "\n".join([f"- Name: {name}\n  Summary: {summary}" for name, summary in selected_candidates])
    
    selection_prompt = f"""You are a search query evaluator. Your task is to select ALL concepts from the candidates list below that are related to the query string.
Emphasis: Get ALL related concepts to maximize recall.

Query String:
{sub_query_string}

Candidates List:
{candidate_list_text}
"""
    project_root = Path(__file__).parent.parent
    duckdb_skill = load_skill_from_dir(project_root / "skills" / "duckdb-skill")
    doc_skill = load_skill_from_dir(project_root / "skills" / "doc-retrieval-skill")
    mcp_params = get_mcp_connection_params(doc_db_path, read_only=True)
    duckdb_mcp = McpToolset(
        connection_params=mcp_params,
        tool_filter=["list_tables", "describe_table", "run_sql", "execute_sql", "select_table", "insert_table", "update_table"],
    )
    skillset = skill_toolset.SkillToolset(
        skills=[duckdb_skill, doc_skill],
        additional_tools=[duckdb_mcp],
    )
    
    agent = LlmAgent(
        model=model,
        name="concept_searcher",
        instruction="Use the doc retrieval skill and DuckDB MCP to evaluate concepts.",
        output_schema=SelectedConcepts,
        tools=[skillset, duckdb_mcp]
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)
    
    new_message = types.Content(parts=[types.Part(text=selection_prompt)])
    events = []
    try:
        async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
            events.append(event)
            if event.error_message:
                raise ValueError(f"Concept selection failed: {event.error_message}")
    finally:
        await duckdb_mcp.close()

    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    if not model_text.strip() and events and events[-1].output:
        sel_obj = events[-1].output
        sel_dict = sel_obj.model_dump()
    else:
        from app.agent import clean_json_text
        cleaned_json = clean_json_text(model_text)
        from google.adk.utils._schema_utils import validate_schema
        sel_dict = validate_schema(SelectedConcepts, cleaned_json)

    selected_names = sel_dict.get("concept_names", [])
    logger.info(f"Selected {len(selected_names)} related concepts.")

    if not selected_names:
        return None

    # Get source segment text for each selected concept
    conn = duckdb.connect(doc_db_path)
    try:
        placeholders = ",".join(["?"] * len(selected_names))
        query = f"""
            SELECT m.concept_name, s.segment_text 
            FROM main.concept_segment_mapping m 
            JOIN main.text_segments s ON m.segment_id = s.segment_id 
            WHERE m.concept_name IN ({placeholders})
        """
        segments = conn.execute(query, selected_names).fetchall()
    except Exception as db_err:
        logger.error(f"Error querying concept segments: {db_err}")
        segments = []
    finally:
        conn.close()

    # Format output in yaml
    concept_to_segments = {}
    for c_name, seg_text in segments:
        if c_name not in concept_to_segments:
            concept_to_segments[c_name] = []
        concept_to_segments[c_name].append(seg_text)

    yaml_list = []
    for c_name, segs in concept_to_segments.items():
        yaml_list.append({
            "concept_name": c_name,
            "segment_text": "\n---\n".join(segs)
        })
    
    if yaml_list:
        return yaml.safe_dump(yaml_list, sort_keys=False, default_flow_style=False)
    return None


async def doc_context_retrieval(
    query_string: str,
    ontology_folder: str = None,
    doc_db_path: str = None,
    top_n: int = None,
    model: Any = None,
    pool_size: int = 5
) -> str:
    """
    Executes doc context retrieval agentic workflow.
    Decomposes the query string, queries concepts, selects related concepts, retrieves segment text,
    condenses, and generates answer in Markdown format.
    """
    if ontology_folder is None:
        ontology_folder = os.getenv("ONTOLOGY_FOLDER", "./ontology")
    if doc_db_path is None:
        doc_db_path = os.getenv("DOC_DB_PATH", "./wiki.db")
    if top_n is None:
        top_n = int(os.getenv("TOP_N", "100"))
    if model is None:
        model = get_llm_model()

    logger.info(f"Running doc_context_retrieval for query: '{query_string}'")

    # Step 1: Planning Step (Decompose query using concept types context)
    concept_types_text = list_concept_types(ontology_folder)
    
    planning_prompt = f"""You are an expert document search planner.
Decompose the query string into a list of sub-queries. Each sub-query will match a specific concept type from the ontology schema and define a sub-query search string.
Ensure sub-query search strings are specific and target a precise objective; DO NOT write general search strings that query all concepts/data.

Available Ontology Concept Types:
{concept_types_text}

Query String:
{query_string}
"""
    project_root = Path(__file__).parent.parent
    duckdb_skill = load_skill_from_dir(project_root / "skills" / "duckdb-skill")
    doc_skill = load_skill_from_dir(project_root / "skills" / "doc-retrieval-skill")
    mcp_params = get_mcp_connection_params(doc_db_path, read_only=True)
    duckdb_mcp = McpToolset(
        connection_params=mcp_params,
        tool_filter=["list_tables", "describe_table", "run_sql", "execute_sql", "select_table", "insert_table", "update_table"],
    )
    skillset = skill_toolset.SkillToolset(
        skills=[duckdb_skill, doc_skill],
        additional_tools=[duckdb_mcp],
    )
    
    agent = LlmAgent(
        model=model,
        name="doc_search_planner",
        instruction="Use the doc retrieval skill and DuckDB MCP to design document search sub-queries.",
        output_schema=DocRetrievalPlan,
        tools=[skillset, duckdb_mcp]
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)
    
    new_message = types.Content(parts=[types.Part(text=planning_prompt)])
    events = []
    try:
        async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
            events.append(event)
            if event.error_message:
                raise ValueError(f"Doc planning failed: {event.error_message}")
    finally:
        await duckdb_mcp.close()

    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    if not model_text.strip() and events and events[-1].output:
        plan_obj = events[-1].output
        plan_dict = plan_obj.model_dump()
    else:
        from app.agent import clean_json_text
        cleaned_json = clean_json_text(model_text)
        from google.adk.utils._schema_utils import validate_schema
        plan_dict = validate_schema(DocRetrievalPlan, cleaned_json)

    sub_queries = plan_dict.get("sub_queries", [])
    logger.info(f"Doc Retrieval Plan generated: {len(sub_queries)} sub-queries.")

    # Step 2: Execution step (execute sub-queries in parallel using ThreadPoolExecutor)
    combined_yaml_results = []
    
    if sub_queries:
        def run_in_thread(sq):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(
                    execute_doc_sub_query(sq, doc_db_path, top_n, model)
                )
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()

        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [executor.submit(run_in_thread, sq) for sq in sub_queries]
            for fut in futures:
                res = fut.result()
                if res:
                    combined_yaml_results.append(res)

    # Step 3: Combine and condense
    if not combined_yaml_results:
        return "No document context found for the query."

    combined_text = "\n\n".join(combined_yaml_results)
    
    # Condense summary recursively
    condensed_text = await condense_summary(combined_text, model=model)

    # Step 4: Query analysis
    analysis_prompt = f"""You are given the following unstructured document context (concepts and source segment texts):

{condensed_text}

Using the context above, analyze and answer the search query. Provide your answer and explanation in markdown format.

Query:
{query_string}
"""
    project_root = Path(__file__).parent.parent
    duckdb_skill = load_skill_from_dir(project_root / "skills" / "duckdb-skill")
    doc_skill = load_skill_from_dir(project_root / "skills" / "doc-retrieval-skill")
    mcp_params = get_mcp_connection_params(doc_db_path, read_only=True)
    duckdb_mcp = McpToolset(
        connection_params=mcp_params,
        tool_filter=["list_tables", "describe_table", "run_sql", "execute_sql", "select_table", "insert_table", "update_table"],
    )
    skillset = skill_toolset.SkillToolset(
        skills=[duckdb_skill, doc_skill],
        additional_tools=[duckdb_mcp],
    )
    
    agent = LlmAgent(
        model=model,
        name="doc_query_analyst",
        instruction="Use the doc retrieval skill and DuckDB MCP to analyze concept documentation.",
        tools=[skillset, duckdb_mcp]
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)
    
    new_message = types.Content(parts=[types.Part(text=analysis_prompt)])
    events = []
    try:
        async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
            events.append(event)
            if event.error_message:
                raise ValueError(f"Doc query analysis failed: {event.error_message}")
    finally:
        await duckdb_mcp.close()

    analysis_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            analysis_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    return analysis_text


# --- Tabular Data Retrieval Function ---

async def execute_tabular_sub_query(
    idx: int,
    sq: Dict[str, Any],
    semantic_mapping: str,
    table_schemas: str,
    db_path: str,
    row_limit: int,
    max_sql_iterations: int,
    model: Any
) -> Optional[str]:
    """
    Executes a single tabular sub-query: defines a data cube, generates SQL,
    runs the SQL (with repair loop), and writes the result to a temp CSV file.
    """
    sq_description = sq.get("description", "")
    logger.info(f"Executing sub-query {idx+1}: '{sq_description}'")

    project_root = Path(__file__).parent.parent
    duckdb_skill = load_skill_from_dir(project_root / "skills" / "duckdb-skill")
    tabular_skill = load_skill_from_dir(project_root / "skills" / "tabular-retrieval-skill")
    mcp_params = get_mcp_connection_params(db_path, read_only=True)
    duckdb_mcp = McpToolset(
        connection_params=mcp_params,
        tool_filter=["list_tables", "describe_table", "run_sql", "execute_sql", "select_table", "insert_table", "update_table"],
    )
    skillset = skill_toolset.SkillToolset(
        skills=[duckdb_skill, tabular_skill],
        additional_tools=[duckdb_mcp],
    )

    try:
        # 3a. Define Data Cube
        cube_prompt = f"""You are a database modeler. Define a Data Cube definition for the sub-query below.
The Data Cube must detail: measures, dimensions, filtering conditions, sort order, and aggregation functions.

Sub-query:
{sq_description}

Semantic Mapping Context:
{semantic_mapping}

DuckDB Database Schemas:
{table_schemas}
"""
        agent_cube = LlmAgent(
            model=model,
            name=f"cube_definer_{idx}",
            instruction="Use the tabular retrieval skill and DuckDB MCP to define OLAP data cubes.",
            output_schema=DataCubeDefinition,
            tools=[skillset, duckdb_mcp]
        )
        runner_cube = InMemoryRunner(agent=agent_cube)
        cube_sess = f"session_cube_{uuid.uuid4().hex}"
        await runner_cube.session_service.create_session(app_name=runner_cube.app_name, user_id="user", session_id=cube_sess)
        
        new_message = types.Content(parts=[types.Part(text=cube_prompt)])
        cube_events = []
        async for event in runner_cube.run_async(user_id="user", session_id=cube_sess, new_message=new_message):
            cube_events.append(event)
            if event.error_message:
                raise ValueError(f"Data cube definition failed: {event.error_message}")

        cube_model_text = ""
        for event in reversed(cube_events):
            if event.content and event.content.role == 'model' and event.content.parts:
                cube_model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                break

        if not cube_model_text.strip() and cube_events and cube_events[-1].output:
            cube_obj = cube_events[-1].output
            cube_dict = cube_obj.model_dump()
        else:
            from app.agent import clean_json_text
            cleaned_json = clean_json_text(cube_model_text)
            from google.adk.utils._schema_utils import validate_schema
            cube_dict = validate_schema(DataCubeDefinition, cleaned_json)

        logger.info(f"Data Cube defined for sub-query {idx+1}: measure='{cube_dict.get('measure')}'")

        # 3b. Generate SQL & Execute with self-fixing loop
        sql_success = False
        current_sql = ""
        last_error = ""

        # Create one SQL generator agent and session for this sub-query's repair loop
        agent_sql = LlmAgent(
            model=model,
            name=f"sql_generator_{idx}",
            instruction=(
                "Use the tabular retrieval skill and DuckDB MCP to write and repair SQL statements.\n"
                "CRITICAL RULES:\n"
                "1. NEVER use, query, or reference the table 'loan_prediction_train' or its columns (such as Loan_ID, Gender, Married, Dependents, Education, Self_Employed, ApplicantIncome, CoapplicantIncome, LoanAmount, Loan_Amount_Term, Credit_History, Property_Area, Loan_Status).\n"
                "2. ONLY use the table 'loan_data' and its columns that are explicitly listed in the provided DuckDB Database Schemas. Do not assume or hallucinate columns (such as risk_category, insurance_status, guarantee_status, underwriter_notes).\n"
                "3. DO NOT compare a VARCHAR column (like text/string IDs) with numeric columns (like DOUBLE, BIGINT) in JOIN conditions or WHERE clauses. Ensure all compared/joined fields have identical data types to avoid conversion errors.\n"
                "4. All columns and tables queried MUST exist in the provided DuckDB Database Schemas. Do not assume or hallucinate table columns.\n"
                "5. Any column name containing dots (e.g. log.annual.inc, int.rate, credit.policy, days.with.cr.line, revol.bal, revol.util, inq.last.6mths, delinq.2yrs, pub.rec, not.fully.paid) MUST always be enclosed in double quotes in the SQL query (e.g. \"log.annual.inc\", \"int.rate\", \"credit.policy\"). Dot-notation without quotes causes syntax errors (e.g. near '.6', '.2') or table binder errors (e.g., 'Referenced table credit not found').\n"
                "6. SQL aliases / AS names must not contain dots unless they are double-quoted. Preferably use underscores (e.g. `AVG_log_annual_inc` instead of `AVG_log.annual.inc`).\n"
                "7. For queries containing aggregate functions (e.g. SUM, AVG, COUNT, MIN, MAX), every non-aggregated column referenced in SELECT, ORDER BY, or WHERE clauses must appear in the GROUP BY clause. Ensure no GROUP BY binder errors occur (e.g., 'column must appear in the GROUP BY clause').\n"
                f"8. ALWAYS generate SQL statements that cap the result to a maximum of {row_limit} rows, not exceeding {row_limit}. Apply a LIMIT clause (e.g., `LIMIT {row_limit}`) to every query. Use proper sorting order (using an ORDER BY clause) to ensure the most relevant top {row_limit} rows are retrieved.\n"
                "9. Output strictly the SQL query string only. Do not wrap the SQL query in JSON structure trailing characters like `}`, `]`, or `}]` inside the `sql_query` field.\n"
                "10. MUST use the table and columns in the DuckDB table schema ONLY. DO NOT use other tables or columns.\n"
                "11. MUST follow DuckDB SQL syntax.\n"
                f"12. MUST use LIMIT to limit the row count with proper sorting (using an ORDER BY clause) so that we can retrieve the top-n rows for analysis."
            ),
            output_schema=GeneratedSQL,
            tools=[skillset, duckdb_mcp]
        )
        runner_sql = InMemoryRunner(agent=agent_sql)
        sql_sess = f"session_sql_{uuid.uuid4().hex}"
        await runner_sql.session_service.create_session(app_name=runner_sql.app_name, user_id="user", session_id=sql_sess)

        for loop_idx in range(max_sql_iterations):
            if loop_idx == 0:
                sql_prompt = f"""You are a DuckDB SQL developer. Generate a valid DuckDB SQL statement to query the tables according to the following Data Cube definition, Semantic Mapping, and schemas.
Ensure the query is capped to a maximum of {row_limit} rows, not exceeding {row_limit} under any circumstances. You must explicitly append a LIMIT clause (e.g., LIMIT {row_limit} or less) with proper sorting order (using an ORDER BY clause).

CRITICAL RULES:
- Do NOT reference or query the table 'loan_prediction_train'.
- Only query table 'loan_data' and use the columns explicitly listed in the schemas below. Do not assume or guess column names like risk_category, insurance_status, guarantee_status, underwriter_notes.
- MUST use the table and columns in the DuckDB table schema ONLY. DO NOT use other tables or columns.
- MUST follow DuckDB SQL syntax.
- Avoid join type conversion errors: DO NOT compare VARCHAR columns to numeric columns (BIGINT, DOUBLE, etc.) in JOINs or comparison expressions.
- Column naming rule: Any column name containing dots (e.g. log.annual.inc, int.rate, credit.policy, days.with.cr.line, revol.bal, revol.util, inq.last.6mths, delinq.2yrs, pub.rec, not.fully.paid) MUST always be double-quoted in the query (e.g. "log.annual.inc", "int.rate").
- Aliases naming rule: SQL aliases / AS names must not contain dots unless double-quoted. Use underscores (e.g., AVG_log_annual_inc) instead.
- Group By rule: If the query uses aggregates (SUM, AVG, COUNT, MIN, MAX), every non-aggregated column in the SELECT, ORDER BY, or WHERE clause MUST appear in the GROUP BY clause.
- Capping rule: The query result MUST NOT exceed {row_limit} rows. Add `LIMIT {row_limit}` (or lower if appropriate, but never higher) with proper sorting (using an ORDER BY clause) to your SQL query.
- MUST use LIMIT to limit the row count with proper sorting (using an ORDER BY clause) so that we can retrieve the top-n rows for analysis.
- Format rule: Output only the SQL query string in the response. Do not include trailing braces/brackets like `}}`, `]`, or `}}]` inside the SQL.

Data Cube:
{yaml.safe_dump(cube_dict)}

Semantic Mapping:
{semantic_mapping}

DuckDB Database Schemas:
{table_schemas}
"""
            else:
                sql_prompt = f"""The previous SQL statement failed with an error. Please FIX the SQL statement to resolve this error.
Crucial: You MUST still answer/retrieve data for the exact same Data Cube definition:
{yaml.safe_dump(cube_dict)}

Failed SQL:
{current_sql}

Error detail:
{last_error}

CRITICAL RULES:
- Do NOT reference or query the table 'loan_prediction_train'.
- Only query table 'loan_data' and use the columns explicitly listed in the schemas below. Do not assume or guess column names like risk_category, insurance_status, guarantee_status, underwriter_notes.
- MUST use the table and columns in the DuckDB table schema ONLY. DO NOT use other tables or columns.
- MUST follow DuckDB SQL syntax.
- Avoid join type conversion errors: DO NOT compare VARCHAR columns to numeric columns (BIGINT, DOUBLE, etc.) in JOINs or comparison expressions.
- Column naming rule: Any column name containing dots (e.g. log.annual.inc, int.rate, credit.policy, days.with.cr.line, revol.bal, revol.util, inq.last.6mths, delinq.2yrs, pub.rec, not.fully.paid) MUST always be double-quoted in the query (e.g. "log.annual.inc", "int.rate").
- Aliases naming rule: SQL aliases / AS names must not contain dots unless double-quoted. Use underscores (e.g., AVG_log_annual_inc) instead.
- Group By rule: If the query uses aggregates (SUM, AVG, COUNT, MIN, MAX), every non-aggregated column in the SELECT, ORDER BY, or WHERE clause MUST appear in the GROUP BY clause.
- Capping rule: The query result MUST NOT exceed {row_limit} rows. Make sure to include `LIMIT {row_limit}` (or lower if appropriate) with proper sorting (using an ORDER BY clause) in your fixed SQL query.
- MUST use LIMIT to limit the row count with proper sorting (using an ORDER BY clause) so that we can retrieve the top-n rows for analysis.
- Format rule: Output only the SQL query string in the response. Do not include trailing braces/brackets like `}}`, `]`, or `}}]` inside the SQL.

To assist you in fixing the SQL query, here are the database schemas and mappings:
DuckDB Database Schemas:
{table_schemas}

Semantic Mapping:
{semantic_mapping}

Generate the corrected SQL query.
"""

            new_message = types.Content(parts=[types.Part(text=sql_prompt)])
            sql_events = []
            async for event in runner_sql.run_async(user_id="user", session_id=sql_sess, new_message=new_message):
                sql_events.append(event)
                if event.error_message:
                    raise ValueError(f"SQL generation failed: {event.error_message}")

            sql_model_text = ""
            for event in reversed(sql_events):
                if event.content and event.content.role == 'model' and event.content.parts:
                    sql_model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                    break

            if not sql_model_text.strip() and sql_events and sql_events[-1].output:
                sql_obj = sql_events[-1].output
                sql_dict = sql_obj.model_dump()
            else:
                from app.agent import clean_json_text
                cleaned_json = clean_json_text(sql_model_text)
                from google.adk.utils._schema_utils import validate_schema
                sql_dict = validate_schema(GeneratedSQL, cleaned_json)

            current_sql = sql_dict.get("sql_query", "")
            logger.info(f"Generated SQL for sub-query {idx+1} (attempt {loop_idx+1}): {current_sql}")

            # Run SQL on DuckDB
            conn = duckdb.connect(db_path)
            try:
                # Validate the generated SQL to check and prevent SQL injection
                check_sql_injection(current_sql)
                
                # Ensure the SQL has LIMIT
                if not re.search(r"\blimit\b", current_sql, re.IGNORECASE):
                    # Strip trailing semicolon if any
                    current_sql_stripped = current_sql.strip().rstrip(";")
                    current_sql = f"{current_sql_stripped} LIMIT {row_limit}"
                
                result = conn.execute(current_sql)
                cols = [desc[0] for desc in result.description]
                rows = result.fetchall()
                
                # Write to temp CSV
                temp_filename = f"./temp/result_{uuid.uuid4().hex[:8]}.csv"
                with open(temp_filename, "w", newline="", encoding="utf-8") as csv_file:
                    writer = csv.writer(csv_file)
                    writer.writerow(cols)
                    writer.writerows(rows)
                
                logger.info(f"SQL execution successful for sub-query {idx+1}! Saved {len(rows)} rows to {temp_filename}")
                sql_success = True
                return temp_filename
            except Exception as sql_err:
                last_error = str(sql_err)
                logger.warning(f"SQL execution failed for sub-query {idx+1}: {last_error}")
                try:
                    import datetime
                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open("sql_error.log", "a", encoding="utf-8") as err_log:
                        err_log.write(f"[{timestamp}] SQL Execution Failed for sub-query {idx+1} (attempt {loop_idx+1}):\n")
                        err_log.write(f"Query: {current_sql}\n")
                        err_log.write(f"Error: {last_error}\n")
                        err_log.write("-" * 80 + "\n")
                except Exception as log_err:
                    logger.error(f"Failed to write to sql_error.log: {log_err}")
            finally:
                conn.close()

        if not sql_success:
            logger.error(f"Failed to execute sub-query {idx+1} SQL after {max_sql_iterations} attempts.")
            return None
    finally:
        await duckdb_mcp.close()


async def tabular_data_retrieval(
    query_string: str,
    semantic_mapping_path: str = None,
    db_path: str = None,
    row_limit: int = None,
    max_sql_iterations: int = None,
    model: Any = None,
    pool_size: int = 5
) -> str:
    """
    Executes tabular data retrieval agentic workflow.
    Loads semantic mapping, decomposes query, defines data cubes, generates and fixes SQL,
    runs the SQL, exports results to temp CSV, and performs LLM analysis.
    """
    if semantic_mapping_path is None:
        semantic_mapping_path = os.getenv("SEMANTIC_MAPPING_PATH", "./mapping.yaml")
        if not os.path.exists(semantic_mapping_path):
            # Fallback to alternative naming
            if os.path.exists("./sematic_mapping.yaml"):
                semantic_mapping_path = "./sematic_mapping.yaml"
    if db_path is None:
        db_path = os.getenv("DOC_DB_PATH", "./mappings.db")
    if row_limit is None:
        row_limit = int(os.getenv("ROW_LIMIT", "100"))
    if max_sql_iterations is None:
        max_sql_iterations = int(os.getenv("MAX_SQL_ITERATIONS", "5"))
    if model is None:
        model = get_llm_model()

    logger.info(f"Running tabular_data_retrieval for query: '{query_string}'")

    # Step 1: get_semantic_mapping
    try:
        with open(semantic_mapping_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        
        filtered_data = clean_and_filter_mapping(data)
        semantic_mapping = yaml.safe_dump(filtered_data, sort_keys=False, default_flow_style=False)
    except Exception as e:
        logger.error(f"Error reading semantic mapping file from {semantic_mapping_path}: {e}")
        try:
            with open(semantic_mapping_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            filtered_data = clean_and_filter_mapping(data)
            semantic_mapping = yaml.safe_dump(filtered_data, sort_keys=False, default_flow_style=False)
        except Exception:
            semantic_mapping = ""

    # Fetch DuckDB table schemas to make planning and SQL generation reliable
    table_schemas = get_duckdb_table_schemas(db_path)

    # Step 2: Planning Step (Decompose query into SQL query plan)
    planning_prompt = f"""You are a database architect. Decompose the query string into a query plan consisting of a list of sub-queries that can be executed by SQL queries.
Each sub-query should return less than {row_limit} rows.

CRITICAL RULE: DO NOT plan sub-queries that select all data or query the entire dataset in general. Each sub-query must be highly specific, targeting a particular metric, subset, or objective with clear parameters.

Semantic Mapping Context:
{semantic_mapping}

DuckDB Database Schemas:
{table_schemas}

Query:
{query_string}
"""
    project_root = Path(__file__).parent.parent
    duckdb_skill = load_skill_from_dir(project_root / "skills" / "duckdb-skill")
    tabular_skill = load_skill_from_dir(project_root / "skills" / "tabular-retrieval-skill")
    mcp_params = get_mcp_connection_params(db_path, read_only=True)
    duckdb_mcp = McpToolset(
        connection_params=mcp_params,
        tool_filter=["list_tables", "describe_table", "run_sql", "execute_sql", "select_table", "insert_table", "update_table"],
    )
    skillset = skill_toolset.SkillToolset(
        skills=[duckdb_skill, tabular_skill],
        additional_tools=[duckdb_mcp],
    )
    
    agent = LlmAgent(
        model=model,
        name="tabular_query_planner",
        instruction="Use the tabular retrieval skill and DuckDB MCP to plan SQL sub-queries.",
        output_schema=TabularQueryPlan,
        tools=[skillset, duckdb_mcp]
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)
    
    new_message = types.Content(parts=[types.Part(text=planning_prompt)])
    events = []
    try:
        async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
            events.append(event)
            if event.error_message:
                raise ValueError(f"Tabular planning failed: {event.error_message}")
    finally:
        await duckdb_mcp.close()

    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    if not model_text.strip() and events and events[-1].output:
        plan_obj = events[-1].output
        plan_dict = plan_obj.model_dump()
    else:
        from app.agent import clean_json_text
        cleaned_json = clean_json_text(model_text)
        from google.adk.utils._schema_utils import validate_schema
        plan_dict = validate_schema(TabularQueryPlan, cleaned_json)

    sub_queries = plan_dict.get("sub_queries", [])
    logger.info(f"Tabular SQL Plan generated: {len(sub_queries)} sub-queries.")

    # Create temporary directory for CSV exports
    os.makedirs("./temp", exist_ok=True)
    temp_csv_files = []

    # Step 3: Execution Phase (parallel execution using ThreadPoolExecutor)
    if sub_queries:
        def run_in_thread(idx, sq):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(
                    execute_tabular_sub_query(
                        idx,
                        sq,
                        semantic_mapping,
                        table_schemas,
                        db_path,
                        row_limit,
                        max_sql_iterations,
                        model
                    )
                )
            finally:
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
                loop.close()

        with ThreadPoolExecutor(max_workers=pool_size) as executor:
            futures = [executor.submit(run_in_thread, idx, sq) for idx, sq in enumerate(sub_queries)]
            for fut in futures:
                res = fut.result()
                if res:
                    temp_csv_files.append(res)

    # Step 4: Query Analysis (analyze csv data)
    if not temp_csv_files:
        return "No tabular data retrieved."

    # Load and format the CSV files
    csv_contents = []
    context_char_limit = int(os.getenv("CONTEXT_SIZE_LIMIT", "10000")) * 4  # Estimate chars
    current_chars = 0

    for temp_csv in temp_csv_files:
        if current_chars >= context_char_limit:
            break
        try:
            with open(temp_csv, "r", encoding="utf-8") as csv_file:
                reader = csv.reader(csv_file)
                header = next(reader, None)
                if not header:
                    continue
                rows = []
                for row in reader:
                    rows.append(row)
                    # Check size estimate
                    estimate = len(",".join(row))
                    if current_chars + estimate > context_char_limit:
                        logger.info("CSV content size limit reached, truncating rows.")
                        break
                    current_chars += estimate
                
                # Format csv representation
                csv_str = f"Filename: {os.path.basename(temp_csv)}\n"
                csv_str += ",".join(header) + "\n"
                for r in rows:
                    csv_str += ",".join(r) + "\n"
                csv_contents.append(csv_str)
        except Exception as e:
            logger.error(f"Error reading temp csv {temp_csv}: {e}")

    combined_csv_context = "\n\n".join(csv_contents)

    analysis_prompt = f"""You are a data analyst. Analyze the following retrieved tabular data to answer the query.
Provide the final answer and a detailed explanation in markdown format.

Retrieved CSV Data:
{combined_csv_context}

Query:
{query_string}
"""
    project_root = Path(__file__).parent.parent
    duckdb_skill = load_skill_from_dir(project_root / "skills" / "duckdb-skill")
    tabular_skill = load_skill_from_dir(project_root / "skills" / "tabular-retrieval-skill")
    mcp_params = get_mcp_connection_params(db_path, read_only=True)
    duckdb_mcp = McpToolset(
        connection_params=mcp_params,
        tool_filter=["list_tables", "describe_table", "run_sql", "execute_sql", "select_table", "insert_table", "update_table"],
    )
    skillset = skill_toolset.SkillToolset(
        skills=[duckdb_skill, tabular_skill],
        additional_tools=[duckdb_mcp],
    )
    
    agent_analysis = LlmAgent(
        model=model,
        name="tabular_query_analyst",
        instruction="Use the tabular retrieval skill and DuckDB MCP to analyze retrieved tabular data.",
        tools=[skillset, duckdb_mcp]
    )
    runner_analysis = InMemoryRunner(agent=agent_analysis)
    analysis_sess = f"session_analysis_{uuid.uuid4().hex}"
    await runner_analysis.session_service.create_session(app_name=runner_analysis.app_name, user_id="user", session_id=analysis_sess)
    
    new_message = types.Content(parts=[types.Part(text=analysis_prompt)])
    anal_events = []
    try:
        async for event in runner_analysis.run_async(user_id="user", session_id=analysis_sess, new_message=new_message):
            anal_events.append(event)
            if event.error_message:
                raise ValueError(f"Tabular analysis failed: {event.error_message}")
    finally:
        await duckdb_mcp.close()

    analysis_text = ""
    for event in reversed(anal_events):
        if event.content and event.content.role == 'model' and event.content.parts:
            analysis_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    # Clean up temp CSV files
    for temp_csv in temp_csv_files:
        try:
            os.remove(temp_csv)
        except Exception:
            pass

    return analysis_text
