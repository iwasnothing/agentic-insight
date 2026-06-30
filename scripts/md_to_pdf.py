"""
scripts/md_to_pdf.py - Markdown to PDF Converter.

This script converts a specified Markdown file into a PDF document.
The output PDF is saved in the same directory as the input Markdown file,
using the same base filename but with a '.pdf' extension.
It includes support for custom CSS styling, handles relative image paths,
and converts Mermaid syntax blocks to embedded PNG images.
"""

import sys
import os
import argparse
import logging
import re
import base64
import requests
import hashlib
from typing import Optional, Tuple, List
from markdown_pdf import MarkdownPdf, Section

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Premium default CSS theme
DEFAULT_CSS = """
body {
    font-family: helvetica, sans-serif;
    line-height: 1.6;
    color: #2D3748;
    margin: 24px;
}
h1, h2, h3, h4, h5, h6 {
    color: #1A365D;
    font-weight: bold;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
}
h1 {
    font-size: 22pt;
    border-bottom: 2px solid #E2E8F0;
    padding-bottom: 0.3em;
}
h2 {
    font-size: 16pt;
    border-bottom: 1px solid #E2E8F0;
    padding-bottom: 0.2em;
}
h3 {
    font-size: 13pt;
}
p {
    margin-bottom: 1em;
}
code {
    font-family: courier, monospace;
    background-color: #EDF2F7;
    padding: 2px 4px;
    border-radius: 4px;
    font-size: 0.9em;
}
pre {
    background-color: #EDF2F7;
    padding: 12px;
    border-radius: 6px;
    margin: 1.5em 0;
}
pre code {
    background-color: transparent;
    padding: 0;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 1.5em 0;
}
th, td {
    border: 1px solid #CBD5E0;
    padding: 8px 12px;
    text-align: left;
}
th {
    background-color: #F7FAFC;
    font-weight: bold;
}
blockquote {
    border-left: 4px solid #3182CE;
    padding-left: 15px;
    color: #4A5568;
    font-style: italic;
    margin: 1.5em 0;
}
ul, ol {
    margin-bottom: 1.5em;
    padding-left: 20px;
}
li {
    margin-bottom: 0.5em;
}
a {
    color: #3182CE;
    text-decoration: none;
}
"""

def _process_mermaid_diagrams(md_content: str, root_dir: str) -> Tuple[str, List[str]]:
    """
    Finds Mermaid code blocks in Markdown, converts them to PNGs via mermaid.ink,
    and replaces the blocks with Markdown image links.
    
    Args:
        md_content: The original Markdown content string.
        root_dir: The directory where the Markdown file is located.
        
    Returns:
        A tuple of (modified_md_content, list_of_temporary_image_paths).
    """
    # Match ```mermaid ... ```
    pattern = re.compile(r"```mermaid\s*\n(.*?)\n\s*```", re.DOTALL)
    temp_images = []
    diagram_cache = {}

    def replace_match(match):
        mermaid_code = match.group(1).strip()
        if not mermaid_code:
            return match.group(0)

        # Check cache first
        if mermaid_code in diagram_cache:
            temp_filename = diagram_cache[mermaid_code]
            return f"![Mermaid Diagram]({temp_filename})"

        # Generate a unique MD5 filename to resolve conflicts
        code_hash = hashlib.md5(mermaid_code.encode("utf-8")).hexdigest()
        temp_filename = f".mermaid_temp_{code_hash}.png"
        temp_path = os.path.join(root_dir, temp_filename)

        # Base64 encode for the mermaid.ink URL
        encoded = base64.urlsafe_b64encode(mermaid_code.encode("utf-8")).decode("ascii")
        url = f"https://mermaid.ink/img/{encoded}"

        logger.info(f"Converting Mermaid block (hash: {code_hash[:8]}) to image via mermaid.ink API...")
        try:
            # Reuse file if already exists
            if os.path.exists(temp_path):
                logger.info(f"Reusing existing Mermaid image file: {temp_path}")
                if temp_path not in temp_images:
                    temp_images.append(temp_path)
                diagram_cache[mermaid_code] = temp_filename
                return f"![Mermaid Diagram]({temp_filename})"

            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                with open(temp_path, "wb") as f:
                    f.write(response.content)
                temp_images.append(temp_path)
                diagram_cache[mermaid_code] = temp_filename
                logger.debug(f"Saved Mermaid image to: {temp_path}")
                return f"![Mermaid Diagram]({temp_filename})"
            else:
                logger.warning(
                    f"Failed to fetch Mermaid image (HTTP {response.status_code}). "
                    f"Response: {response.text[:200]}"
                )
                return match.group(0)
        except Exception as ex:
            logger.error(f"Error fetching Mermaid image: {ex}")
            return match.group(0)

    processed_content = pattern.sub(replace_match, md_content)
    return processed_content, temp_images

def convert_markdown_to_pdf(md_path: str, output_path: Optional[str] = None, css_path: Optional[str] = None) -> str:
    """
    Converts a markdown file to a PDF file.
    
    Args:
        md_path: Path to the input Markdown file.
        output_path: Path where the output PDF will be saved. If None, it defaults
                     to the same path as md_path but with a .pdf extension.
        css_path: Optional path to a custom CSS file for styling the PDF.
        
    Returns:
        The path to the generated PDF file.
    """
    if not os.path.exists(md_path):
        raise FileNotFoundError(f"Markdown file not found: {md_path}")
        
    if not output_path:
        base_path, _ = os.path.splitext(md_path)
        output_path = base_path + ".pdf"
        
    logger.info(f"Reading Markdown content from '{md_path}'...")
    with open(md_path, "r", encoding="utf-8") as f:
        md_content = f.read()
        
    # Determine custom or default CSS
    css_content = DEFAULT_CSS
    if css_path:
        if os.path.exists(css_path):
            logger.info(f"Loading custom CSS from '{css_path}'...")
            with open(css_path, "r", encoding="utf-8") as f:
                css_content = f.read()
        else:
            logger.warning(f"Custom CSS file '{css_path}' not found. Using default styling.")
            
    # Use the markdown file's directory as root for resolving relative resources (like images)
    root_dir = os.path.dirname(os.path.abspath(md_path))
    logger.debug(f"Using root directory '{root_dir}' for resolving relative assets.")
    
    temp_images = []
    try:
        # Process mermaid diagrams
        md_content, temp_images = _process_mermaid_diagrams(md_content, root_dir)
        
        logger.info("Initializing PDF generation...")
        pdf = MarkdownPdf(toc_level=2)
        
        section = Section(md_content, root=root_dir)
        pdf.add_section(section, user_css=css_content)
        
        logger.info(f"Saving PDF to '{output_path}'...")
        pdf.save(output_path)
        logger.info("PDF generation completed successfully.")
    finally:
        # Cleanup temporary mermaid images
        for temp_img in temp_images:
            if os.path.exists(temp_img):
                try:
                    os.remove(temp_img)
                    logger.debug(f"Cleaned up temporary file: {temp_img}")
                except Exception as e:
                    logger.warning(f"Could not clean up temporary file {temp_img}: {e}")
                    
    return output_path

def main() -> None:
    """
    CLI entry point for md_to_pdf converter.
    """
    parser = argparse.ArgumentParser(
        description="Convert Markdown files to beautifully styled PDF documents."
    )
    parser.add_argument(
        "md_path",
        help="Path to the input Markdown file."
    )
    parser.add_argument(
        "-o", "--output",
        help="Path to the output PDF file (defaults to input filename with .pdf extension)."
    )
    parser.add_argument(
        "--css",
        help="Path to a custom CSS file to style the PDF."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging output."
    )
    
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
        
    try:
        convert_markdown_to_pdf(
            md_path=args.md_path,
            output_path=args.output,
            css_path=args.css
        )
    except Exception as e:
        logger.error(f"Failed to convert Markdown to PDF: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
