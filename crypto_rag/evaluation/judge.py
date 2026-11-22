"""Faithfulness of answers to their context, judged by a stronger model (stage 17).

Follows the RAGAS faithfulness metric: the judge splits the answer into atomic
claims and decides for each whether the context supports it; the score is the share
of supported claims. The context is exactly what the generator saw: the metrics and
news returned by its tool calls (read back from query_log). The judge model
(settings.judge_model) differs from, and is stronger than, the generator.

Answers without context (configuration "no_retrieval") and refusals are not judged.
To fit the budget, only the annotated subset of questions is judged (20 of 41, see
runs.annotation_questions), so faithfulness and its manual check share questions.

Usage:
    python -m crypto_rag.evaluation.judge [--workers 4]
"""

import argparse
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import Engine, text

from crypto_rag.agent.prompts import DISCLAIMER
from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.session import get_engine
from crypto_rag.evaluation.runs import (
    ANSWERS,
    DEFAULT_BUDGET_USD,
    RESULTS,
    Budget,
    annotation_questions,
    read_jsonl,
)
from crypto_rag.llm import log_usage
from crypto_rag.verification.verify import collect_metrics, collect_sources

logger = logging.getLogger(__name__)

JUDGEMENTS = RESULTS / "judgements.jsonl"
# Within the 2026-11 budget only the two configurations with numeric tools are judged
# (news_only would add ~$0.9); add "news_only" here to judge it as well.
JUDGED_CONFIGS = ("news_tools", "full")
CHUNK_CHARS = 1500

JUDGE_PROMPT = """\
You check whether an answer about the crypto market is faithful to its CONTEXT.
1. Split the ANSWER into atomic factual claims: one fact per claim, self-contained
   (resolve pronouns), in the answer's language. Skip headings, the disclaimer, and
   pure hedging or advice that states no fact.
2. For each claim decide `supported`: true only if the CONTEXT states it or it follows
   directly from it (numbers must match at the stated rounding; a claim that two events
   coincided in time is supported if both are in the CONTEXT with matching times).
   Background knowledge does not count: a true fact absent from the CONTEXT is unsupported.
3. List the CONTEXT ids ([m…], [f…], [c_…]) that support each supported claim.
"""


class Claim(BaseModel):
    claim: str = Field(description="One atomic factual statement from the answer")
    supported: bool = Field(description="True only if the CONTEXT states it or it follows directly")
    evidence: list[str] = Field(default_factory=list, description="Supporting CONTEXT ids")


class Judgement(BaseModel):
    claims: list[Claim]


def context_text(tool_results: dict[str, Any]) -> str:
    lines = []
    for mid, m in collect_metrics(tool_results).items():
        when = m.get("extra", {}).get("ts") or m.get("extra", {}).get("date") or ""
        lines.append(f"[{mid}] {m['description']}: {m['display']} {when}".rstrip())
    for cid, c in collect_sources(tool_results).items():
        body = (c.get("text") or "")[:CHUNK_CHARS]
        lines.append(
            f"[{cid}] {c.get('source')}, published {c['published_at']}: {c.get('title')}\n{body}"
        )
    return "\n".join(lines) if lines else "(empty)"


def strip_disclaimer(answer: str) -> str:
    return answer.replace(DISCLAIMER, "").strip()


def tool_results_of(engine: Engine, query_id: int) -> dict[str, Any]:
    with engine.connect() as conn:
        trace = conn.execute(
            text("SELECT trace FROM query_log WHERE id = :i"), {"i": query_id}
        ).scalar_one()
    return trace.get("tool_results", {})


def judge_answer(engine: Engine, llm: Any, answer: str, context: str) -> Judgement:
    structured = llm.with_structured_output(Judgement, include_raw=True)
    out = structured.invoke([
        ("system", JUDGE_PROMPT),
        ("human", f"CONTEXT:\n{context}\n\nANSWER:\n{strip_disclaimer(answer)}"),
    ])  # fmt: skip
    usage = getattr(out.get("raw"), "usage_metadata", None) or {}
    log_usage(engine, "judge", get_settings().judge_model,
              usage.get("input_tokens", 0), usage.get("output_tokens", 0))  # fmt: skip
    if out["parsed"] is None:
        raise ValueError(f"judge output unparsable: {out.get('parsing_error')}")
    return out["parsed"]


def faithfulness(j: Judgement) -> float | None:
    return sum(c.supported for c in j.claims) / len(j.claims) if j.claims else None


def run(engine: Engine, workers: int = 4, budget_usd: float = DEFAULT_BUDGET_USD) -> int:
    from langchain_openai import ChatOpenAI

    s = get_settings()
    llm = ChatOpenAI(
        model=s.judge_model, api_key=s.openai_api_key.get_secret_value(),
        temperature=s.llm_temperature, max_retries=3, use_responses_api=True,
    )  # fmt: skip
    done = {(r["question_id"], r["config"]) for r in read_jsonl(JUDGEMENTS)}
    subset = {q["id"] for q in annotation_questions()}
    todo = [
        a for a in read_jsonl(ANSWERS)
        if a["config"] in JUDGED_CONFIGS and not a["refused"] and a["question_id"] in subset
        and (a["question_id"], a["config"]) not in done
    ]  # fmt: skip
    lock = threading.Lock()
    budget = Budget(engine, budget_usd)

    def work(a: dict[str, Any]) -> None:
        if budget.exhausted():
            return
        try:
            j = judge_answer(
                engine, llm, a["answer"], context_text(tool_results_of(engine, a["query_id"]))
            )
        except Exception:
            logger.exception(
                "judge failed for %s / %s; rerun to retry", a["question_id"], a["config"]
            )
            return
        record = {"question_id": a["question_id"], "config": a["config"], "query_id": a["query_id"],
                  "faithfulness": faithfulness(j), "claims": [c.model_dump() for c in j.claims]}  # fmt: skip
        with lock, JUDGEMENTS.open("a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, todo))
    return len(todo)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    args = parser.parse_args()
    logger.info("judged %d answers", run(get_engine(), args.workers, args.budget_usd))


if __name__ == "__main__":
    main()
