"""
app/config.py - Configuration management module for the Google ADK workflow engine.

This module loads variables from the environment (.env) and initializes the
LiteLlm model wrapper for custom OpenAI-compatible endpoints, or defaults to standard
models if no custom configuration is provided.
"""

import os
import logging
from dotenv import load_dotenv
from google.adk.models.lite_llm import LiteLlm

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

def get_llm_model() -> LiteLlm | str:
    """
    Creates and returns a LiteLlm model instance configured with the custom OpenAI-compatible
    endpoint from the environment. If no custom endpoint configuration is present,
    returns the default model identifier string ('gemini-3.5-flash').

    Returns:
        LiteLlm | str: LiteLlm wrapper instance or default model string.
    """
    custom_url = os.getenv("CUSTOM_LLM_URL")
    custom_model = os.getenv("CUSTOM_LLM_MODEL")
    custom_api_key = os.getenv("CUSTOM_LLM_API_KEY", "sk-dummy")

    logger.debug(f"Loading custom LLM config: URL={custom_url}, Model={custom_model}")

    if custom_url and custom_model:
        logger.info(f"Configuring custom OpenAI-compatible endpoint. Target model: {custom_model}")
        return LiteLlm(
            model=custom_model,
            api_base=custom_url,
            api_key=custom_api_key,
            custom_llm_provider="openai",
            extra_body={
                "chat_template_kwargs": {
                    "enable_thinking": True
                },
                "skip_special_tokens": False
            }
        )
    else:
        logger.info("Using default model configuration: gemini-3.5-flash")
        return "gemini-3.5-flash"


