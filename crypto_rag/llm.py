"""OpenAI client factory and per-call usage/cost logging (rule 7)."""

import logging
from functools import lru_cache
from typing import Any

from openai import BadRequestError, OpenAI
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert

from crypto_rag.config import get_settings
from crypto_rag.db.models import LlmUsage

logger = logging.getLogger(__name__)


@lru_cache
def get_client() -> OpenAI:
    key = get_settings().openai_api_key.get_secret_value()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    return OpenAI(api_key=key, max_retries=5)


def cost_usd(model: str, input_tokens: int, output_tokens: int = 0) -> float | None:
    """Cost from the USD-per-1M-token table in settings; None if the model is not priced."""
    price = get_settings().llm_prices.get(model)
    if not price:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def log_usage(
    engine: Engine, purpose: str, model: str, input_tokens: int, output_tokens: int = 0
) -> None:
    cost = cost_usd(model, input_tokens, output_tokens)
    logger.info(
        "openai %s model=%s in=%d out=%d cost=%s",
        purpose,
        model,
        input_tokens,
        output_tokens,
        f"${cost:.5f}" if cost is not None else "n/a",
    )
    with engine.begin() as conn:
        conn.execute(
            insert(LlmUsage).values(
                purpose=purpose,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
            )
        )


def parse_structured(
    engine: Engine,
    *,
    purpose: str,
    model: str,
    instructions: str,
    user_input: str,
    schema: type,
    client: OpenAI | None = None,
) -> Any:
    """Structured output via the Responses API, at minimal temperature.

    Some reasoning models reject `temperature`; then the call is repeated
    without it and the fallback is logged.
    """
    client = client or get_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": user_input,
        "text_format": schema,
        "temperature": 0,
    }
    try:
        response = client.responses.parse(**kwargs)
    except BadRequestError as exc:
        if "temperature" not in str(exc):
            raise
        logger.warning("%s does not accept temperature; retrying without it", model)
        kwargs.pop("temperature")
        response = client.responses.parse(**kwargs)
    usage = response.usage
    log_usage(engine, purpose, model, usage.input_tokens if usage else 0,
              usage.output_tokens if usage else 0)  # fmt: skip
    if response.output_parsed is None:
        raise ValueError(f"{purpose}: model returned no parsable output")
    return response.output_parsed
