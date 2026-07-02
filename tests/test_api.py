"""
tests/test_api.py - Unit test suite for the Agentic Search FastAPI backend.

This module contains test cases for utility functions, BM25 reranking, chunk summarization,
CSV to DuckDB loading, and the /run endpoint using pytest.
"""

import os
import tempfile
import pytest
from unittest import mock
from fastapi.testclient import TestClient

from api.main import app, load_csv_files_to_duckdb
from api.utils import bm25_rank, reranking, condense_summary, check_sql_injection

client = TestClient(app)

def test_bm25_rank():
    """Verify BM25 ranking correctly selects and sorts matching documents."""
    corpus = [
        "The loan prediction model trains on client income and credit history.",
        "A borrower application holds collateral and loan terms.",
        "Standard financial institutions operate risk evaluation on lending rates."
    ]
    results = bm25_rank("prediction model credit history", corpus, top_n=2)
    assert len(results) == 2
    # The first document has the highest term matches
    assert "prediction" in results[0] or "model" in results[0]

def test_reranking():
    """Verify reranking selects top-n elements when size threshold is exceeded."""
    list_of_strings = [f"Item {i} data payload string for testing" for i in range(150)]
    filtered = reranking("testing query", list_of_strings, top_n=100)
    assert len(filtered) == 100

@pytest.mark.anyio
async def test_condense_summary_short():
    """Verify condense_summary returns original context if line count is below threshold."""
    context = "A very short context with few lines."
    result = await condense_summary(context, lines_threshold=10)
    assert result == context

@pytest.mark.anyio
async def test_condense_summary_long():
    """Verify condense_summary splits content into chunks and summarizes them using LLM."""
    context = "\n".join([f"Line {i} content text" for i in range(600)])
    mock_model = mock.MagicMock()

    with mock.patch("api.utils.LlmAgent") as mock_agent_class, \
         mock.patch("api.utils.InMemoryRunner") as mock_runner_class:
         
         mock_runner = mock.AsyncMock()
         mock_runner_class.return_value = mock_runner
         
         mock_event = mock.MagicMock()
         mock_event.error_message = None
         mock_event.output = mock.MagicMock()
         mock_event.output.summary = "Mocked Chunk Summary"
         
         async def mock_run_async(*args, **kwargs):
             yield mock_event
             
         mock_runner.run_async = mock_run_async
         
         result = await condense_summary(context, model=mock_model, lines_threshold=500, context_size_limit=1000)
         assert "Mocked Chunk Summary" in result

def test_load_csv_files_to_duckdb():
    """Verify load_csv_files_to_duckdb successfully reads CSV files and registers tables in DuckDB."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = os.path.join(tmp_dir, "loan_test.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("loan_id,amount,status\nLP101,150000,Approved\nLP102,250000,Pending")
            
        db_path = os.path.join(tmp_dir, "test_mappings.db")
        load_csv_files_to_duckdb(tmp_dir, db_path)
        
        import duckdb
        conn = duckdb.connect(db_path)
        try:
            res = conn.execute("SELECT * FROM loan_test").fetchall()
            assert len(res) == 2
            assert res[0] == ("LP101", 150000, "Approved")
        finally:
            conn.close()

@mock.patch("api.main.run_agentic_workflow")
@mock.patch("api.main.load_csv_files_to_duckdb")
def test_api_run_endpoint_success(mock_load_csv, mock_run_workflow):
    """Verify API POST /run returns success response when execution succeeds."""
    async def mock_generator(*args, **kwargs):
        yield {"event": "start", "data": {"objective": "Verify compliance in loan prediction"}}
        yield {"event": "final_report", "data": {"report": "Analysis content", "result": {
            "status": "success",
            "iterations": 2,
            "confidence_score": 95,
            "explanation": "Perfect match",
            "report_path": "./report.md",
            "analysis": "Analysis content"
        }}}

    mock_run_workflow.side_effect = mock_generator
    
    payload = {
        "objective": "Verify compliance in loan prediction",
        "dataset_folder": "./dataset",
        "ontology_folder": "./ontology",
        "doc_db_path": "./wiki.db",
        "mappings_db_path": "./mappings.db"
    }
    
    response = client.post("/run", json=payload)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    
    lines = list(response.iter_lines())
    assert len(lines) > 0
    
    import json
    parsed_events = []
    for line in lines:
        if line.startswith("data: "):
            parsed_events.append(json.loads(line[6:]))
            
    assert len(parsed_events) == 2
    assert parsed_events[0]["event"] == "start"
    assert parsed_events[1]["event"] == "final_report"
    assert parsed_events[1]["data"]["result"]["confidence_score"] == 95
    
    mock_load_csv.assert_called_once()
    mock_run_workflow.assert_called_once()

@mock.patch("api.main.load_csv_files_to_duckdb")
@mock.patch("api.main.run_agentic_workflow")
def test_api_run_endpoint_failure(mock_run_workflow, mock_load_csv):
    """Verify API POST /run returns 200 with an error event when workflow encounters an exception during stream."""
    async def mock_generator_fail(*args, **kwargs):
        yield {"event": "start", "data": {"objective": "Auditing"}}
        raise ValueError("Workflow error")

    mock_run_workflow.side_effect = mock_generator_fail
    
    payload = {
        "objective": "Auditing"
    }
    response = client.post("/run", json=payload)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    
    lines = list(response.iter_lines())
    import json
    parsed_events = []
    for line in lines:
        if line.startswith("data: "):
            parsed_events.append(json.loads(line[6:]))
            
    assert len(parsed_events) == 2
    assert parsed_events[0]["event"] == "start"
    assert parsed_events[1]["event"] == "error"
    assert "Workflow error" in parsed_events[1]["data"]["detail"]


def test_clean_and_filter_mapping():
    """Verify clean_and_filter_mapping correctly strips loan_prediction_train reference columns."""
    from api.utils import clean_and_filter_mapping
    
    mock_mapping = {
        "CombinedLoanToValueRatio": {
            "attributes": {
                "appliesTo": {
                    "mapped_columns": ["Loan_ID", "credit.policy"],
                    "sql_formula": "Loan_ID || credit.policy",
                    "explanation": "Test explanation here"
                },
                "refersTo": {
                    "mapped_columns": ["Loan_ID"],
                    "sql_formula": "Loan_ID",
                    "explanation": "Another explanation"
                }
            }
        },
        "Loan": {
            "attributes": {
                "hasPrincipalAmount": {
                    "mapped_columns": ["LoanAmount", "installment"],
                    "sql_formula": "COALESCE(loan_data.installment, loan_prediction_train.LoanAmount)",
                    "explanation": "Both columns might represent amount"
                },
                "hasInterestRate": {
                    "mapped_columns": ["int.rate"],
                    "sql_formula": None,
                    "explanation": "Direct mapping"
                }
            }
        }
    }
    
    result = clean_and_filter_mapping(mock_mapping)
    
    # 1. Explanation keys should be stripped
    assert "explanation" not in result["CombinedLoanToValueRatio"]["attributes"]["appliesTo"]
    
    # 2. loan_prediction_train columns should be excluded
    # For CombinedLoanToValueRatio.appliesTo, Loan_ID is forbidden. Only 'credit.policy' remains.
    assert result["CombinedLoanToValueRatio"]["attributes"]["appliesTo"]["mapped_columns"] == ["credit.policy"]
    # The formula referenced Loan_ID, so it should be set to None.
    assert result["CombinedLoanToValueRatio"]["attributes"]["appliesTo"]["sql_formula"] is None
    
    # For refersTo, Loan_ID is forbidden, so it becomes empty and not_enough_information is True.
    assert result["CombinedLoanToValueRatio"]["attributes"]["refersTo"]["mapped_columns"] == []
    assert result["CombinedLoanToValueRatio"]["attributes"]["refersTo"]["not_enough_information"] is True
    
    # For Loan.hasPrincipalAmount, LoanAmount is forbidden. 'installment' is kept.
    assert result["Loan"]["attributes"]["hasPrincipalAmount"]["mapped_columns"] == ["installment"]
    # The formula referenced loan_prediction_train.LoanAmount, so it is set to None.
    assert result["Loan"]["attributes"]["hasPrincipalAmount"]["sql_formula"] is None
    
    # For Loan.hasInterestRate, int.rate is kept.
    assert result["Loan"]["attributes"]["hasInterestRate"]["mapped_columns"] == ["int.rate"]
    assert result["Loan"]["attributes"]["hasInterestRate"].get("not_enough_information", False) is not True


def test_get_duckdb_table_schemas_excludes_loan_prediction_train():
    """Verify get_duckdb_table_schemas returns schema info but excludes loan_prediction_train table."""
    from api.tools import get_duckdb_table_schemas
    import duckdb
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_schemas.db")
        conn = duckdb.connect(db_path)
        try:
            # Create a table that should be included
            conn.execute("CREATE TABLE loan_data (credit_policy BIGINT, int_rate DOUBLE)")
            # Create a table that should be excluded
            conn.execute("CREATE TABLE loan_prediction_train (loan_id VARCHAR, gender VARCHAR)")
        finally:
            conn.close()
            
        schemas = get_duckdb_table_schemas(db_path)
        
        # Verify loan_data is in schemas, but loan_prediction_train is NOT
        assert "Table: loan_data" in schemas
        assert "credit_policy (BIGINT)" in schemas
        assert "Table: loan_prediction_train" not in schemas


def test_sanitize_semantic_mapping():
    """Verify sanitize_semantic_mapping correctly checks mapping against the schema, filtering out nonexistent columns/tables."""
    from api.utils import sanitize_semantic_mapping
    import duckdb
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_sanitize.db")
        conn = duckdb.connect(db_path)
        try:
            # Create a table with specific columns, including a dotted column name
            conn.execute("CREATE TABLE loan_data (credit_policy BIGINT, int_rate DOUBLE, installment DOUBLE, \"log.annual.inc\" DOUBLE)")
        finally:
            conn.close()
            
        mock_mapping = {
            "RatioAnalysis": {
                "attributes": {
                    "appliesTo": {
                        "mapped_columns": ["nonexistent_col", "credit_policy"],
                        "sql_formula": "nonexistent_col + credit_policy",
                        "explanation": "Should be cleaned"
                    },
                    "ficoScore": {
                        "mapped_columns": ["fico"],
                        "sql_formula": "fico / 10",
                        "explanation": "No FICO score in schema"
                    },
                    "rateTerm": {
                        "mapped_columns": ["int_rate", "installment"],
                        "sql_formula": "int_rate * installment",
                        "explanation": "All valid"
                    },
                    "income": {
                        "mapped_columns": ["log.annual.inc"],
                        "sql_formula": "log.annual.inc * 12",
                        "explanation": "Valid dotted column and formula"
                    }
                }
            }
        }
        
        result = sanitize_semantic_mapping(mock_mapping, db_path)
        
        # 1. appliesTo: nonexistent_col is removed. only credit_policy remains.
        # The sql_formula is invalidated (set to None) because it referenced nonexistent_col.
        assert result["RatioAnalysis"]["attributes"]["appliesTo"]["mapped_columns"] == ["credit_policy"]
        assert result["RatioAnalysis"]["attributes"]["appliesTo"]["sql_formula"] is None
        
        # 2. ficoScore: FICO is nonexistent. mapped_columns becomes empty.
        # not_enough_information becomes True. sql_formula is set to None.
        assert result["RatioAnalysis"]["attributes"]["ficoScore"]["mapped_columns"] == []
        assert result["RatioAnalysis"]["attributes"]["ficoScore"]["not_enough_information"] is True
        assert result["RatioAnalysis"]["attributes"]["ficoScore"]["sql_formula"] is None
        
        # 3. rateTerm: both columns are valid. mapped_columns and sql_formula are unchanged.
        assert result["RatioAnalysis"]["attributes"]["rateTerm"]["mapped_columns"] == ["int_rate", "installment"]
        assert result["RatioAnalysis"]["attributes"]["rateTerm"]["sql_formula"] == "int_rate * installment"
        assert not result["RatioAnalysis"]["attributes"]["rateTerm"].get("not_enough_information", False)

        # 4. income: dotted column is correctly verified and left intact
        assert result["RatioAnalysis"]["attributes"]["income"]["mapped_columns"] == ["log.annual.inc"]
        assert result["RatioAnalysis"]["attributes"]["income"]["sql_formula"] == "log.annual.inc * 12"
        assert not result["RatioAnalysis"]["attributes"]["income"].get("not_enough_information", False)


@pytest.mark.anyio
async def test_prompt_constraints():
    """Verify that doc planning prompts enforce specific query objectives."""
    from api.tools import doc_context_retrieval
    from unittest import mock

    mock_model = mock.MagicMock()
    
    with mock.patch("api.tools.LlmAgent") as mock_agent_class, \
         mock.patch("api.tools.InMemoryRunner") as mock_runner_class, \
         mock.patch("api.tools.list_concept_types", return_value="Concept: Loan"):
        
        mock_runner = mock.AsyncMock()
        mock_runner_class.return_value = mock_runner
        
        mock_event = mock.MagicMock()
        mock_event.error_message = None
        mock_event.output = mock.MagicMock()
        mock_event.output.model_dump.return_value = {"sub_queries": []}
        
        prompt_verified = False

        async def mock_run_async(*args, **kwargs):
            nonlocal prompt_verified
            new_msg = kwargs.get("new_message") or args[2]
            prompt_text = new_msg.parts[0].text
            assert "DO NOT write general search strings" in prompt_text or "specific and target a precise objective" in prompt_text
            prompt_verified = True
            yield mock_event
            
        mock_runner.run_async = mock_run_async
        
        await doc_context_retrieval("test query", ontology_folder="/fake", doc_db_path="/fake.db", model=mock_model)
        assert prompt_verified


@pytest.mark.anyio
async def test_tabular_and_workflow_prompt_constraints():
    """Verify that tabular planning/generation prompts enforce constraints."""
    from api.tools import tabular_data_retrieval
    from unittest import mock

    mock_model = mock.MagicMock()

    with mock.patch("api.tools.LlmAgent") as mock_agent_class, \
         mock.patch("api.tools.InMemoryRunner") as mock_runner_class, \
         mock.patch("api.tools.open", mock.mock_open(read_data="CombinedLoanToValueRatio:\n  attributes: {}")), \
         mock.patch("api.tools.get_duckdb_table_schemas", return_value="Table: loan_data"):
         
         mock_runner = mock.AsyncMock()
         mock_runner_class.return_value = mock_runner
         
         mock_event_plan = mock.MagicMock()
         mock_event_plan.error_message = None
         mock_event_plan.output = mock.MagicMock()
         mock_event_plan.output.model_dump.return_value = {
             "sub_queries": [{"description": "Get high risk loans", "justification": "needed"}]
         }
         
         mock_event_cube = mock.MagicMock()
         mock_event_cube.error_message = None
         mock_event_cube.output = mock.MagicMock()
         mock_event_cube.output.model_dump.return_value = {
             "measure": "COUNT(*)", "dimensions": ["loan_status"], "filtering_conditions": "",
             "sort_order": "", "aggregation_functions": ["COUNT"]
         }

         mock_event_sql = mock.MagicMock()
         mock_event_sql.error_message = None
         mock_event_sql.output = mock.MagicMock()
         mock_event_sql.output.model_dump.return_value = {
             "sql_query": "SELECT * FROM loan_data LIMIT 100"
         }

         plan_verified = False
         sql_verified = False

         async def mock_run_async(*args, **kwargs):
             nonlocal plan_verified, sql_verified
             new_msg = kwargs.get("new_message") or args[2]
             prompt_text = new_msg.parts[0].text
             
             # Fetch the agent instance passed or initialized
             # LlmAgent signature is LlmAgent(model, name, instruction, output_schema)
             # The name argument is usually the 2nd positional or keyword arg
             call_args = mock_agent_class.call_args
             agent_name = call_args[1].get("name") or call_args[0][1]
             
             if "query_planner" in agent_name:
                 assert "DO NOT plan sub-queries that select all data" in prompt_text
                 plan_verified = True
                 yield mock_event_plan
             elif "cube_definer" in agent_name:
                 yield mock_event_cube
             elif "sql_generator" in agent_name:
                 assert "maximum of" in prompt_text or "LIMIT" in prompt_text
                 assert "Column naming rule" in prompt_text or "dots" in prompt_text
                 assert "Group By rule" in prompt_text or "aggregates" in prompt_text
                 assert "Format rule" in prompt_text or "trailing braces" in prompt_text
                 assert "MUST use the table and columns in the DuckDB table schema ONLY" in prompt_text
                 assert "MUST follow DuckDB SQL syntax" in prompt_text
                 assert "MUST use LIMIT to limit the row count with proper sorting" in prompt_text
                 sql_verified = True
                 yield mock_event_sql
             else:
                 yield mock_event_sql

         mock_runner.run_async = mock_run_async
         
         with mock.patch("api.tools.duckdb.connect") as mock_db_connect:
             mock_conn = mock.MagicMock()
             mock_db_connect.return_value = mock_conn
             mock_conn.execute.return_value.description = [("col",)]
             mock_conn.execute.return_value.fetchall.return_value = [("val",)]
             
             await tabular_data_retrieval("test query", semantic_mapping_path="/fake.yaml", db_path="/fake.db", model=mock_model)
             
         assert plan_verified
         assert sql_verified


@pytest.mark.anyio
async def test_sql_limit_fallback():
    """Verify that if generated SQL does not have a LIMIT clause, LIMIT row_limit is programmatically appended."""
    from api.tools import tabular_data_retrieval
    from unittest import mock

    mock_model = mock.MagicMock()

    with mock.patch("api.tools.LlmAgent") as mock_agent_class, \
         mock.patch("api.tools.InMemoryRunner") as mock_runner_class, \
         mock.patch("api.tools.open", mock.mock_open(read_data="CombinedLoanToValueRatio:\n  attributes: {}")), \
         mock.patch("api.tools.get_duckdb_table_schemas", return_value="Table: loan_data"):
         
         mock_runner = mock.AsyncMock()
         mock_runner_class.return_value = mock_runner
         
         mock_event_plan = mock.MagicMock()
         mock_event_plan.error_message = None
         mock_event_plan.output = mock.MagicMock()
         mock_event_plan.output.model_dump.return_value = {
             "sub_queries": [{"description": "Get high risk loans", "justification": "needed"}]
         }
         
         mock_event_cube = mock.MagicMock()
         mock_event_cube.error_message = None
         mock_event_cube.output = mock.MagicMock()
         mock_event_cube.output.model_dump.return_value = {
             "measure": "COUNT(*)", "dimensions": ["loan_status"], "filtering_conditions": "",
             "sort_order": "", "aggregation_functions": ["COUNT"]
         }

         # SQL without LIMIT clause
         mock_event_sql = mock.MagicMock()
         mock_event_sql.error_message = None
         mock_event_sql.output = mock.MagicMock()
         mock_event_sql.output.model_dump.return_value = {
             "sql_query": "SELECT * FROM loan_data"
         }

         async def mock_run_async(*args, **kwargs):
             call_args = mock_agent_class.call_args
             agent_name = call_args[1].get("name") or call_args[0][1]
             if "query_planner" in agent_name:
                 yield mock_event_plan
             elif "cube_definer" in agent_name:
                 yield mock_event_cube
             else:
                 yield mock_event_sql

         mock_runner.run_async = mock_run_async
         
         with mock.patch("api.tools.duckdb.connect") as mock_db_connect:
             mock_conn = mock.MagicMock()
             mock_db_connect.return_value = mock_conn
             mock_conn.execute.return_value.description = [("col",)]
             mock_conn.execute.return_value.fetchall.return_value = [("val",)]
             
             await tabular_data_retrieval(
                 "test query", 
                 semantic_mapping_path="/fake.yaml", 
                 db_path="/fake.db", 
                 row_limit=50, 
                 model=mock_model
             )
             
             # Verify that execute was called with LIMIT 50 appended
             called_sql = mock_conn.execute.call_args[0][0]
             assert "LIMIT 50" in called_sql


def test_litellm_logging_worker_patch():
    """Verify that LiteLLM's LoggingWorker methods are successfully patched to be no-ops."""
    from litellm.litellm_core_utils.logging_worker import LoggingWorker, GLOBAL_LOGGING_WORKER
    
    # Assert that starting, enqueuing or initializing the worker does not raise errors
    # and has no side effects (i.e. remains a no-op).
    assert LoggingWorker.start(GLOBAL_LOGGING_WORKER) is None
    assert LoggingWorker.enqueue(GLOBAL_LOGGING_WORKER, None) is None
    assert LoggingWorker.ensure_initialized_and_enqueue(GLOBAL_LOGGING_WORKER, None) is None
    assert LoggingWorker._handle_queue_full(GLOBAL_LOGGING_WORKER, None) is None
    
    # Check that GLOBAL_LOGGING_WORKER instance itself is patched too
    assert GLOBAL_LOGGING_WORKER.start() is None
    assert GLOBAL_LOGGING_WORKER.enqueue(None) is None
    assert GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue(None) is None


def test_check_sql_injection_success():
    """Verify that valid read-only SELECT queries pass the injection check."""
    valid_queries = [
        "SELECT * FROM loan_data",
        "SELECT col1, col2 FROM loan_data WHERE credit_policy = 1",
        "SELECT \"log.annual.inc\" FROM loan_data LIMIT 100",
        "SELECT purpose, AVG(installment) FROM loan_data GROUP BY purpose",
        "SELECT * FROM loan_data -- trailing comment is stripped",
        "SELECT * FROM loan_data /* multi-line comment */ WHERE fico > 700"
    ]
    for q in valid_queries:
        check_sql_injection(q)  # Should not raise any exception


def test_check_sql_injection_failures():
    """Verify that various injection techniques raise ValueError."""
    invalid_queries = [
        # Stacked query
        "SELECT * FROM loan_data; DROP TABLE loan_data",
        "SELECT 1; SELECT 2",
        # Modifying statement
        "INSERT INTO loan_data VALUES (1, 2)",
        "DROP TABLE loan_data",
        "DELETE FROM loan_data WHERE 1=1",
        "UPDATE loan_data SET int_rate = 0.05",
        "CREATE TABLE test (val INTEGER)",
        "ALTER TABLE loan_data ADD COLUMN new_col VARCHAR",
        # System catalog access
        "SELECT table_name FROM information_schema.tables",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM duckdb_tables",
        "SELECT * FROM pg_catalog.pg_tables",
        # Disallowed functions
        "SELECT * FROM read_csv('data.csv')",
        "SELECT read_parquet('data.parquet')",
        "SELECT getenv('SECRET')",
        "SELECT system('ls')",
        # File-like string literals
        "SELECT * FROM loan_data WHERE purpose = 'test.csv'",
        "SELECT * FROM loan_data WHERE purpose = 'https://malicious.com'",
        "SELECT * FROM loan_data WHERE purpose = 's3://bucket/data.parquet'",
        # Empty query
        "",
        "   ",
    ]
    for q in invalid_queries:
        with pytest.raises(ValueError) as excinfo:
            check_sql_injection(q)
        assert any(x in str(excinfo.value) for x in ["Disallowed", "Multiple", "failed", "empty", "No SQL"])


@pytest.mark.anyio
async def test_sql_injection_fixing_loop():
    """Verify that if the LLM generates a SQL query with potential injection, it is blocked and the loop attempts to fix it."""
    from api.tools import tabular_data_retrieval
    from unittest import mock

    mock_model = mock.MagicMock()

    with mock.patch("api.tools.LlmAgent") as mock_agent_class, \
         mock.patch("api.tools.InMemoryRunner") as mock_runner_class, \
         mock.patch("api.tools.open", mock.mock_open(read_data="CombinedLoanToValueRatio:\n  attributes: {}")), \
         mock.patch("api.tools.get_duckdb_table_schemas", return_value="Table: loan_data"):
         
         mock_runner = mock.AsyncMock()
         mock_runner_class.return_value = mock_runner

         mock_event_plan = mock.MagicMock()
         mock_event_plan.error_message = None
         mock_event_plan.output = mock.MagicMock()
         mock_event_plan.output.model_dump.return_value = {
             "sub_queries": [{"description": "Get high risk loans", "justification": "needed"}]
         }

         mock_event_cube = mock.MagicMock()
         mock_event_cube.error_message = None
         mock_event_cube.output = mock.MagicMock()
         mock_event_cube.output.model_dump.return_value = {
             "measure": "COUNT(*)", "dimensions": ["loan_status"], "filtering_conditions": "",
             "sort_order": "", "aggregation_functions": ["COUNT"]
         }

         # Mock two different SQL outputs: first is SQL injection, second is valid query
         mock_event_sql_1 = mock.MagicMock()
         mock_event_sql_1.error_message = None
         mock_event_sql_1.output = mock.MagicMock()
         mock_event_sql_1.output.model_dump.return_value = {
             "sql_query": "SELECT * FROM read_csv('passwords.csv')"
         }

         mock_event_sql_2 = mock.MagicMock()
         mock_event_sql_2.error_message = None
         mock_event_sql_2.output = mock.MagicMock()
         mock_event_sql_2.output.model_dump.return_value = {
             "sql_query": "SELECT * FROM loan_data"
         }

         # Use a list to return different SQL queries sequentially
         sql_events = [mock_event_sql_1, mock_event_sql_2]
         sql_call_count = 0

         async def mock_run_async(*args, **kwargs):
             nonlocal sql_call_count
             call_args = mock_agent_class.call_args
             agent_name = call_args[1].get("name") or call_args[0][1]
             if "query_planner" in agent_name:
                 yield mock_event_plan
             elif "cube_definer" in agent_name:
                 yield mock_event_cube
             else:
                 event = sql_events[min(sql_call_count, len(sql_events) - 1)]
                 sql_call_count += 1
                 yield event

         mock_runner.run_async = mock_run_async

         with mock.patch("api.tools.duckdb.connect") as mock_db_connect:
             mock_conn = mock.MagicMock()
             mock_db_connect.return_value = mock_conn
             mock_conn.execute.return_value.description = [("col",)]
             mock_conn.execute.return_value.fetchall.return_value = [("val",)]

             await tabular_data_retrieval(
                 "test query", 
                 semantic_mapping_path="/fake.yaml", 
                 db_path="/fake.db", 
                 row_limit=50, 
                 model=mock_model
             )
             
             # Verify that the SQL generation agent was called at least twice (initial attempt + fix attempt)
             assert sql_call_count >= 2
             
             # Verify that the final execution query was the safe query and had LIMIT appended
             called_sql = mock_conn.execute.call_args[0][0]
             assert "SELECT * FROM loan_data" in called_sql
             assert "LIMIT 50" in called_sql


