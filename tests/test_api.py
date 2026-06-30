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
from api.utils import bm25_rank, reranking, condense_summary

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
    mock_run_workflow.return_value = {
        "status": "success",
        "iterations": 2,
        "confidence_score": 95,
        "explanation": "Perfect match",
        "report_path": "./report.md",
        "analysis": "Analysis content"
    }
    
    payload = {
        "objective": "Verify compliance in loan prediction",
        "dataset_folder": "./dataset",
        "ontology_folder": "./ontology",
        "doc_db_path": "./wiki.db",
        "mappings_db_path": "./mappings.db"
    }
    
    response = client.post("/run", json=payload)
    assert response.status_code == 200
    json_data = response.json()
    assert json_data["status"] == "success"
    assert json_data["confidence_score"] == 95
    assert json_data["iterations"] == 2
    
    mock_load_csv.assert_called_once()
    mock_run_workflow.assert_called_once()

@mock.patch("api.main.load_csv_files_to_duckdb")
@mock.patch("api.main.run_agentic_workflow", side_effect=ValueError("Workflow error"))
def test_api_run_endpoint_failure(mock_run_workflow, mock_load_csv):
    """Verify API POST /run returns 500 when workflow execution encounters an exception."""
    payload = {
        "objective": "Auditing"
    }
    response = client.post("/run", json=payload)
    assert response.status_code == 500
    assert "Workflow error" in response.json()["detail"]


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
                 assert "maximum of 100 rows, not exceeding 100" in prompt_text or "LIMIT 100" in prompt_text
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



