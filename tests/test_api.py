import math
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from crypto_rag.agent.graph import AgentDeps
from crypto_rag.agent.state import RouterOut
from crypto_rag.api.main import app, deps_dep, engine_dep, split_sections
from tests.fakes import ScriptedChat
from tests.synthetic import T0, insert_candles, reset

AS_OF = T0 + timedelta(days=20)


def test_split_sections_maps_headings():
    answer = "## Що сталося\nA [m1]\n\n## Ймовірні причини\nB [c_1]\n\n## Прогноз\nC\n\nНе є фінансовою порадою."
    s = split_sections(answer)
    assert s["what_happened"] == "A [m1]"
    assert s["likely_causes"] == "B [c_1]"
    assert s["forecast"].startswith("C")


def test_openapi_docs_available():
    client = TestClient(app)
    assert client.get("/docs").status_code == 200
    assert "/ask" in client.get("/openapi.json").json()["paths"]


@pytest.fixture
def client(engine):
    reset(engine, "BTCUSDT")
    insert_candles(engine, "BTCUSDT", [100 + 5 * math.sin(i / 10) for i in range(24 * 25)])
    app.dependency_overrides[engine_dep] = lambda: engine
    yield TestClient(app)
    app.dependency_overrides.clear()


def _with_agent(engine, router_out, script):
    deps = AgentDeps(engine, ScriptedChat(structured=[router_out], script=[], seen=[]),
                     ScriptedChat(script=script, seen=[], structured=[]))  # fmt: skip
    app.dependency_overrides[deps_dep] = lambda: deps


@pytest.mark.integration
def test_ask_then_trace(engine, client):
    _with_agent(engine, RouterOut(intent="explain", asset="BTC"), [
        AIMessage(content="", tool_calls=[{"name": "get_market_stats", "args": {}, "id": "c1"}]),
        AIMessage(content="## Що сталося\nBTC moved over the last 24 h.\n\nНе є фінансовою порадою."),
    ])  # fmt: skip
    r = client.post("/ask", json={"question": "What happened to BTC?", "as_of": AS_OF.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verification"]["ok"] and body["intent"] == "explain"
    assert "Не є фінансовою порадою" in body["answer"]
    assert body["sections"]["what_happened"].startswith("BTC moved")
    assert body["metrics"] == []
    t = client.get(f"/trace/{body['query_id']}")
    assert t.status_code == 200
    tr = t.json()
    assert tr["routing"]["output"]["intent"] == "explain"
    assert tr["tool_calls"][0]["name"] == "get_market_stats"
    assert any(e["event"] == "verify" for e in tr["attempts"])


@pytest.mark.integration
def test_ask_rejects_future_as_of(client):
    _with_agent(None, RouterOut(intent="explain"), [])
    r = client.post("/ask", json={"question": "BTC?", "as_of": "2099-01-01T00:00:00Z"})
    assert r.status_code == 422


@pytest.mark.integration
def test_trace_404(client):
    assert client.get("/trace/999999").status_code == 404


@pytest.mark.integration
def test_prices_bounded_by_as_of(client):
    r = client.get(
        "/prices",
        params={
            "asset": "BTC",
            "start": T0.isoformat(),
            "as_of": (T0 + timedelta(hours=5)).isoformat(),
        },
    )
    assert r.status_code == 200
    assert len(r.json()) == 5


@pytest.mark.integration
def test_health_and_stats(client):
    h = client.get("/health").json()
    assert h["database"] == "ok" and "openai_configured" in h
    s = client.get("/stats")
    assert s.status_code == 200
    assert {"data", "topic_reaction", "forecasts", "llm_usage"} <= set(s.json())


def test_ask_without_key_is_503(monkeypatch):
    from pydantic import SecretStr

    from crypto_rag.config import get_settings

    monkeypatch.setattr(get_settings(), "openai_api_key", SecretStr(""))
    client = TestClient(app)
    assert client.post("/ask", json={"question": "BTC?"}).status_code == 503
