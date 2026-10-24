import math
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import text

from crypto_rag.agent.graph import AgentDeps, run_query
from crypto_rag.agent.state import RouterOut
from tests.fakes import ScriptedChat
from tests.synthetic import T0, insert_candles, reset

pytestmark = pytest.mark.integration
AS_OF = T0 + timedelta(days=20)


def call(name, args, i=1):
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}_{i}"}]
    )


@pytest.fixture
def market(engine):
    reset(engine, "BTCUSDT")
    closes = [100 + 5 * math.sin(i / 10) for i in range(24 * 25)]
    insert_candles(engine, "BTCUSDT", closes)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM query_log"))


def _deps(engine, router_out, script):
    router = ScriptedChat(structured=[router_out], script=[], seen=[])
    gen = ScriptedChat(script=script, seen=[], structured=[])
    return AgentDeps(engine, router, gen), gen


def _answer_from(state_ids_value: str) -> str:
    return f"## Що сталося\nBTC changed {state_ids_value} [m3] over the window.\n\nНе є фінансовою порадою."


def test_explain_question_end_to_end(engine, market):
    # m3 = price_change_pct; compute expected display through the tool itself.
    from crypto_rag.tools.market import get_market_stats

    expected = next(m for m in get_market_stats(engine, "BTCUSDT", AS_OF - timedelta(hours=24), AS_OF, AS_OF)
                    if m.name == "price_change_pct")  # fmt: skip
    deps, _ = _deps(engine, RouterOut(intent="explain", asset="BTC"), [
        call("get_market_stats", {"window_hours": 24}),
        AIMessage(content=_answer_from(f"{abs(expected.value):.2f}%")),
    ])  # fmt: skip
    out = run_query(deps, "Why did BTC move today?", as_of=AS_OF)
    assert out["verification"]["ok"], out["verification"]
    assert out["answer"].endswith("Не є фінансовою порадою.")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT intent, trace, tool_calls FROM query_log WHERE id = :i"),
            {"i": out["query_id"]},
        ).one()
    assert row.intent == "explain"
    assert [e["event"] for e in row.trace["events"]] == ["router", "generate", "verify"]


def test_model_supplied_date_is_ignored(engine, market):
    deps, _ = _deps(engine, RouterOut(intent="explain"), [
        call("get_market_stats", {"window_hours": 24, "end": "2031-01-01T00:00:00Z", "as_of": "2031-01-01"}),
        AIMessage(content="Nothing to report.\n\nНе є фінансовою порадою."),
    ])  # fmt: skip
    out = run_query(deps, "What happened?", as_of=AS_OF)
    (tc,) = out["tool_calls"]
    assert tc["as_of_used"] == AS_OF.isoformat()
    assert tc["ignored_model_as_of"] == "2031-01-01"
    stats = out["tool_results"]["get_market_stats"][0]
    end_price = next(m for m in stats if m["name"] == "price_end")
    assert "2031" not in end_price["description"]


def test_forecast_tool_hidden_for_explain_intent(engine, market):
    deps, gen = _deps(engine, RouterOut(intent="explain"), [
        call("get_forecast", {}),
        AIMessage(content="No forecast.\n\nНе є фінансовою порадою."),
    ])  # fmt: skip
    out = run_query(deps, "Explain BTC", as_of=AS_OF)
    assert "get_forecast" not in gen.seen[0][0]["tools"]
    assert "not available" in out["tool_calls"][0]["error"]


def test_unsupported_is_refused_without_generation(engine, market):
    deps, gen = _deps(engine, RouterOut(intent="unsupported"), [])
    out = run_query(deps, "Should I buy Dogecoin?", as_of=AS_OF)
    assert out["refused"] and "BTC та ETH" in out["answer"]
    assert gen.seen == []


def test_failed_verification_retries_then_refuses(engine, market):
    bad = AIMessage(content="BTC rose 99.99% [m1].")
    deps, _ = _deps(engine, RouterOut(intent="mixed"), [call("get_market_stats", {}), bad, bad])
    out = run_query(deps, "BTC outlook?", as_of=AS_OF)
    assert out["attempts"] == 2
    assert out["refused"]
    assert [e["event"] for e in out["trace"]] == [
        "router",
        "generate",
        "verify",
        "generate",
        "verify",
        "refuse",
    ]


def test_retry_succeeds_after_feedback(engine, market):
    deps, gen = _deps(engine, RouterOut(intent="explain"), [
        call("get_market_stats", {}),
        AIMessage(content="BTC rose 99.99% [m1]."),
        AIMessage(content="BTC moved over the window.\n\nНе є фінансовою порадою."),
    ])  # fmt: skip
    out = run_query(deps, "BTC?", as_of=AS_OF)
    assert out["verification"]["ok"] and not out.get("refused")
    retry_prompt = gen.seen[-1][-1].content
    assert "99.99%" in retry_prompt and "does not match" in retry_prompt
