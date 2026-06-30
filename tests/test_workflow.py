"""
tests/test_workflow.py - Unit test suite for the Agent Workflow Engine.

This module contains test cases for config loader, ontology parser, dataset columns extractor,
and DuckDB schema initialization and checkpoints using pytest.
"""

import os
import tempfile
import pytest
import duckdb
from unittest import mock

from app.config import get_llm_model
from app.ontology import find_loan_folder, get_ttl_files, parse_ontology_classes
from app.dataset import get_all_csv_columns, format_columns_for_prompt
from app.db import init_db, get_processed_classes, save_class_mapping, get_all_mappings

def test_config_default():
    """Verify get_llm_model works and returns the default model identifier string."""
    with mock.patch.dict(os.environ, {}, clear=True):
        model = get_llm_model()
        assert model == "gemini-3.5-flash"

def test_config_custom_endpoint():
    """Verify get_llm_model handles custom OpenAI-compatible endpoint correctly."""
    custom_env = {
        "CUSTOM_LLM_URL": "https://custom-url.com/v1",
        "CUSTOM_LLM_MODEL": "my-custom-model",
        "CUSTOM_LLM_API_KEY": "my-secret-key"
    }
    with mock.patch.dict(os.environ, custom_env):
        model = get_llm_model()
        assert model is not None
        from google.adk.models.lite_llm import LiteLlm
        assert isinstance(model, LiteLlm)
        assert model.model == "my-custom-model"
        assert model._additional_args["api_base"] == "https://custom-url.com/v1"
        assert model._additional_args["api_key"] == "my-secret-key"
        assert model._additional_args["custom_llm_provider"] == "openai"

def test_find_loan_folder_success():
    """Verify find_loan_folder finds the LOAN subfolder (case-insensitive)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        loan_path = os.path.join(tmp_dir, "fibo", "ontology", "LOAN")
        os.makedirs(loan_path)
        found_path = find_loan_folder(tmp_dir)
        assert os.path.basename(found_path).upper() == "LOAN"

def test_find_loan_folder_direct_success():
    """Verify find_loan_folder returns the path directly when given the LOAN folder itself."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        loan_path = os.path.join(tmp_dir, "fibo", "ontology", "LOAN")
        os.makedirs(loan_path)
        found_path = find_loan_folder(loan_path)
        assert os.path.abspath(found_path) == os.path.abspath(loan_path)

def test_find_loan_folder_not_found():
    """Verify find_loan_folder raises FileNotFoundError when no LOAN folder exists."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        with pytest.raises(FileNotFoundError):
            find_loan_folder(tmp_dir)


def test_dataset_columns_extraction():
    """Verify get_all_csv_columns parses headers correctly from multiple files."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create dummy csv 1
        csv1 = os.path.join(tmp_dir, "test1.csv")
        with open(csv1, "w", encoding="utf-8") as f:
            f.write("col1,col2,col3\nvalue1,value2,value3")
            
        # Create dummy csv 2
        csv2 = os.path.join(tmp_dir, "test2.csv")
        with open(csv2, "w", encoding="utf-8") as f:
            f.write("id,name\n1,Alice")
            
        columns_map = get_all_csv_columns(tmp_dir)
        assert "test1.csv" in columns_map
        assert columns_map["test1.csv"] == ["col1", "col2", "col3"]
        assert "test2.csv" in columns_map
        assert columns_map["test2.csv"] == ["id", "name"]
        
        formatted = format_columns_for_prompt(columns_map)
        assert "Dataset: test1.csv" in formatted
        assert "- col1" in formatted
        assert "Dataset: test2.csv" in formatted
        assert "- name" in formatted

def test_db_init_and_checkpoint():
    """Verify DuckDB database initialization, checkpoints, and mappings transactions."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_mappings.duckdb")
        
        # Initialize
        init_db(db_path)
        
        # Checkpoint should be empty
        processed = get_processed_classes(db_path)
        assert len(processed) == 0
        
        # Mock class mapping data
        mapping = {
            "class_name": "TestClass",
            "class_uri": "https://spec.edmcouncil.org/test/TestClass",
            "source_ttl_file": "TestOntology.ttl",
            "attribute_mappings": [
                {
                    "attribute_name": "attr1",
                    "mapped_columns": ["col1", "col2"],
                    "sql_formula": "col1 + col2",
                    "not_enough_information": False,
                    "explanation": "Calculated by addition"
                }
            ]
        }
        
        # Save mapping
        save_class_mapping(db_path, mapping)
        
        # Checkpoint should now have the URI
        processed = get_processed_classes(db_path)
        assert len(processed) == 1
        assert "https://spec.edmcouncil.org/test/TestClass" in processed
        
        # Query results using duckdb connection to verify
        conn = duckdb.connect(db_path)
        mappings = conn.execute("SELECT attribute_name, sql_formula, not_enough_information FROM attribute_mappings").fetchall()
        assert len(mappings) == 1
        assert mappings[0][0] == "attr1"
        assert mappings[0][1] == "col1 + col2"
        assert mappings[0][2] is False
        conn.close()


def test_parse_ontology_classes():
    """Verify parse_ontology_classes correctly parses classes and filters out blank nodes."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        ttl_file = os.path.join(tmp_dir, "test_ontology.ttl")
        content = """
        @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix my: <http://example.org/my#> .

        my:LoanClass a owl:Class ;
            rdfs:label "Loan Class" ;
            rdfs:comment "A loan class example" .

        # A blank node owl:Class
        [] a owl:Class ;
            owl:unionOf (my:LoanClass) .
        """
        with open(ttl_file, "w", encoding="utf-8") as f:
            f.write(content)
            
        classes = parse_ontology_classes([ttl_file])
        assert len(classes) == 1
        assert classes[0]["class_name"] == "LoanClass"
        assert classes[0]["class_uri"] == "http://example.org/my#LoanClass"


def test_get_all_mappings():
    """Verify get_all_mappings queries and structures saved mappings correctly."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_mappings.duckdb")
        init_db(db_path)
        
        mapping1 = {
            "class_name": "ClassA",
            "class_uri": "http://example.org/ClassA",
            "source_ttl_file": "/path/to/ontology1.ttl",
            "attribute_mappings": [
                {
                    "attribute_name": "attr1",
                    "mapped_columns": ["col1"],
                    "sql_formula": None,
                    "not_enough_information": False,
                    "explanation": "Direct mapping"
                }
            ]
        }
        mapping2 = {
            "class_name": "ClassB",
            "class_uri": "http://example.org/ClassB",
            "source_ttl_file": "/path/to/ontology2.ttl",
            "attribute_mappings": [
                {
                    "attribute_name": "attr2",
                    "mapped_columns": ["col2", "col3"],
                    "sql_formula": "col2 + col3",
                    "not_enough_information": False,
                    "explanation": "Calculated value"
                }
            ]
        }
        
        save_class_mapping(db_path, mapping1)
        save_class_mapping(db_path, mapping2)
        
        all_mappings = get_all_mappings(db_path)
        
        assert "/path/to/ontology1.ttl" in all_mappings
        assert "ClassA" in all_mappings["/path/to/ontology1.ttl"]
        assert all_mappings["/path/to/ontology1.ttl"]["ClassA"]["attributes"]["attr1"] == {
            "mapped_columns": ["col1"],
            "sql_formula": None,
            "explanation": "Direct mapping",
            "not_enough_information": False
        }
        
        assert "/path/to/ontology2.ttl" in all_mappings
        assert "ClassB" in all_mappings["/path/to/ontology2.ttl"]
        assert all_mappings["/path/to/ontology2.ttl"]["ClassB"]["attributes"]["attr2"] == {
            "mapped_columns": ["col2", "col3"],
            "sql_formula": "col2 + col3",
            "explanation": "Calculated value",
            "not_enough_information": False
        }


def test_cli_yaml_output():
    """Verify that the Click command create-data-mapping generates the expected YAML mapping output."""
    from click.testing import CliRunner
    from app.cli import cli
    import yaml
    
    with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmp_dir:
        ontology_dir = os.path.join(tmp_dir, "ontology")
        loan_dir = os.path.join(ontology_dir, "LOAN")
        os.makedirs(loan_dir)
        ttl_file = os.path.join(loan_dir, "MockLoan.ttl")
        with open(ttl_file, "w", encoding="utf-8") as f:
            f.write("""
            @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            @prefix my: <http://example.org/my#> .

            my:TestLoanClass a owl:Class ;
                rdfs:label "Mock Loan Class" .
            my:hasLoanAmount a owl:ObjectProperty ;
                rdfs:domain my:TestLoanClass .
            """)
            
        dataset_dir = os.path.join(tmp_dir, "dataset")
        os.makedirs(dataset_dir)
        csv_file = os.path.join(dataset_dir, "loan_data.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("loan_amt,term\n100,36")
            
        db_path = os.path.join(tmp_dir, "mappings.duckdb")
        output_yaml = os.path.join(tmp_dir, "output.yaml")
        
        # Mock run_mapping_agent since we shouldn't make real API calls
        mock_agent_result = {
            "class_name": "TestLoanClass",
            "class_uri": "http://example.org/my#TestLoanClass",
            "source_ttl_file": ttl_file,
            "attribute_mappings": [
                {
                    "attribute_name": "hasLoanAmount",
                    "mapped_columns": ["loan_amt"],
                    "sql_formula": None,
                    "not_enough_information": False,
                    "explanation": "Direct semantic mapping"
                }
            ]
        }
        
        runner = CliRunner()
        with mock.patch("app.cli.run_mapping_agent", return_value=mock_agent_result):
            result = runner.invoke(cli, [
                "create-data-mapping",
                "--ontology-folder", ontology_dir,
                "--dataset-folder", dataset_dir,
                "--db-path", db_path,
                "--output-yaml", output_yaml,
                "--thread-count", "1"
            ])
            
            assert result.exit_code == 0, f"CLI command failed: {result.output}"
            
            # Verify YAML output exists and is populated correctly
            assert os.path.exists(output_yaml), "YAML output file was not created"
            with open(output_yaml, "r", encoding="utf-8") as f:
                lines = f.readlines()
                assert lines[0].startswith("# Coverage: 50.00%"), f"Expected coverage comment at top, got: {lines[0]}"
                yaml_content = "".join(lines[1:])
                data = yaml.safe_load(yaml_content)
                
            # Verify path mapping logic (relative paths)
            rel_ttl_path = os.path.relpath(ttl_file, os.getcwd())
            assert rel_ttl_path in data, f"Expected relative path {rel_ttl_path} in keys"
            assert "TestLoanClass" in data[rel_ttl_path]
            assert "attributes" in data[rel_ttl_path]["TestLoanClass"]
            assert data[rel_ttl_path]["TestLoanClass"]["attributes"]["hasLoanAmount"] == {
                "mapped_columns": ["loan_amt"],
                "sql_formula": None,
                "explanation": "Direct semantic mapping"
            }


def test_cli_generate_report():
    """Verify that the Click command generate-report correctly generates a detailed YAML mapping from database."""
    from click.testing import CliRunner
    from app.cli import cli
    import yaml
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_mappings.duckdb")
        init_db(db_path)
        
        # Create dataset folder and dummy CSV with matching columns
        dataset_dir = os.path.join(tmp_dir, "dataset")
        os.makedirs(dataset_dir)
        csv_file = os.path.join(dataset_dir, "report_data.csv")
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write("col_rep,other_col\n10,20")
            
        mapping = {
            "class_name": "ReportClass",
            "class_uri": "http://example.org/my#ReportClass",
            "source_ttl_file": "/absolute/path/to/Report.ttl",
            "attribute_mappings": [
                {
                    "attribute_name": "reportProperty",
                    "mapped_columns": ["col_rep"],
                    "sql_formula": "col_rep * 2",
                    "not_enough_information": False,
                    "explanation": "Double the column"
                },
                {
                    "attribute_name": "emptyProperty",
                    "mapped_columns": [],
                    "sql_formula": None,
                    "not_enough_information": False,
                    "explanation": "No mapping found"
                },
                {
                    "attribute_name": "insufficientProperty",
                    "mapped_columns": ["other_col"],
                    "sql_formula": None,
                    "not_enough_information": True,
                    "explanation": "Not enough info"
                }
            ]
        }
        save_class_mapping(db_path, mapping)
        
        output_yaml = os.path.join(tmp_dir, "report.yaml")
        
        # Run command: generate-report
        runner = CliRunner()
        result = runner.invoke(cli, [
            "generate-report",
            "--db-path", db_path,
            "--dataset-folder", dataset_dir,
            "--output-yaml", output_yaml
        ])
        
        assert result.exit_code == 0, f"generate-report failed: {result.output}"
        assert os.path.exists(output_yaml), "report.yaml was not created"
        
        with open(output_yaml, "r", encoding="utf-8") as f:
            lines = f.readlines()
            # The first line must be the coverage comment header
            assert lines[0].startswith("# Coverage: 50.00%"), f"Expected coverage comment at top, got: {lines[0]}"
            yaml_content = "".join(lines[1:])
            data = yaml.safe_load(yaml_content)
            
        assert "/absolute/path/to/Report.ttl" in data
        assert "ReportClass" in data["/absolute/path/to/Report.ttl"]
        assert "attributes" in data["/absolute/path/to/Report.ttl"]["ReportClass"]
        
        attrs = data["/absolute/path/to/Report.ttl"]["ReportClass"]["attributes"]
        
        # 'reportProperty' should be present
        assert "reportProperty" in attrs
        assert attrs["reportProperty"]["mapped_columns"] == ["col_rep"]
        assert attrs["reportProperty"]["sql_formula"] == "col_rep * 2"
        assert attrs["reportProperty"]["explanation"] == "Double the column"
        
        # 'emptyProperty' and 'insufficientProperty' must be filtered out
        assert "emptyProperty" not in attrs
        assert "insufficientProperty" not in attrs


def test_run_mapping_agent_mocked():
    """Verify that run_mapping_agent executes successfully using mocked LiteLLMClient.acompletion."""
    from app.agent import run_mapping_agent
    from google.adk.models.lite_llm import LiteLLMClient
    import json
    
    class_info = {
        "class_name": "TestClass",
        "class_uri": "http://example.org/TestClass",
        "definition": "A test class",
        "properties": ["prop1", "prop2"],
        "source_file": "TestOntology.ttl"
    }
    formatted_columns = "Dataset: test.csv\n- col1\n- col2"
    
    fake_completion_result = {
        "class_name": "TestClass",
        "class_uri": "http://example.org/TestClass",
        "source_ttl_file": "TestOntology.ttl",
        "attribute_mappings": [
            {
                "attribute_name": "prop1",
                "mapped_columns": ["col1"],
                "sql_formula": None,
                "not_enough_information": False,
                "explanation": "Direct mapping"
            }
        ]
    }
    
    mock_response = mock.MagicMock()
    mock_response.model = "openai/gpt-4o"
    
    mock_message = mock.MagicMock()
    mock_message.content = json.dumps(fake_completion_result)
    mock_message.get.side_effect = lambda key, default=None: {
        "content": json.dumps(fake_completion_result),
        "tool_calls": None
    }.get(key, default)
    
    mock_choice = mock.MagicMock()
    mock_choice.message = mock_message
    mock_choice.finish_reason = "stop"
    mock_choice.get.side_effect = lambda key, default=None: {
        "message": mock_message,
        "finish_reason": "stop"
    }.get(key, default)
    
    mock_response.choices = [mock_choice]
    mock_response.get.side_effect = lambda key, default=None: {
        "choices": [mock_choice],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30
        }
    }.get(key, default)
    
    mock_response.__getitem__.side_effect = lambda key: {
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30
        }
    }[key]
    
    with mock.patch.dict(os.environ, {
        "CUSTOM_LLM_URL": "https://custom-url.com/v1",
        "CUSTOM_LLM_MODEL": "openai/gpt-4o"
    }):
        with mock.patch.object(LiteLLMClient, "acompletion", return_value=mock_response):
            result = run_mapping_agent(class_info, formatted_columns)
            assert result is not None
            assert result["class_name"] == "TestClass"
            assert result["class_uri"] == "http://example.org/TestClass"
            assert result["source_ttl_file"] == "TestOntology.ttl"
            assert len(result["attribute_mappings"]) == 1
            assert result["attribute_mappings"][0]["attribute_name"] == "prop1"
            assert result["attribute_mappings"][0]["mapped_columns"] == ["col1"]


