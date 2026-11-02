"""Query graph: router -> generate (tool loop) -> verify -> END | generate | refuse.

    router ──unsupported──> refuse
       └──> generate ──> verify ──ok──> END
               ^            │
               └─errors & attempts < 2
                            └─otherwise──> refuse

Tools never receive as_of from the model: the executor injects it from state
and clamps any model-supplied end time to it.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from sqlalchemy import Engine, insert

from crypto_rag.agent.prompts import (
    DISCLAIMER,
    FORECAST_HINT,
    GENERATOR_PROMPT,
    NO_FORECAST_HINT,
    RETRY_PROMPT,
    ROUTER_PROMPT,
)
from crypto_rag.agent.state import AgentState, RouterOut
from crypto_rag.db.models import QueryLog
from crypto_rag.llm import log_usage
from crypto_rag.processing.assets import ASSET_SYMBOL
from crypto_rag.retrieval.hybrid import QueryEmbedder, search_news, search_news_around
from crypto_rag.tools.base import IdAllocator
from crypto_rag.tools.episodes import find_similar_episodes
from crypto_rag.tools.forecast import get_forecast
from crypto_rag.tools.market import get_market_stats
from crypto_rag.verification.verify import verify

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
MAX_TOOL_ROUNDS = 8
DEFAULT_WINDOW_HOURS = 24
AssetArg = Literal["BTC", "ETH"] | None


# --- Tool schemas (what the model sees; as_of is deliberately absent) -------


class get_market_stats_tool(BaseModel):
    """Price change, sharpest hourly move, volatility, volume vs 30-day average,
    aggressive-buy share, funding and comparison with the past year, for a window."""

    window_hours: int = Field(default=DEFAULT_WINDOW_HOURS, ge=1, le=24 * 90)
    end: str | None = Field(default=None, description="Window end, ISO UTC; defaults to now")
    asset: AssetArg = None


class search_news_tool(BaseModel):
    """Hybrid search over news chunks published in the last window_hours."""

    query: str
    window_hours: int = Field(default=72, ge=1, le=24 * 90)
    asset: AssetArg = None


class news_around_move_tool(BaseModel):
    """News published within ±hours of a moment, e.g. the sharpest hourly move."""

    at: str = Field(description="ISO UTC timestamp of the move")
    hours: int = Field(default=6, ge=1, le=48)
    asset: AssetArg = None


class find_similar_episodes_tool(BaseModel):
    """Past 48-hour windows whose price shape is most similar to the last 48 hours."""

    k: int = Field(default=3, ge=1, le=5)
    asset: AssetArg = None


class get_forecast_tool(BaseModel):
    """Latest model forecast: probability of a 7-day rise and next-day volatility."""

    asset: AssetArg = None


TOOL_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_market_stats": get_market_stats_tool,
    "search_news": search_news_tool,
    "news_around_move": news_around_move_tool,
    "find_similar_episodes": find_similar_episodes_tool,
    "get_forecast": get_forecast_tool,
}


def _tool_spec(name: str, schema: type[BaseModel]) -> dict[str, Any]:
    params = schema.model_json_schema()
    params.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": (schema.__doc__ or "").strip(),
            "parameters": params,
        },
    }


def tools_for_intent(intent: str) -> list[dict[str, Any]]:
    return [
        _tool_spec(n, s)
        for n, s in TOOL_SCHEMAS.items()
        if n != "get_forecast" or intent in ("forecast", "mixed")
    ]


# --- Tool execution ----------------------------------------------------------


def _parse_ts(value: str | None, as_of: datetime) -> datetime:
    if not value:
        return as_of
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return min(ts, as_of)


@dataclass
class ToolExecutor:
    engine: Engine
    as_of: datetime
    default_asset: str
    allowed: set[str]
    ids: IdAllocator
    embed_query: QueryEmbedder | None = None
    locked_asset: bool = False

    def _asset(self, requested: str | None) -> str:
        return self.default_asset if self.locked_asset or not requested else requested

    def run(self, name: str, raw_args: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        if name not in self.allowed:
            raise ValueError(f"tool {name} is not available for this question")
        note: dict[str, Any] = {}
        if "as_of" in raw_args:
            note["ignored_model_as_of"] = raw_args["as_of"]
        args = TOOL_SCHEMAS[name].model_validate(raw_args)
        asset = self._asset(getattr(args, "asset", None))
        symbol = ASSET_SYMBOL[asset]
        if name == "get_market_stats":
            end = _parse_ts(args.end, self.as_of)
            result = [
                m.to_dict()
                for m in get_market_stats(
                    self.engine,
                    symbol,
                    end - timedelta(hours=args.window_hours),
                    end,
                    self.as_of,
                    self.ids,
                )
            ]
        elif name == "search_news":
            hits = search_news(
                self.engine,
                args.query,
                self.as_of,
                asset,
                args.window_hours,
                embed_query=self.embed_query,
                ids=self.ids,
            )
            result = [h.to_dict() for h in hits]
        elif name == "news_around_move":
            result = search_news_around(
                self.engine,
                _parse_ts(args.at, self.as_of),
                self.as_of,
                asset,
                args.hours,
                ids=self.ids,
            )
        elif name == "find_similar_episodes":
            result = find_similar_episodes(self.engine, symbol, self.as_of, args.k, self.ids)
        else:
            result = [m.to_dict() for m in get_forecast(self.engine, symbol, self.as_of, self.ids)]
        note.update({"asset": asset, "as_of_used": self.as_of.isoformat()})
        return result, note


# --- Graph ---------------------------------------------------------------------


@dataclass
class AgentDeps:
    engine: Engine
    router_llm: BaseChatModel
    generator_llm: BaseChatModel
    embed_query: QueryEmbedder | None = None
    router_model_name: str = "router"
    generator_model_name: str = "generator"


def _log_llm(deps: AgentDeps, purpose: str, model: str, message: Any) -> None:
    usage = getattr(message, "usage_metadata", None) or {}
    if usage:
        log_usage(
            deps.engine, purpose, model, usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        )


def _trace(state: AgentState, event: str, **data: Any) -> list[dict[str, Any]]:
    return [*state.get("trace", []), {"event": event, "at": datetime.now(UTC).isoformat(), **data}]


# (language, unsupported?) -> text. The disclaimer is appended in Ukrainian in every case.
REFUSALS = {
    ("uk", True): (
        "Я можу пояснювати рухи ціни BTC та ETH, показувати схожі епізоди з історії "
        "та денні прогнози волатильності й імовірності зростання. Це запитання виходить "
        "за ці межі. Спробуйте, наприклад: «Чому BTC впав за останню добу?» або "
        "«Який прогноз волатильності ETH на завтра?»"
    ),
    ("en", True): (
        "I can explain BTC and ETH price moves, show similar episodes from history, and give "
        "daily forecasts of volatility and the chance of a rise. This question is outside that "
        'scope. Try, for example: "Why did BTC fall over the last 24 hours?" or '
        '"What is the volatility forecast for ETH tomorrow?"'
    ),
    ("uk", False): (
        "Не вдалося сформувати відповідь, що проходить автоматичну перевірку фактів, "
        "тож я її не показую. Проблеми:\n{feedback}\n\n"
        "Спробуйте звузити запитання (один актив, конкретне вікно часу)."
    ),
    ("en", False): (
        "I could not produce an answer that passes the automatic fact check, so I am not "
        "showing it. Problems:\n{feedback}\n\n"
        "Try narrowing the question (one asset, a specific time window)."
    ),
}


def build_graph(deps: AgentDeps) -> Any:
    def router(state: AgentState) -> dict[str, Any]:
        structured = deps.router_llm.with_structured_output(RouterOut, include_raw=True)
        out = structured.invoke([SystemMessage(ROUTER_PROMPT), HumanMessage(state["question"])])
        _log_llm(deps, "router", deps.router_model_name, out.get("raw"))
        parsed: RouterOut = out["parsed"] or RouterOut(
            intent="unsupported", reason="router output unparsable"
        )
        asset = state.get("asset") or parsed.asset or "BTC"
        window = parsed.window_hours or state.get("window_hours") or DEFAULT_WINDOW_HOURS
        return {
            "intent": parsed.intent,
            "asset": asset,
            "window_hours": window,
            "trace": _trace(
                state, "router", output=parsed.model_dump(), asset_used=asset, window_hours=window
            ),
        }

    def generate(state: AgentState) -> dict[str, Any]:
        ids = IdAllocator()
        ids._counters = dict(state.get("id_counters", {}))
        tools = tools_for_intent(state["intent"])
        executor = ToolExecutor(
            deps.engine,
            state["as_of"],
            state["asset"],
            {t["function"]["name"] for t in tools},
            ids,
            deps.embed_query,
            locked_asset=bool(state.get("asset_locked")),
        )
        system = GENERATOR_PROMPT.format(
            as_of=f"{state['as_of']:%Y-%m-%d %H:%M}",
            asset=state["asset"],
            intent=state["intent"],
            window_hours=state["window_hours"],
            disclaimer=DISCLAIMER,
            forecast_hint=FORECAST_HINT
            if state["intent"] in ("forecast", "mixed")
            else NO_FORECAST_HINT,
        )
        messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(state["question"])]
        if state.get("attempts", 0) > 0:
            messages.append(
                HumanMessage(
                    "Tool results already available (ids are valid):\n"
                    + json.dumps(state.get("tool_results", {}), default=str)[:60000]
                    + "\n\n"
                    + RETRY_PROMPT.format(
                        feedback=state["verification"].get("feedback", ""),
                        draft=state.get("draft_answer", ""),
                    )
                )
            )
        llm = deps.generator_llm.bind_tools(tools)
        tool_results = {k: list(v) for k, v in state.get("tool_results", {}).items()}
        tool_calls = list(state.get("tool_calls", []))
        draft = ""
        for _ in range(MAX_TOOL_ROUNDS):
            ai: AIMessage = llm.invoke(messages)
            _log_llm(deps, "generate", deps.generator_model_name, ai)
            messages.append(ai)
            if not ai.tool_calls:
                # Responses API returns content as a list of blocks; .text joins the text parts.
                draft = ai.text
                break
            for call in ai.tool_calls:
                started = time.perf_counter()
                try:
                    result, note = executor.run(call["name"], call["args"])
                    content = json.dumps(result, default=str)
                except Exception as exc:  # reported back to the model, not raised
                    result, note = None, {"error": str(exc)}
                    content = json.dumps({"error": str(exc)})
                if result is not None:
                    tool_results.setdefault(call["name"], []).append(result)
                tool_calls.append(
                    {
                        "name": call["name"],
                        "args": call["args"],
                        **note,
                        "ms": round((time.perf_counter() - started) * 1000),
                    }
                )
                messages.append(ToolMessage(content=content, tool_call_id=call["id"]))
        return {
            "draft_answer": draft,
            "tool_results": tool_results,
            "tool_calls": tool_calls,
            "id_counters": dict(ids._counters),
            "attempts": state.get("attempts", 0) + 1,
            "trace": _trace(state, "generate", attempt=state.get("attempts", 0) + 1, draft=draft),
        }

    def verify_node(state: AgentState) -> dict[str, Any]:
        result = verify(
            state.get("draft_answer", ""), state.get("tool_results", {}), state["as_of"]
        )
        if not state.get("draft_answer", "").strip():
            result.ok = False
        verification = {**result.to_dict(), "feedback": result.feedback() or "empty answer"}
        update: dict[str, Any] = {
            "verification": verification,
            "trace": _trace(state, "verify", attempt=state.get("attempts", 0), **result.to_dict()),
        }
        if result.ok:
            answer = state["draft_answer"].rstrip()
            if DISCLAIMER not in answer:
                answer += f"\n\n{DISCLAIMER}"
            update["answer"] = answer
        return update

    def refuse(state: AgentState) -> dict[str, Any]:
        ukrainian = bool(re.search(r"[А-Яа-яІіЇїЄєҐґ]", state.get("question", "")))
        feedback = state.get("verification", {}).get("feedback", "")
        text = REFUSALS[("uk" if ukrainian else "en", state.get("intent") == "unsupported")]
        text = text.format(feedback=feedback)
        return {
            "answer": f"{text}\n\n{DISCLAIMER}",
            "refused": True,
            "trace": _trace(state, "refuse", intent=state.get("intent")),
        }

    def after_router(state: AgentState) -> str:
        return "refuse" if state["intent"] == "unsupported" else "generate"

    def after_verify(state: AgentState) -> str:
        if state["verification"]["ok"]:
            return END
        return "generate" if state.get("attempts", 0) < MAX_ATTEMPTS else "refuse"

    g = StateGraph(AgentState)
    g.add_node("router", router)
    g.add_node("generate", generate)
    g.add_node("verify", verify_node)
    g.add_node("refuse", refuse)
    g.add_edge(START, "router")
    g.add_conditional_edges("router", after_router, ["refuse", "generate"])
    g.add_edge("generate", "verify")
    g.add_conditional_edges("verify", after_verify, [END, "generate", "refuse"])
    g.add_edge("refuse", END)
    return g.compile()


def run_query(
    deps: AgentDeps,
    question: str,
    asset: str | None = None,
    as_of: datetime | None = None,
    graph_factory: Callable[[AgentDeps], Any] = build_graph,
    window_hours: int | None = None,
) -> dict[str, Any]:
    """Run the graph and persist the full trace to query_log. Returns state + query id."""
    as_of = as_of or datetime.now(UTC)
    graph = graph_factory(deps)
    state: AgentState = graph.invoke(
        {
            "question": question,
            "asset": asset,
            "asset_locked": bool(asset),
            "as_of": as_of,
            **({"window_hours": window_hours} if window_hours else {}),
            "attempts": 0,
            "trace": [],
        }
    )
    retrieved = [
        h
        for k in ("search_news", "news_around_move")
        for call in state.get("tool_results", {}).get(k, [])
        for h in call
    ]
    with deps.engine.begin() as conn:
        query_id = conn.execute(
            insert(QueryLog)
            .values(
                question=question,
                asset=state.get("asset"),
                as_of=as_of,
                intent=state.get("intent"),
                tool_calls=state.get("tool_calls", []),
                retrieved=json.loads(json.dumps(retrieved, default=str)),
                answer=state.get("answer"),
                verification=state.get("verification", {}),
                trace=json.loads(
                    json.dumps(
                        {
                            "events": state.get("trace", []),
                            "tool_results": state.get("tool_results", {}),
                        },
                        default=str,
                    )
                ),
            )
            .returning(QueryLog.id)
        ).scalar_one()
    return {**state, "query_id": query_id}


def default_deps(engine: Engine) -> AgentDeps:
    from langchain_openai import ChatOpenAI

    from crypto_rag.config import get_settings
    from crypto_rag.retrieval.hybrid import openai_query_embedder

    s = get_settings()
    key = s.openai_api_key.get_secret_value()
    # Responses API: the gpt-5.6 models refuse function tools on /v1/chat/completions.
    router = ChatOpenAI(
        model=s.router_model,
        api_key=key,
        temperature=s.llm_temperature,
        max_retries=3,
        use_responses_api=True,
    )
    generator = ChatOpenAI(
        model=s.generator_model,
        api_key=key,
        temperature=s.llm_temperature,
        max_retries=3,
        use_responses_api=True,
    )
    return AgentDeps(
        engine, router, generator, openai_query_embedder(engine), s.router_model, s.generator_model
    )
