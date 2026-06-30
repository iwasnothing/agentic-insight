"""
tests/test_md_to_pdf.py - Unit tests for scripts/md_to_pdf.py.
"""

import os
import tempfile
import pytest
from pypdf import PdfReader
from scripts.md_to_pdf import convert_markdown_to_pdf

def test_convert_markdown_to_pdf_default():
    """
    Verify that convert_markdown_to_pdf converts a markdown file
    to PDF and saves it at the default location with correct content.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = os.path.join(tmpdir, "test_doc.md")
        expected_pdf_path = os.path.join(tmpdir, "test_doc.pdf")
        
        md_content = (
            "# Main Test Title\n\n"
            "This is a paragraph under the main title. "
            "It tests the PDF generation functionality.\n\n"
            "## Subsection Test\n\n"
            "Some content in a subsection with a `code_snippet` here."
        )
        
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
            
        # Run conversion without specifying output_path (defaulting to .pdf)
        pdf_path = convert_markdown_to_pdf(md_path)
        
        assert pdf_path == expected_pdf_path
        assert os.path.exists(pdf_path)
        
        # Verify the generated PDF's text content
        reader = PdfReader(pdf_path)
        assert len(reader.pages) > 0
        
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        assert "Main Test Title" in full_text
        assert "This is a paragraph under the main title" in full_text
        assert "Subsection Test" in full_text
        assert "code_snippet" in full_text

def test_convert_markdown_to_pdf_custom_output_and_css():
    """
    Verify that convert_markdown_to_pdf supports custom output paths
    and doesn't crash when passing custom CSS.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = os.path.join(tmpdir, "doc.md")
        custom_pdf_path = os.path.join(tmpdir, "custom_output.pdf")
        css_path = os.path.join(tmpdir, "style.css")
        
        md_content = "# Hello World\nTesting custom output."
        css_content = "h1 { color: #FF0000; }"
        
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
            
        with open(css_path, "w", encoding="utf-8") as f:
            f.write(css_content)
            
        pdf_path = convert_markdown_to_pdf(md_path, output_path=custom_pdf_path, css_path=css_path)
        
        assert pdf_path == custom_pdf_path
        assert os.path.exists(pdf_path)
        
        reader = PdfReader(pdf_path)
        text = reader.pages[0].extract_text() or ""
        assert "Hello World" in text

def test_convert_markdown_to_pdf_file_not_found():
    """
    Verify that FileNotFoundError is raised if the source markdown file doesn't exist.
    """
    with pytest.raises(FileNotFoundError):
        convert_markdown_to_pdf("non_existent_file.md")

def test_convert_markdown_to_pdf_with_mermaid():
    """
    Verify that convert_markdown_to_pdf successfully parses and converts
    Mermaid diagrams, downloads them, embeds them, and cleans up the temporary files.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        md_path = os.path.join(tmpdir, "mermaid_doc.md")
        expected_pdf_path = os.path.join(tmpdir, "mermaid_doc.pdf")
        
        md_content = (
            "# Document with Diagram\n\n"
            "Here is a flowchart:\n\n"
            "```mermaid\n"
            "graph TD\n"
            "    A[Start] --> B(Process)\n"
            "    B --> C{Decision}\n"
            "    C -->|Yes| D[Result 1]\n"
            "    C -->|No| E[Result 2]\n"
            "```\n\n"
            "End of doc."
        )
        
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
            
        # Run conversion
        pdf_path = convert_markdown_to_pdf(md_path)
        
        assert pdf_path == expected_pdf_path
        assert os.path.exists(pdf_path)
        
        # Verify that no temporary files are left behind
        temp_files = [f for f in os.listdir(tmpdir) if f.startswith(".mermaid_temp_")]
        assert len(temp_files) == 0, f"Stray temp files found: {temp_files}"
        
        # Verify the generated PDF's text content
        reader = PdfReader(pdf_path)
        assert len(reader.pages) > 0
        
        full_text = ""
        for page in reader.pages:
            full_text += page.extract_text() or ""
            
        assert "Document with Diagram" in full_text
        assert "End of doc." in full_text
