"""
tests/test_wiki.py - Unit test suite for the LLM Wiki workflow command.
"""

import os
import tempfile
import json
import pytest
import duckdb
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from app.wiki import (
    normalize_concept_name,
    get_union_coverage,
    get_unique_concept_name,
    execute_wiki_workflow,
    TextSegmentsResponse,
    TextSegment,
    SegmentClassification,
    ConceptMatchDecision,
)

def test_normalize_concept_name():
    """Verify name normalization follows the core rules in Step 10."""
    assert normalize_concept_name("Google Inc.") == "google-inc"
    assert normalize_concept_name("A & B Corporation?") == "a-and-b-corporation"
    assert normalize_concept_name("  -Clean--Me- ") == "clean-me"
    assert normalize_concept_name("!!!SpecialChars!!!") == "specialchars"
    assert normalize_concept_name("") == "concept"

def test_get_union_coverage():
    """Verify union range line coverage calculations work correctly."""
    # Disjoint intervals
    assert get_union_coverage([(1, 5), (10, 15)]) == 11
    # Overlapping intervals
    assert get_union_coverage([(1, 10), (5, 15)]) == 15
    # Nested and out of order
    assert get_union_coverage([(20, 25), (1, 10), (5, 12)]) == 18
    # Empty list
    assert get_union_coverage([]) == 0

def test_get_unique_concept_name():
    """Verify suffix generation (-1, -2, etc.) for duplicate concepts."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE concepts (concept_name VARCHAR PRIMARY KEY)")
    
    # First entry should be direct
    assert get_unique_concept_name(conn, "my-concept") == "my-concept"
    
    # Insert first entry
    conn.execute("INSERT INTO concepts (concept_name) VALUES ('my-concept')")
    
    # Second should append -1
    assert get_unique_concept_name(conn, "my-concept") == "my-concept-1"
    conn.execute("INSERT INTO concepts (concept_name) VALUES ('my-concept-1')")
    
    # Third should append -2
    assert get_unique_concept_name(conn, "my-concept") == "my-concept-2"
    conn.close()

class MockConversionResult:
    def __init__(self, text):
        self.text_content = text

@pytest.mark.anyio
@mock.patch("app.wiki.get_llm_model")
@mock.patch("markitdown.MarkItDown.convert")
async def test_execute_wiki_workflow(mock_convert, mock_get_llm_model):
    """Test the complete workflow from PDF to coverage report under mocked LLM."""
    mock_convert.return_value = MockConversionResult(
        "Line 1: Google is a technology company.\n"
        "Line 2: They build search engines and software.\n"
        "Line 3: Microsoft is another tech company.\n"
        "Line 4: They build operating systems.\n"
    )
    
    # Mock LLM model
    mock_model = MagicMock()
    mock_get_llm_model.return_value = mock_model
    
    # We will mock the helper agent calls in app.wiki
    mock_segment_response = TextSegmentsResponse(
        segments=[
            TextSegment(segment_text="Google information", relative_start_line=0, relative_end_line=1),
            TextSegment(segment_text="Microsoft information", relative_start_line=2, relative_end_line=3)
        ]
    )
    
    mock_class_response_1 = SegmentClassification(
        concept_type="Company",
        concept_name="Google Inc.",
        rationale="Mentions Google as a tech company",
        summary="Google profile",
        attributes_and_values={"industry": "technology", "founder": "Larry Page"}
    )
    
    mock_class_response_2 = SegmentClassification(
        concept_type="Company",
        concept_name="Microsoft Corp",
        rationale="Mentions Microsoft",
        summary="Microsoft profile",
        attributes_and_values={"industry": "software"}
    )
    
    mock_reconcile_response_new = ConceptMatchDecision(
        decision="new_concept",
        matched_concept_name=None,
        merged_attributes=None,
        merged_summary=None
    )
    
    mock_reconcile_response_update_google = ConceptMatchDecision(
        decision="update_concept",
        matched_concept_name="google-inc",
        merged_attributes={"industry": "technology", "founder": "Larry Page", "hq": "Mountain View"},
        merged_summary="Google profile (updated)"
    )
    
    mock_reconcile_response_update_msft = ConceptMatchDecision(
        decision="update_concept",
        matched_concept_name="microsoft-corp",
        merged_attributes={"industry": "software", "hq": "Redmond"},
        merged_summary="Microsoft profile (updated)"
    )
    
    with mock.patch("app.wiki.segment_chunk_content", return_value=mock_segment_response) as mock_seg, \
         mock.patch("app.wiki.classify_segment") as mock_class, \
         mock.patch("app.wiki.reconcile_concept") as mock_rec:
         
        # Make classify_segment return different classifications based on call order
        mock_class.side_effect = [
            mock_class_response_1,
            mock_class_response_2,
            mock_class_response_1,
            mock_class_response_2,
        ]
        
        # 1st call: new type (Google) - no existing concepts (db helper doesn't call reconcile_concept)
        # 2nd call: new type (Microsoft) - no existing concepts (db helper doesn't call reconcile_concept)
        # 3rd call: existing Google - should match Google
        # 4th call: existing Microsoft - should match Microsoft
        mock_rec.side_effect = [
            mock_reconcile_response_update_google,
            mock_reconcile_response_update_msft
        ]
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            input_folder = os.path.join(tmp_dir, "input")
            output_folder = os.path.join(tmp_dir, "output")
            os.makedirs(input_folder)
            
            # Create a dummy PDF file (its content doesn't matter since markitdown is mocked)
            pdf_path = os.path.join(input_folder, "test_doc.pdf")
            with open(pdf_path, "wb") as f:
                f.write(b"%PDF-1.4 ... dummy content")
                
            # Create a LOAN directory structure with a dummy TTL file
            ontology_dir = os.path.join(tmp_dir, "ontology")
            loan_dir = os.path.join(ontology_dir, "LOAN")
            os.makedirs(loan_dir)
            
            ttl_file = os.path.join(loan_dir, "Company.ttl")
            with open(ttl_file, "w", encoding="utf-8") as f:
                f.write("""
                @prefix owl: <http://www.w3.org/2002/07/owl#> .
                @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
                @prefix my: <http://example.org/my#> .
                
                my:Company a owl:Class ;
                    rdfs:label "Company" ;
                    rdfs:comment "A business entity." .
                """)
                
            db_path = os.path.join(tmp_dir, "wiki.db")
            
            # Execute workflow passing the ontology folder path
            await execute_wiki_workflow(
                input_folder=input_folder,
                output_folder=output_folder,
                ontology_ttl=ontology_dir,
                chunk_line_count=2, # Small chunk size to verify overlap calculation
                db_path=db_path
            )
            
            # Verify tables content
            conn = duckdb.connect(db_path)
            
            # Check file_line_sequence
            seq = conn.execute("SELECT start_line_no, end_line_no FROM file_line_sequence ORDER BY start_line_no").fetchall()
            # Total lines is 4. chunk_line_count = 2, overlap = 0. Step = 2.
            # start=1, end=2. start=3, end=4.
            assert len(seq) == 2
            assert seq[0] == (1, 2)
            assert seq[1] == (3, 4)
            
            # Check text_segments (since there are 2 chunks, and each gets split into 2 segments by mock)
            segments = conn.execute("SELECT segment_text, segment_start_line_no_global, segment_end_line_no_global FROM text_segments").fetchall()
            # 2 chunks * 2 segments each = 4 segments
            assert len(segments) == 4
            
            # Check concepts (two unique concepts: google-inc and microsoft-corp)
            concepts = conn.execute("SELECT concept_name, concept_type, summary FROM concepts ORDER BY concept_name").fetchall()
            assert len(concepts) == 2
            assert concepts[0][0] == "google-inc"
            assert concepts[0][1] == "Company"
            assert concepts[1][0] == "microsoft-corp"
            assert concepts[1][1] == "Company"
            
            # Check concept segment mappings
            mappings = conn.execute("SELECT count(*) FROM concept_segment_mapping").fetchone()[0]
            assert mappings == 4
            
            conn.close()
            
            # Check coverage report file was written
            report_file = os.path.join(output_folder, "coverage_report.txt")
            assert os.path.exists(report_file)
            with open(report_file, "r") as f:
                report_content = f.read()
            assert "COVERAGE REPORT" in report_content
            assert "Company:" in report_content
            assert "File:" in report_content

def test_cli_llm_wiki():
    """Verify that the llm-wiki click command can be invoked successfully with aliases."""
    from click.testing import CliRunner
    from app.cli import cli
    
    # Test --ontology-ttl
    with mock.patch("app.wiki.execute_wiki_workflow") as mock_execute:
        runner = CliRunner()
        result = runner.invoke(cli, [
            "llm-wiki",
            "--input-folder", os.getcwd(),
            "--output-folder", "/tmp/dummy_output",
            "--ontology-ttl", "AGENTS.md",
            "--db-path", "/tmp/dummy.db",
            "--chunk-line-count", "500"
        ])
        assert result.exit_code == 0
        mock_execute.assert_called_once_with(
            os.getcwd(),
            "/tmp/dummy_output",
            "AGENTS.md",
            500,
            "/tmp/dummy.db"
        )

    # Test --ontology-folder
    with mock.patch("app.wiki.execute_wiki_workflow") as mock_execute:
        runner = CliRunner()
        result = runner.invoke(cli, [
            "llm-wiki",
            "--input-folder", os.getcwd(),
            "--output-folder", "/tmp/dummy_output",
            "--ontology-folder", os.getcwd(),
            "--db-path", "/tmp/dummy.db",
            "--chunk-line-count", "500"
        ])
        assert result.exit_code == 0
        mock_execute.assert_called_once_with(
            os.getcwd(),
            "/tmp/dummy_output",
            os.getcwd(),
            500,
            "/tmp/dummy.db"
        )

