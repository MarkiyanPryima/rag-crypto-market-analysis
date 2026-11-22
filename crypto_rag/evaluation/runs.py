"""Stage 17: answers of the four configurations and deterministic retrieval runs.

answers   — every question through every configuration of the graph (CONFIGS in
            agent/graph.py): no retrieval / news only / news + numeric tools / full
            system (recency decay + verification). The asset is fixed from the
            question and the window is 24 h, as in the question text.
retrieval — search_news with the question text as the query, 72 h before as_of,
            top-k, for several decay half-lives T½ (math.inf = no decay). No model
            is involved, so this isolates the effect of T½ on retrieval.

Both outputs are append-only JSONL, so an interrupted run resumes where it stopped.
Paid runs stop starting new work once the evaluation's spend since the run began
(table llm_usage) exceeds --budget-usd.

Usage:
    python -m crypto_rag.evaluation.runs answers [--configs full news_tools] [--limit N]
    python -m crypto_rag.evaluation.runs retrieval
"""

import argparse
import json
import logging
import math
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import Engine, text

from crypto_rag.agent.graph import CONFIGS, default_deps, run_query
from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.session import get_engine
from crypto_rag.retrieval.hybrid import openai_query_embedder, search_news

logger = logging.getLogger(__name__)

QUESTIONS = Path("evaluation/questions.jsonl")
RESULTS = Path("evaluation/results")
ANSWERS = RESULTS / "answers.jsonl"
RETRIEVAL = RESULTS / "retrieval.jsonl"
HALF_LIVES = (6.0, 12.0, 24.0, 48.0, 96.0, 168.0, math.inf)
RETRIEVAL_WINDOW_HOURS = 72
QUESTION_WINDOW_HOURS = 24
QUESTIONS_PER_GROUP = 5  # annotated subset: per asset and evaluation window
EVAL_PURPOSES = ("router", "generate", "embed_query", "judge")
DEFAULT_BUDGET_USD = 8.0


def load_questions(path: Path = QUESTIONS) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sample(items: list[Any], n: int, rng: np.random.Generator) -> list[Any]:
    """n items without replacement, in their original order (reproducible with the seed)."""
    if len(items) <= n:
        return list(items)
    return [items[i] for i in sorted(rng.choice(len(items), n, replace=False))]


def annotation_questions(seed: int | None = None) -> list[dict[str, Any]]:
    """The subset the author annotates and the judge scores: 5 per asset and window."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for q in load_questions():
        groups[(q["window"], q["asset"])].append(q)
    rng = np.random.default_rng(get_settings().random_seed if seed is None else seed)
    return [q for key in sorted(groups) for q in sample(groups[key], QUESTIONS_PER_GROUP, rng)]


def spent_since(engine: Engine, since: datetime) -> float:
    with engine.connect() as conn:
        return float(
            conn.execute(
                text(
                    "SELECT coalesce(sum(cost_usd), 0) FROM llm_usage "
                    "WHERE ts >= :s AND purpose = ANY(:p)"
                ),
                {"s": since, "p": list(EVAL_PURPOSES)},
            ).scalar_one()
        )


class Budget:
    """Stops new paid work once the spend since construction exceeds `usd`."""

    def __init__(self, engine: Engine, usd: float) -> None:
        self.engine, self.usd, self.since = engine, usd, datetime.now(UTC)

    def exhausted(self) -> bool:
        spent = spent_since(self.engine, self.since)
        if spent > self.usd:
            logger.warning("budget $%.2f reached ($%.2f spent); skipping the rest", self.usd, spent)
            return True
        return False


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def retrieved_articles(tool_results: dict[str, Any]) -> list[str]:
    """Article ids the answer had in context, in retrieval order, without repeats."""
    ids = [
        hit["article_id"]
        for name in ("search_news", "news_around_move")
        for call in tool_results.get(name, [])
        for hit in call
    ]
    return list(dict.fromkeys(ids))


def _answer(engine: Engine, q: dict[str, Any], config: str) -> dict[str, Any]:
    deps = default_deps(engine, CONFIGS[config])
    started = time.perf_counter()
    state = run_query(
        deps,
        q["question"],
        q["asset"],
        datetime.fromisoformat(q["as_of"]),
        window_hours=QUESTION_WINDOW_HOURS,
    )
    return {
        "question_id": q["id"],
        "config": config,
        "query_id": state["query_id"],
        "answer": state.get("answer", ""),
        "refused": bool(state.get("refused")),
        "intent": state.get("intent"),
        "attempts": state.get("attempts", 0),
        "verification_ok": state.get("verification", {}).get("ok"),
        "tools": [c["name"] for c in state.get("tool_calls", [])],
        "retrieved": retrieved_articles(state.get("tool_results", {})),
        "seconds": round(time.perf_counter() - started, 1),
    }


def run_answers(
    engine: Engine,
    configs: Sequence[str],
    limit: int | None = None,
    workers: int = 4,
    budget_usd: float = DEFAULT_BUDGET_USD,
) -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    done = {(r["question_id"], r["config"]) for r in read_jsonl(ANSWERS)}
    questions = load_questions()[:limit] if limit else load_questions()
    todo = [(q, c) for q in questions for c in configs if (q["id"], c) not in done]
    # The annotated subset first: if the budget runs out, the judged and hand-labelled
    # questions are complete in every configuration.
    subset = {q["id"] for q in annotation_questions()}
    todo.sort(key=lambda item: item[0]["id"] not in subset)
    lock = threading.Lock()
    budget = Budget(engine, budget_usd)

    def work(item: tuple[dict[str, Any], str]) -> None:
        q, c = item
        if budget.exhausted():
            return
        try:
            record = _answer(engine, q, c)
        except Exception:
            logger.exception("%s / %s failed; rerun to retry", q["id"], c)
            return
        with lock, ANSWERS.open("a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info("%s / %s: refused=%s %.0fs", q["id"], c, record["refused"], record["seconds"])

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, todo))
    return len(todo)


def run_retrieval(engine: Engine, half_lives: Sequence[float] = HALF_LIVES) -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    done = {(r["question_id"], r["half_life_hours"]) for r in read_jsonl(RETRIEVAL)}
    embed = openai_query_embedder(engine)
    k = get_settings().retrieval_top_k
    n = 0
    for q in load_questions():
        cache: dict[str, list[float]] = {}

        def cached(query: str, cache: dict[str, list[float]] = cache) -> list[float]:
            if query not in cache:
                cache[query] = embed(query)
            return cache[query]

        for hl in half_lives:
            key = "inf" if math.isinf(hl) else hl
            if (q["id"], key) in done:
                continue
            hits = search_news(
                engine, q["question"], datetime.fromisoformat(q["as_of"]), q["asset"],
                RETRIEVAL_WINDOW_HOURS, k=k, embed_query=cached, half_life_hours=hl,
                with_reaction=False,
            )  # fmt: skip
            with RETRIEVAL.open("a") as fh:
                fh.write(json.dumps({
                    "question_id": q["id"], "half_life_hours": key,
                    "articles": list(dict.fromkeys(h.article_id for h in hits)),
                }) + "\n")  # fmt: skip
            n += 1
    return n


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["answers", "retrieval"])
    parser.add_argument("--configs", nargs="+", choices=list(CONFIGS), default=list(CONFIGS))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    args = parser.parse_args()
    engine = get_engine()
    if args.command == "answers":
        n = run_answers(engine, args.configs, args.limit, args.workers, args.budget_usd)
    else:
        n = run_retrieval(engine)
    logger.info("%s: %d new runs", args.command, n)


if __name__ == "__main__":
    main()
