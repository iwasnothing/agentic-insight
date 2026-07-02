"""
app/wiki.py - Module for PDF text extraction, segmentation, classification,
and ontology mapping workflow.

This module recursively processes PDF files, converts them to Markdown,
chunks the content, segments and classifies chunks using LiteLlm agents,
performs deduplication/merging, and outputs verification results with a coverage report.
"""

import os
import re
import json
import uuid
import logging
import duckdb
from typing import List, Optional, Dict, Any, Tuple
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.config import get_llm_model

logger = logging.getLogger(__name__)

# Pydantic schemas for structured LLM interaction

class TextSegment(BaseModel):
    """Represents a single logical text segment within a chunk."""
    segment_text: str = Field(description="The text content of the logical segment")
    relative_start_line: int = Field(description="The 0-indexed relative start line number of this segment within the chunk content (first line is 0)")
    relative_end_line: int = Field(description="The 0-indexed relative end line number of this segment within the chunk content")

class TextSegmentsResponse(BaseModel):
    """Represents a list of text segments returned by the LLM."""
    segments: List[TextSegment] = Field(description="List of logical text segments")

class SegmentClassification(BaseModel):
    """Represents the classification response for a segment."""
    concept_type: str = Field(description="The classified concept type from the ontology matching one of the options in the ontology schema exactly")
    concept_name: str = Field(description="The proposed concept/entity name representing the main subject, e.g. 'acme-corporation'")
    rationale: str = Field(description="Rationale for the classification result")
    summary: str = Field(description="A 1-line summary of the concept")
    attributes_and_values: Dict[str, str] = Field(description="Key-value attributes extracted for this concept from the segment text")

class ConceptMatchDecision(BaseModel):
    """Represents the matching and reconciliation decision for a concept."""
    decision: str = Field(description="Must be either 'new_concept' or 'update_concept'")
    matched_concept_name: Optional[str] = Field(default=None, description="The name of the matched existing concept. Null if 'new_concept'")
    merged_attributes: Optional[Dict[str, str]] = Field(default=None, description="Merged dictionary of concept attributes and values. Null if 'new_concept'")
    merged_summary: Optional[str] = Field(default=None, description="Updated 1-line summary of the merged concept. Null if 'new_concept'")


def init_wiki_db(db_path: str) -> None:
    """Initializes the database schema for the llm-wiki command."""
    logger.info(f"DB Write - Initializing wiki tables in: {db_path}")
    conn = duckdb.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_line_sequence (
                filename VARCHAR,
                start_line_no INTEGER,
                end_line_no INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS text_segments (
                segment_id VARCHAR PRIMARY KEY,
                segment_text VARCHAR,
                filename VARCHAR,
                segment_start_line_no_global INTEGER,
                segment_end_line_no_global INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concepts (
                concept_type VARCHAR,
                rationale VARCHAR,
                concept_name VARCHAR PRIMARY KEY,
                attributes_and_values VARCHAR,
                summary VARCHAR
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS concept_segment_mapping (
                segment_id VARCHAR,
                concept_name VARCHAR
            )
        """)
        # Clear tables that are rebuilt during each fresh execution
        conn.execute("DELETE FROM file_line_sequence")
        conn.execute("DELETE FROM text_segments")
        conn.execute("DELETE FROM concept_segment_mapping")
    finally:
        conn.close()


def normalize_concept_name(name: str) -> str:
    """Normalizes a concept name according to the core normalization rules (Step 10)."""
    name = name.lower()
    name = name.replace("&", "and")
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"[\s\-]+", "-", name)
    name = name.strip("-")
    return name or "concept"


def get_unique_concept_name(conn: duckdb.DuckDBPyConnection, base_name: str) -> str:
    """Finds a unique concept name in the database, appending suffix if duplicate."""
    logger.debug(f"DB Read - Checking uniqueness for concept: {base_name}")
    row = conn.execute("SELECT concept_name FROM concepts WHERE concept_name = ?", [base_name]).fetchone()
    if not row:
        return base_name

    i = 1
    while True:
        candidate = f"{base_name}-{i}"
        row = conn.execute("SELECT concept_name FROM concepts WHERE concept_name = ?", [candidate]).fetchone()
        if not row:
            return candidate
        i += 1


def get_union_coverage(intervals: List[Tuple[int, int]]) -> int:
    """Calculates the total number of unique lines covered by a list of intervals."""
    if not intervals:
        return 0
    valid_intervals = [(start, end) for start, end in intervals if start <= end]
    if not valid_intervals:
        return 0
    valid_intervals.sort(key=lambda x: x[0])

    merged = []
    for start, end in valid_intervals:
        if not merged or merged[-1][1] < start:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    return sum(end - start + 1 for start, end in merged)


async def segment_chunk_content(model: Any, chunk_content: str) -> TextSegmentsResponse:
    """Calls LLM to split a numbered chunk into logical text segments with boundaries."""
    prompt = f"""You are an expert content analyzer.
Your task is to analyze the following text chunk (where each line is prefixed with its 0-indexed relative line number: "[line_num]: line_content") and split it into a list of logical text segments.
A logical text segment is a coherent block of text that discusses a single topic, entity, or concept.
For each segment, you must extract:
1. The exact text content of the segment (without the line number prefixes).
2. The start line number (the 0-indexed relative line number of the first line of the segment).
3. The end line number (the 0-indexed relative line number of the last line of the segment).

Ensure that every part of the text belongs to a segment, and segments do not overlap unless necessary. Line numbers must correspond exactly to the text lines in the input.

Here is the chunk content:
{chunk_content}
"""
    logger.info("LLM Call - Segmenting chunk content")
    agent = LlmAgent(
        model=model,
        name="segmenter_agent",
        instruction="You are a data architect segmenting document chunks into logical sections.",
        output_schema=TextSegmentsResponse
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)

    new_message = types.Content(parts=[types.Part(text=prompt)])
    events = []
    async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
        events.append(event)
        if event.error_message:
            raise ValueError(f"LLM segmentation failed: {event.error_message}")

    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    if not model_text.strip() and events and events[-1].output:
        return events[-1].output

    from app.agent import clean_json_text
    cleaned_json = clean_json_text(model_text)

    from google.adk.utils._schema_utils import validate_schema
    result_dict = validate_schema(TextSegmentsResponse, cleaned_json)
    return TextSegmentsResponse(**result_dict)


async def classify_segment(model: Any, segment_text: str, ontology_content: str) -> SegmentClassification:
    """Calls LLM to classify segment text into exactly one concept type from the ontology."""
    prompt = f"""You are an expert data architect and ontologist.
Your task is to classify the following segment text into exactly one of the concept types defined in the ontology schema.

Ontology Schema (Concept Types and Descriptions):
{ontology_content}

Segment Text:
{segment_text}

Rules:
1. Classify the segment text into exactly 1 of the concept types in the ontology. If multiple concept types are possible, choose the most probable type for the main entity/subject discussed.
2. Identify the concept/entity name representing the main subject.
3. Provide a clear rationale explaining why this concept type was chosen.
4. Provide a 1-line summary of the concept.
5. Extract all relevant attributes and values mentioned in the segment text for this concept.
"""
    logger.info("LLM Call - Classifying segment")
    agent = LlmAgent(
        model=model,
        name="classifier_agent",
        instruction="You are a data architect classifying text segments to ontology concepts.",
        output_schema=SegmentClassification
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)

    new_message = types.Content(parts=[types.Part(text=prompt)])
    events = []
    async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
        events.append(event)
        if event.error_message:
            raise ValueError(f"LLM classification failed: {event.error_message}")

    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    if not model_text.strip() and events and events[-1].output:
        return events[-1].output

    from app.agent import clean_json_text
    cleaned_json = clean_json_text(model_text)

    from google.adk.utils._schema_utils import validate_schema
    result_dict = validate_schema(SegmentClassification, cleaned_json)
    return SegmentClassification(**result_dict)


async def reconcile_concept(
    model: Any,
    concept_type: str,
    candidate_name: str,
    candidate_summary: str,
    candidate_attributes: Dict[str, str],
    existing_concepts: List[Tuple[str, str, str]]
) -> ConceptMatchDecision:
    """Calls LLM to determine whether a concept is new or matches an existing one (fuzzy match)."""
    existing_list_str = ""
    for idx, (name, summary, attrs_json) in enumerate(existing_concepts):
        existing_list_str += f"{idx+1}. Name: {name}\n   Summary: {summary}\n   Attributes: {attrs_json}\n\n"

    prompt = f"""You are a master data deduplication and ontology reconciliation assistant.
Your task is to reconcile a newly classified concept with a list of existing concepts of the same type.

Concept Type: {concept_type}

New Classified Concept:
- Candidate Name: {candidate_name}
- 1-line Summary: {candidate_summary}
- Extracted Attributes: {candidate_attributes}

Existing Concepts of the same type:
{existing_list_str}

Instructions:
1. Determine whether the New Classified Concept represents a NEW entity (new concept) or matches one of the Existing Concepts (using fuzzy match, alias detection, or semantic equivalence).
2. If it is a new entity, return decision = "new_concept".
3. If it matches one of the existing concepts, return decision = "update_concept", specify the exact matched_concept_name, and merge their attributes, combining all attributes and values, and write an updated, cohesive 1-line summary.
"""
    logger.info(f"LLM Call - Reconciling concept: {candidate_name}")
    agent = LlmAgent(
        model=model,
        name="reconciliation_agent",
        instruction="You are a data architect reconciling and deduplicating entities.",
        output_schema=ConceptMatchDecision
    )
    runner = InMemoryRunner(agent=agent)
    session_id = f"session_{uuid.uuid4().hex}"
    await runner.session_service.create_session(app_name=runner.app_name, user_id="user", session_id=session_id)

    new_message = types.Content(parts=[types.Part(text=prompt)])
    events = []
    async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
        events.append(event)
        if event.error_message:
            raise ValueError(f"LLM reconciliation failed: {event.error_message}")

    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model' and event.content.parts:
            model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break

    if not model_text.strip() and events and events[-1].output:
        return events[-1].output

    from app.agent import clean_json_text
    cleaned_json = clean_json_text(model_text)

    from google.adk.utils._schema_utils import validate_schema
    result_dict = validate_schema(ConceptMatchDecision, cleaned_json)
    return ConceptMatchDecision(**result_dict)


async def execute_wiki_workflow(
    input_folder: str,
    output_folder: str,
    ontology_ttl: str,
    chunk_line_count: int,
    db_path: str
) -> None:
    """Executes the entire llm-wiki workflow: parsing, chunking, LLM analysis, mapping, and reporting."""
    # 1. Initialize Tables
    init_wiki_db(db_path)

    # 2. Iterate for PDF files & Convert using markitdown
    from markitdown import MarkItDown
    md_converter = MarkItDown()

    pdf_files = []
    for root, _, files in os.walk(input_folder):
        for file in files:
            if file.lower().endswith(".pdf"):
                pdf_files.append(os.path.join(root, file))

    if not pdf_files:
        logger.warning(f"No PDF files found recursively in: {input_folder}")
        return

    md_files = []
    for pdf_path in pdf_files:
        logger.info(f"Converting PDF: {pdf_path}")
        try:
            result = md_converter.convert(pdf_path)
            rel_path = os.path.relpath(pdf_path, input_folder)
            md_rel_path = os.path.splitext(rel_path)[0] + ".md"
            md_path = os.path.join(output_folder, md_rel_path)
            os.makedirs(os.path.dirname(md_path), exist_ok=True)

            with open(md_path, "w", encoding="utf-8") as f:
                f.write(result.text_content)
            md_files.append(md_path)
        except Exception as e:
            logger.error(f"Failed to convert PDF {pdf_path}: {e}")

    if not md_files:
        logger.warning("No markdown files were generated.")
        return

    # 3. Calculate and generate line ranges sequence with 10% overlap
    conn = duckdb.connect(db_path)
    for md_path in md_files:
        with open(md_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        total_lines = len(lines)
        if total_lines == 0:
            conn.execute("INSERT INTO file_line_sequence (filename, start_line_no, end_line_no) VALUES (?, ?, ?)",
                         [md_path, 1, 1])
            continue

        overlap = int(chunk_line_count * 0.1)
        step = chunk_line_count - overlap
        if step <= 0:
            step = chunk_line_count

        start = 1
        while True:
            end = min(start + chunk_line_count - 1, total_lines)
            conn.execute("INSERT INTO file_line_sequence (filename, start_line_no, end_line_no) VALUES (?, ?, ?)",
                         [md_path, start, end])
            if end >= total_lines:
                break
            start += step
    conn.close()

    # 4. Process segments for each row in file_line_sequence
    conn = duckdb.connect(db_path)
    chunks = conn.execute("SELECT filename, start_line_no, end_line_no FROM file_line_sequence").fetchall()
    conn.close()

    model = get_llm_model()

    for idx, (filename, start_line_no, end_line_no) in enumerate(chunks):
        logger.info(f"Segmenting chunk {idx+1}/{len(chunks)}: {filename} ({start_line_no}-{end_line_no})")
        with open(filename, "r", encoding="utf-8") as f:
            lines = f.readlines()

        chunk_lines = lines[start_line_no - 1 : end_line_no]
        numbered_lines = [f"{rel_idx}: {line}" for rel_idx, line in enumerate(chunk_lines)]
        chunk_content_numbered = "".join(numbered_lines)

        try:
            segments_res = await segment_chunk_content(model, chunk_content_numbered)
        except Exception as e:
            logger.error(f"Error segmenting chunk {filename} ({start_line_no}-{end_line_no}): {e}")
            continue

        conn = duckdb.connect(db_path)
        for seg in segments_res.segments:
            segment_id = f"seg_{uuid.uuid4().hex[:12]}"
            global_start = start_line_no + seg.relative_start_line
            global_end = start_line_no + seg.relative_end_line

            conn.execute("""
                INSERT INTO text_segments (
                    segment_id, segment_text, filename, segment_start_line_no_global, segment_end_line_no_global
                ) VALUES (?, ?, ?, ?, ?)
            """, [segment_id, seg.segment_text, filename, global_start, global_end])
        conn.close()

    # 5. Read the ontology file or parse multiple Turtle files
    from app.ontology import find_loan_folder, get_ttl_files, parse_ontology_classes
    
    ontology_content = ""
    # Check if the ontology path is a directory
    if os.path.isdir(ontology_ttl):
        try:
            loan_folder = find_loan_folder(ontology_ttl)
            ttl_files = get_ttl_files(loan_folder)
            all_classes = parse_ontology_classes(ttl_files)
        except Exception as e:
            logger.error(f"Failed to find or parse LOAN folder under {ontology_ttl}: {e}")
            all_classes = []
    else:
        # If it's a file, check if parent dir has LOAN folder
        all_classes = []
        if os.path.isfile(ontology_ttl):
            parent_dir = os.path.dirname(os.path.abspath(ontology_ttl))
            try:
                loan_folder = find_loan_folder(parent_dir)
                ttl_files = get_ttl_files(loan_folder)
                all_classes = parse_ontology_classes(ttl_files)
            except Exception:
                pass
            
            # If not found, try parsing the file directly as TTL
            if not all_classes:
                try:
                    all_classes = parse_ontology_classes([ontology_ttl])
                except Exception:
                    pass

    if all_classes:
        # Construct the classification ontology content from all parsed classes
        ontology_md_list = []
        for cls in all_classes:
            cls_name = cls["class_name"]
            cls_uri = cls["class_uri"]
            definition = cls.get("definition") or cls.get("label") or ""
            properties_str = ", ".join(cls.get("properties", []))
            
            ontology_md_list.append(
                f"Concept Type: {cls_name}\n"
                f"URI: {cls_uri}\n"
                f"Description: {definition}\n"
                f"Attributes: {properties_str}"
            )
        ontology_content = "\n---\n".join(ontology_md_list)
        logger.info(f"Loaded {len(all_classes)} concept types from ontology folder.")
    else:
        # Fallback to reading file directly as markdown
        if os.path.isfile(ontology_ttl):
            with open(ontology_ttl, "r", encoding="utf-8") as f:
                ontology_content = f.read()
        else:
            raise ValueError(f"No OWL classes or text content found at ontology path: {ontology_ttl}")

    # 6. Classify and reconcile text segments
    conn = duckdb.connect(db_path)
    segments = conn.execute("SELECT segment_id, segment_text FROM text_segments").fetchall()
    conn.close()

    for seg_id, seg_text in segments:
        try:
            classification = await classify_segment(model, seg_text, ontology_content)
        except Exception as e:
            logger.error(f"Failed to classify segment {seg_id}: {e}")
            continue

        conn = duckdb.connect(db_path)
        concepts_table_exists = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'concepts'"
        ).fetchone()[0]

        existing_concepts = []
        if concepts_table_exists:
            existing_concepts = conn.execute(
                "SELECT concept_name, summary, attributes_and_values FROM concepts WHERE concept_type = ?",
                [classification.concept_type]
            ).fetchall()
        conn.close()

        is_new = True
        matched_name = None
        merged_attrs = classification.attributes_and_values
        merged_summary = classification.summary

        if existing_concepts:
            try:
                reconciliation = await reconcile_concept(
                    model,
                    classification.concept_type,
                    classification.concept_name,
                    classification.summary,
                    classification.attributes_and_values,
                    existing_concepts
                )
                if reconciliation.decision == "update_concept" and reconciliation.matched_concept_name:
                    is_new = False
                    matched_name = reconciliation.matched_concept_name
                    merged_attrs = reconciliation.merged_attributes or classification.attributes_and_values
                    merged_summary = reconciliation.merged_summary or classification.summary
            except Exception as e:
                logger.error(f"Reconciliation failed for segment {seg_id}, treating as new: {e}")

        conn = duckdb.connect(db_path)
        if is_new:
            base_normalized_name = normalize_concept_name(classification.concept_name)
            final_concept_name = get_unique_concept_name(conn, base_normalized_name)
            conn.execute("""
                INSERT INTO concepts (concept_type, rationale, concept_name, attributes_and_values, summary)
                VALUES (?, ?, ?, ?, ?)
            """, [
                classification.concept_type,
                classification.rationale,
                final_concept_name,
                json.dumps(merged_attrs),
                merged_summary
            ])
        else:
            final_concept_name = matched_name
            conn.execute("""
                UPDATE concepts
                SET attributes_and_values = ?, summary = ?, rationale = ?
                WHERE concept_name = ?
            """, [
                json.dumps(merged_attrs),
                merged_summary,
                classification.rationale,
                final_concept_name
            ])

        conn.execute("""
            INSERT INTO concept_segment_mapping (segment_id, concept_name)
            VALUES (?, ?)
        """, [seg_id, final_concept_name])
        conn.close()

    # 7. Verification (Step 8)
    conn = duckdb.connect(db_path)
    unmatched = conn.execute("""
        SELECT DISTINCT m.concept_name
        FROM concept_segment_mapping m
        LEFT JOIN concepts c ON m.concept_name = c.concept_name
        WHERE c.concept_name IS NULL
    """).fetchall()

    if unmatched:
        conn.close()
        msg = f"Verification failed! Unmatched concept names: {[u[0] for u in unmatched]}"
        logger.error(msg)
        raise ValueError(msg)
    else:
        logger.info("Verification passed! All mapped concepts exist in concepts table.")

    # 8. Coverage Report (Step 9)
    concepts_by_type = conn.execute("""
        SELECT concept_type, COUNT(*)
        FROM concepts
        GROUP BY concept_type
        ORDER BY concept_type
    """).fetchall()

    concepts_multiple_segments = conn.execute("""
        WITH counts AS (
            SELECT c.concept_name, c.concept_type, COUNT(m.segment_id) as seg_count
            FROM concepts c
            JOIN concept_segment_mapping m ON c.concept_name = m.concept_name
            GROUP BY c.concept_name, c.concept_type
        )
        SELECT concept_type, COUNT(*)
        FROM counts
        WHERE seg_count > 1
        GROUP BY concept_type
        ORDER BY concept_type
    """).fetchall()

    segments_by_file = conn.execute("""
        SELECT filename, segment_start_line_no_global, segment_end_line_no_global
        FROM text_segments
        ORDER BY filename, segment_start_line_no_global
    """).fetchall()
    conn.close()

    file_segments: Dict[str, List[Tuple[int, int]]] = {}
    for fname, start, end in segments_by_file:
        if fname not in file_segments:
            file_segments[fname] = []
        file_segments[fname].append((start, end))

    report = []
    report.append("=========================================")
    report.append("            COVERAGE REPORT              ")
    report.append("=========================================")
    report.append("\n--- Concepts by Type ---")
    for ctype, count in concepts_by_type:
        report.append(f"{ctype}: {count}")

    report.append("\n--- Concepts with > 1 Source Segments by Type ---")
    mult_map = dict(concepts_multiple_segments)
    for ctype, _ in concepts_by_type:
        mult_count = mult_map.get(ctype, 0)
        report.append(f"{ctype}: {mult_count}")

    report.append("\n--- File Line Coverage ---")
    for filename in sorted(file_segments.keys()):
        intervals = file_segments[filename]
        union_lines_covered = get_union_coverage(intervals)

        try:
            with open(filename, "r", encoding="utf-8") as f:
                total_lines = len(f.readlines())
        except Exception as e:
            logger.error(f"Error reading file {filename} for line count: {e}")
            total_lines = 1

        total_lines = max(1, total_lines)
        coverage_pct = (union_lines_covered / total_lines) * 100.0
        report.append(
            f"File: {filename}\n"
            f"  Total Lines: {total_lines}\n"
            f"  Lines Covered: {union_lines_covered}\n"
            f"  Coverage: {coverage_pct:.2f}%"
        )

    report_text = "\n".join(report)
    logger.info(report_text)

    report_path = os.path.join(output_folder, "coverage_report.txt")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        logger.info(f"Saved coverage report to: {report_path}")
    except Exception as e:
        logger.error(f"Failed to write coverage report: {e}")
