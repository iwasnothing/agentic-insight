"""
api/workflow.py - Core orchestrator for the Agentic Search workflow.

This module coordinates the 6 steps of the agentic search:
1. Planning (objective analysis and action plan generation).
2. Plan execution (using doc and tabular retrieval tools).
3. Analysis (generating recommendations based on accumulated context).
4. Evaluation (evaluating recommendations and generating confidence score).
5. Conditional branching (looping back if confidence score < 90, up to max iterations).
6. Report generation (writing the final analysis and logs to markdown).
"""

import os
import logging
import uuid
import duckdb
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.config import get_llm_model
from api.utils import condense_summary, clean_and_filter_mapping, sanitize_semantic_mapping_file
from api.tools import doc_context_retrieval, tabular_data_retrieval

logger = logging.getLogger(__name__)

# --- Pydantic schemas for the workflow ---

class ActionItem(BaseModel):
    tool: str = Field(description="The tool to call: 'doc_context_retrieval' or 'tabular_data_retrieval'")
    query_string: str = Field(description="The search/query string to execute")
    explanation: str = Field(description="Explanation of why this retrieval is needed")

class ActionPlan(BaseModel):
    steps: List[ActionItem] = Field(description="List of retrieval action items")

class EvaluationResult(BaseModel):
    confidence_score: int = Field(description="Confidence score from 0 to 100 evaluating the analysis")
    explanation: str = Field(description="Detailed explanation of the score and remaining gaps")


# --- Util database functions for step 1 ---

def get_concept_summary(doc_db_path: str) -> str:
    """
    Selects all concepts with their 1-line summary from the concepts table in DuckDB.

    Args:
        doc_db_path (str): Path to the DuckDB database.

    Returns:
        str: Summary text listing concepts.
    """
    logger.debug(f"Fetching concept summaries from doc database: {doc_db_path}")
    conn = duckdb.connect(doc_db_path)
    try:
        rows = conn.execute("SELECT concept_name, concept_type, summary FROM concepts").fetchall()
        if not rows:
            return "No concepts found."
        
        lines = []
        for name, c_type, summary in rows:
            lines.append(f"- Concept: {name} (Type: {c_type}) - Summary: {summary}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error querying concepts summaries: {e}")
        return f"Error fetching concepts: {str(e)}"
    finally:
        conn.close()

def get_semantic_mapping(semantic_mapping_path: str) -> str:
    """
    Reads semantic mapping and strips the verbose 'explanation' fields
    to drastically reduce context size and prevent model context overflow.
    """
    logger.debug(f"Reading semantic mapping from: {semantic_mapping_path}")
    if not os.path.exists(semantic_mapping_path):
        return "Semantic mapping file not found."
    try:
        import yaml
        with open(semantic_mapping_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        
        filtered_data = clean_and_filter_mapping(data)
        return yaml.safe_dump(filtered_data, sort_keys=False, default_flow_style=False)
    except Exception as e:
        logger.error(f"Error reading semantic mapping: {e}")
        try:
            with open(semantic_mapping_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""


# --- Main workflow execution ---

async def run_agentic_workflow(
    objective: str,
    ontology_folder: str = "./ontology",
    dataset_folder: str = "./dataset",
    doc_db_path: str = "./wiki.db",
    mappings_db_path: str = "./mappings.db",
    semantic_mapping_path: str = "./mapping.yaml",
    output_md_path: str = "./report.md",
    max_iterations: int = 10,
    top_n: int = 100,
    row_limit: int = 100,
    max_sql_iterations: int = 5,
    lines_threshold: int = 500,
    context_size_limit: int = 10000
) -> Dict[str, Any]:
    """
    Orchestrates the multi-step agentic search and audit workflow.

    Args:
        objective (str): Analysis objective.
        ontology_folder (str): Directory containing the ontology.
        dataset_folder (str): Directory containing dataset CSVs.
        doc_db_path (str): DuckDB document/wiki file path.
        mappings_db_path (str): DuckDB mappings file path.
        semantic_mapping_path (str): YAML mapping file path.
        output_md_path (str): Path to write the final markdown report.
        max_iterations (int): Maximum planning-evaluation loop iterations.
        top_n (int): Max concepts returned during search reranking.
        row_limit (int): Cap on database rows fetched per query.
        max_sql_iterations (int): Cap on self-fixing SQL repair attempts.
        lines_threshold (int): Lines threshold for triggering condensation.
        context_size_limit (int): Word limit target for condensed context.

    Returns:
        Dict[str, Any]: Run metadata and results.
    """
    logger.info(f"Starting agentic search workflow for objective: '{objective}'")
    
    # Verification & Sanitization of the semantic mapping file against actual DuckDB schemas
    logger.info(f"Sanitizing semantic mapping file {semantic_mapping_path} against schema in {mappings_db_path}")
    try:
        sanitize_semantic_mapping_file(semantic_mapping_path, mappings_db_path)
    except Exception as e:
        logger.error(f"Failed to sanitize semantic mapping file: {e}")

    model = get_llm_model()

    # Minimum State variables
    accumulated_context = ""
    accumulated_analysis = []
    accumulated_action_plans = []

    # Local state for loops
    iteration = 0
    confidence_score = 0
    evaluation_explanation = ""
    last_analysis = ""

    while iteration < max_iterations:
        iteration += 1
        logger.info(f"=== Workflow Iteration {iteration}/{max_iterations} ===")

        # Step 1: Planning - objective analysis and decomposition
        semantic_map = get_semantic_mapping(semantic_mapping_path)
        concept_sum = get_concept_summary(doc_db_path)
        
        combined_bg = f"--- Structured Semantic Mapping ---\n{semantic_map}\n\n--- Unstructured Concepts Summary ---\n{concept_sum}"
        
        # Ensure combined background context is within context limit
        condensed_bg = await condense_summary(
            combined_bg,
            model=model,
            lines_threshold=lines_threshold,
            context_size_limit=context_size_limit
        )

        if iteration == 1:
            planning_prompt = f"""You are a master financial database researcher. Analyze the objective and the background context to generate an action plan.
Objective:
{objective}

Background Context:
{condensed_bg}

Rules:
1. Generate an action plan listing steps to retrieve necessary data.
2. The plan steps MUST use ONLY the tools: 'doc_context_retrieval' and 'tabular_data_retrieval'. Do not use any other tools.
3. DO NOT use, query, reference, or retrieve any data from the table 'loan_prediction_train'. It is completely excluded. Only search and query the 'loan_data' table or document context.
4. DO NOT request steps or query strings that select all data or retrieve the entire dataset in general. Each step must use a specific query with a specific objective (e.g., matching specific filters, retrieving specific columns, or focusing on a specific metric).
"""
        else:
            planning_prompt = f"""You are a master financial database researcher. Your previous analysis did not meet the confidence threshold.
Generate a NEW research action plan to retrieve additional information to address the remaining gaps described in the evaluation.

Objective:
{objective}

Background Context:
{condensed_bg}

Previous Accumulated Context:
{accumulated_context}

Previous Analysis & Recommendations:
{last_analysis}

Evaluation of Gaps / Explanations:
{evaluation_explanation}

Rules:
1. Generate an action plan listing steps to retrieve additional necessary data to close the gaps.
2. The plan steps MUST use ONLY the tools: 'doc_context_retrieval' and 'tabular_data_retrieval'. Do not use any other tools.
3. DO NOT use, query, reference, or retrieve any data from the table 'loan_prediction_train'. It is completely excluded. Only search and query the 'loan_data' table or document context.
4. DO NOT request steps or query strings that select all data or retrieve the entire dataset in general. Each step must use a specific query with a specific objective (e.g., matching specific filters, retrieving specific columns, or focusing on a specific metric).
"""
        agent_planner = LlmAgent(
            model=model,
            name=f"planner_iter_{iteration}",
            instruction="You are a data architect designing search action plans.",
            output_schema=ActionPlan
        )
        runner_planner = InMemoryRunner(agent=agent_planner)
        plan_sess = f"session_plan_{uuid.uuid4().hex}"
        await runner_planner.session_service.create_session(app_name=runner_planner.app_name, user_id="user", session_id=plan_sess)
        
        new_message = types.Content(parts=[types.Part(text=planning_prompt)])
        plan_events = []
        async for event in runner_planner.run_async(user_id="user", session_id=plan_sess, new_message=new_message):
            plan_events.append(event)
            if event.error_message:
                raise ValueError(f"Planning agent failed: {event.error_message}")

        plan_model_text = ""
        for event in reversed(plan_events):
            if event.content and event.content.role == 'model' and event.content.parts:
                plan_model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                break

        if not plan_model_text.strip() and plan_events and plan_events[-1].output:
            plan_obj = plan_events[-1].output
            plan_dict = plan_obj.model_dump()
        else:
            from app.agent import clean_json_text
            cleaned_json = clean_json_text(plan_model_text)
            from google.adk.utils._schema_utils import validate_schema
            plan_dict = validate_schema(ActionPlan, cleaned_json)

        action_plan_steps = plan_dict.get("steps", [])
        logger.info(f"Action plan generated with {len(action_plan_steps)} steps.")
        accumulated_action_plans.append(plan_dict)

        # Step 2: Execute Plan
        plan_results_text = []
        for step_idx, step in enumerate(action_plan_steps):
            tool = step["tool"]
            q_str = step["query_string"]
            logger.info(f"Executing Action Plan Step {step_idx+1}: {tool}('{q_str}')")

            retrieved_data = ""
            if tool == "doc_context_retrieval":
                retrieved_data = await doc_context_retrieval(
                    query_string=q_str,
                    ontology_folder=ontology_folder,
                    doc_db_path=doc_db_path,
                    top_n=top_n,
                    model=model
                )
            elif tool == "tabular_data_retrieval":
                retrieved_data = await tabular_data_retrieval(
                    query_string=q_str,
                    semantic_mapping_path=semantic_mapping_path,
                    db_path=mappings_db_path,
                    row_limit=row_limit,
                    max_sql_iterations=max_sql_iterations,
                    model=model
                )
            else:
                logger.warning(f"Unrecognized tool requested: {tool}")

            # Append retrieved context
            step_header = f"### Retrieved using {tool} for query: '{q_str}'\n"
            plan_results_text.append(step_header + retrieved_data)
            
            # Mark action item as done
            step["status"] = "done"

        # Update accumulated context
        new_retrieved = "\n\n".join(plan_results_text)
        if accumulated_context:
            accumulated_context += "\n\n" + new_retrieved
        else:
            accumulated_context = new_retrieved

        # Condense the accumulated context to make sure it won't exceed limit
        accumulated_context = await condense_summary(
            accumulated_context,
            model=model,
            lines_threshold=lines_threshold,
            context_size_limit=context_size_limit
        )

        # Step 3: Analysis
        analysis_prompt = f"""You are a principal financial data auditor. Your objective is:
{objective}

Here is the accumulated background, guidelines, and retrieved database context:
{accumulated_context}

Perform a comprehensive analysis and provide clear recommendations in Markdown format to achieve the objective.
"""
        agent_analysis = LlmAgent(
            model=model,
            name=f"analyst_iter_{iteration}",
            instruction="You are a principal auditor analyzing loan files."
        )
        runner_analysis = InMemoryRunner(agent=agent_analysis)
        analysis_sess = f"session_anal_{uuid.uuid4().hex}"
        await runner_analysis.session_service.create_session(app_name=runner_analysis.app_name, user_id="user", session_id=analysis_sess)
        
        new_message = types.Content(parts=[types.Part(text=analysis_prompt)])
        anal_events = []
        async for event in runner_analysis.run_async(user_id="user", session_id=analysis_sess, new_message=new_message):
            anal_events.append(event)
            if event.error_message:
                raise ValueError(f"Analysis agent failed: {event.error_message}")

        analysis_text = ""
        for event in reversed(anal_events):
            if event.content and event.content.role == 'model' and event.content.parts:
                analysis_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                break

        last_analysis = analysis_text
        accumulated_analysis.append(last_analysis)
        logger.info("Analysis and recommendations generated successfully.")

        # Step 4: Evaluation
        eval_prompt = f"""You are an independent quality assurance evaluator. Compare the generated analysis against the objective.
Objective:
{objective}

Generated Analysis and Recommendations:
{last_analysis}

Evaluate if the analysis fully achieves the objective. Rate your confidence from 0 to 100, and provide a detailed explanation justifying the score and listing any remaining gaps.
"""
        agent_eval = LlmAgent(
            model=model,
            name=f"evaluator_iter_{iteration}",
            instruction="You are an independent QA auditor scoring analytical reports.",
            output_schema=EvaluationResult
        )
        runner_eval = InMemoryRunner(agent=agent_eval)
        eval_sess = f"session_eval_{uuid.uuid4().hex}"
        await runner_eval.session_service.create_session(app_name=runner_eval.app_name, user_id="user", session_id=eval_sess)
        
        new_message = types.Content(parts=[types.Part(text=eval_prompt)])
        eval_events = []
        async for event in runner_eval.run_async(user_id="user", session_id=eval_sess, new_message=new_message):
            eval_events.append(event)
            if event.error_message:
                raise ValueError(f"Evaluation agent failed: {event.error_message}")

        eval_model_text = ""
        for event in reversed(eval_events):
            if event.content and event.content.role == 'model' and event.content.parts:
                eval_model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                break

        if not eval_model_text.strip() and eval_events and eval_events[-1].output:
            eval_obj = eval_events[-1].output
            eval_dict = eval_obj.model_dump()
        else:
            from app.agent import clean_json_text
            cleaned_json = clean_json_text(eval_model_text)
            from google.adk.utils._schema_utils import validate_schema
            eval_dict = validate_schema(EvaluationResult, cleaned_json)

        confidence_score = eval_dict.get("confidence_score", 0)
        evaluation_explanation = eval_dict.get("explanation", "")
        logger.info(f"Evaluation completed. Confidence Score: {confidence_score}/100. Explanation: {evaluation_explanation}")

        # Step 5: Conditional branch
        if confidence_score >= 90:
            logger.info(f"Confidence score {confidence_score} meets or exceeds target (90). Exiting loop.")
            break
        else:
            logger.info(f"Confidence score {confidence_score} < 90. Loop back to step 1 for iteration {iteration + 1}.")

    # Step 6: Report generation
    # Format action plan steps beautifully for markdown
    action_plans_md = ""
    for idx, plan in enumerate(accumulated_action_plans):
        action_plans_md += f"### Iteration {idx+1} Action Plan\n"
        for s in plan.get("steps", []):
            action_plans_md += f"- **Tool**: `{s.get('tool')}` | **Query**: `{s.get('query_string')}` | **Status**: `{s.get('status', 'done')}`\n  *Explanation*: {s.get('explanation')}\n"
        action_plans_md += "\n"

    report_content = f"""# Agentic Search Analysis Report

## Executive Summary
- **Objective**: {objective}
- **Confidence Score**: {confidence_score}/100
- **Total Iterations Completed**: {iteration}

## Evaluator QA Explanation
{evaluation_explanation}

## Analysis & Recommendations
{last_analysis}

## Accumulated Action Plans
{action_plans_md}
"""

    try:
        # Write report to markdown file
        os.makedirs(os.path.dirname(os.path.abspath(output_md_path)), exist_ok=True)
        with open(output_md_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        logger.info(f"Markdown report generated and saved at: {output_md_path}")
    except Exception as e:
        logger.error(f"Error saving report to markdown file: {e}")

    return {
        "status": "success",
        "iterations": iteration,
        "confidence_score": confidence_score,
        "explanation": evaluation_explanation,
        "report_path": output_md_path,
        "analysis": last_analysis
    }
