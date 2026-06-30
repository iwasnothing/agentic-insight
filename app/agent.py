"""
app/agent.py - Google ADK agent execution module.

This module sets up the prompt template, defines the target Pydantic structures for
mappings, and invokes the ADK Agent (configured with custom endpoints if any).
"""

import os
import logging
import asyncio
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from app.config import get_llm_model

logger = logging.getLogger(__name__)

# Pydantic schemas to enforce structured JSON output
class AttributeMapping(BaseModel):
    attribute_name: str = Field(
        description="The name or URI of the ontology attribute (property)"
    )
    mapped_columns: List[str] = Field(
        description="List of dataset columns mapped to this attribute"
    )
    sql_formula: Optional[str] = Field(
        default=None,
        description="DuckDB SQL expression/formula to calculate the attribute from mapped columns. E.g. 'log.annual.inc * 12'. Null if 1-to-1 or not applicable."
    )
    not_enough_information: bool = Field(
        description="True if there is not enough information in columns to map this attribute"
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Explanation justifying this mapping or explaining lack of info"
    )

class ClassMapping(BaseModel):
    class_name: str = Field(description="The local name of the ontology class")
    class_uri: str = Field(description="The full URI of the ontology class")
    source_ttl_file: str = Field(description="The name of the source TTL file")
    attribute_mappings: List[AttributeMapping] = Field(
        description="List of attribute-level mappings for this class"
    )

def load_prompt_template() -> str:
    """
    Loads the prompt template file.

    Returns:
        str: Prompt text content.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(current_dir, "prompt", "mapping_prompt.txt")
    
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(f"Externalized prompt not found at {prompt_path}")
        
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()

def clean_json_text(text: str) -> str:
    """
    Removes potential markdown code block formatting (like ```json ... ```).
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            if lines[-1].strip() == "```":
                return "\n".join(lines[1:-1]).strip()
    return text

async def _async_map_class_to_columns(class_info: Dict[str, Any], formatted_columns: str) -> Dict[str, Any]:
    """
    Asynchronously invokes the ADK Agent to map class properties to CSV headers.

    Args:
        class_info (Dict[str, Any]): Dictionary of class properties/metadata.
        formatted_columns (str): String listing CSV columns.

    Returns:
        Dict[str, Any]: Mapping output matching ClassMapping schema.
    """
    prompt_template = load_prompt_template()
    
    props = class_info.get("properties", [])
    attributes_str = "\n".join([f"- {p}" for p in props]) if props else "- (No attributes defined)"
    
    prompt = prompt_template.format(
        class_name=class_info["class_name"],
        class_uri=class_info["class_uri"],
        class_description=class_info.get("definition") or class_info.get("label", ""),
        attributes_list=attributes_str,
        dataset_columns=formatted_columns
    )
    
    model = get_llm_model()
    
    agent = LlmAgent(
        model=model,
        name=f"mapping_agent_{class_info['class_name']}",
        instruction="You are a data architect mapping database columns to ontology classes.",
        output_schema=ClassMapping
    )
    
    runner = InMemoryRunner(agent=agent)
    
    import uuid
    session_id = f"session_{uuid.uuid4().hex}"
    
    logger.info(f"Creating runner session context for class: {class_info['class_name']}")
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="user",
        session_id=session_id
    )
    
    new_message = types.Content(parts=[types.Part(text=prompt)])
    
    logger.info(f"Sending mapping request to LLM for class: {class_info['class_name']}")
    events = []
    async for event in runner.run_async(user_id="user", session_id=session_id, new_message=new_message):
        events.append(event)
        if event.error_message:
            msg = f"LLM returned an error during mapping for class {class_info['class_name']}: {event.error_message}"
            logger.error(msg)
            raise ValueError(msg)
            
    if not events:
        msg = f"No events returned from agent mapping for class {class_info['class_name']}"
        logger.error(msg)
        raise ValueError(msg)
        
    # Extract response text from model events (ignoring thinking block)
    model_text = ""
    for event in reversed(events):
        if event.content and event.content.role == 'model':
            if event.content.parts:
                model_text = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
            break
            
    # Fallback to output directly if no text content was matched
    if not model_text.strip():
        last_event = events[-1]
        if last_event.output:
            result = last_event.output
            if hasattr(result, "model_dump"):
                result_dict = result.model_dump()
            elif hasattr(result, "dict"):
                result_dict = result.dict()
            elif isinstance(result, dict):
                result_dict = result
            else:
                result_dict = dict(result)
            result_dict["source_ttl_file"] = class_info["source_file"]
            return result_dict

    if not model_text.strip():
        msg = f"LLM did not return structured output for class {class_info['class_name']}"
        logger.error(msg)
        raise ValueError(msg)
        
    cleaned_json = clean_json_text(model_text)
    
    try:
        from google.adk.utils._schema_utils import validate_schema
        result_dict = validate_schema(ClassMapping, cleaned_json)
    except Exception as e:
        msg = f"Failed to validate schema for class {class_info['class_name']}: {e}"
        logger.exception(msg)
        raise ValueError(msg) from e
        
    logger.info(f"Received structured response for class: {class_info['class_name']}")
    
    # Guarantee correct source file path is used, avoiding LLM hallucinations
    result_dict["source_ttl_file"] = class_info["source_file"]
    return result_dict

def run_mapping_agent(class_info: Dict[str, Any], formatted_columns: str) -> Dict[str, Any]:
    """
    Thread-safe synchronous wrapper around async agent call.

    Args:
        class_info (Dict[str, Any]): Dictionary of class properties/metadata.
        formatted_columns (str): String listing CSV columns.

    Returns:
        Dict[str, Any]: Mapping output matching ClassMapping schema.
    """
    logger.info(f"Worker thread starting mapping for: {class_info['class_name']}")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(_async_map_class_to_columns(class_info, formatted_columns))
        return result
    except Exception as e:
        logger.exception(f"Error mapping class {class_info.get('class_name')}: {e}")
        raise e
    finally:
        loop.close()
