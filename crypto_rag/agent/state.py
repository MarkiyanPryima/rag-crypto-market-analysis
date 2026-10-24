"""LangGraph state for one question.

as_of is set by code (API request or "now") and is never read from model
output: tool executors take it from here and ignore any date the model sends.
"""

from datetime import datetime
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

Intent = Literal["explain", "forecast", "mixed", "unsupported"]


class RouterOut(BaseModel):
    intent: Intent = Field(description="explain: why the price moved / what happened; "
                           "forecast: what is expected next; mixed: both; unsupported: anything else")  # fmt: skip
    asset: Literal["BTC", "ETH"] | None = Field(
        default=None, description="Asset named in the question, if any"
    )
    window_hours: int | None = Field(
        default=None, ge=1, le=24 * 90, description="Time window named in the question, in hours"
    )
    reason: str = Field(default="", description="One short sentence explaining the choice")


class AgentState(TypedDict, total=False):
    question: str
    asset: str | None
    asset_locked: bool
    as_of: datetime
    window_hours: int
    intent: Intent
    tool_results: dict[str, list[Any]]
    tool_calls: list[dict[str, Any]]
    id_counters: dict[str, int]
    draft_answer: str
    answer: str
    verification: dict[str, Any]
    attempts: int
    refused: bool
    trace: list[dict[str, Any]]
