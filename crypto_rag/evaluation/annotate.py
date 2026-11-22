"""Items for the author's manual annotation (stage 17) and their export.

Three tasks, sized for about 1.5–2 hours of work in the UI ("Розмітка" tab):
  relevance — 20 questions (5 per asset and evaluation window, fixed seed). The
              pool is every article that any T½ retrieval run or any retrieval
              configuration put in context for the question (pooling, as in TREC);
              the author marks each article relevant to the move or not.
  claims    — 20 judged answers (10 "news_tools", 10 "full", fixed seed). The author
              sees the answer, the context the judge saw and the judge's claims, and
              marks each claim supported or not. The judge's verdicts stay hidden.
  labels    — 40 articles with model topic/tone labels (20 The Guardian, 20 CoinDesk);
              the author assigns topic and tone without seeing the model's labels.

Keys of `payload` that start with "_" (model outputs) are never sent to the UI.
Rebuilding keeps labels already given (existing items are left as they are).

Usage:
    python -m crypto_rag.evaluation.annotate build
    python -m crypto_rag.evaluation.annotate export   # labels -> evaluation/manual/*.jsonl
"""

import argparse
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Annotation
from crypto_rag.db.session import get_engine
from crypto_rag.evaluation.judge import JUDGEMENTS, context_text, tool_results_of
from crypto_rag.evaluation.runs import (
    ANSWERS,
    RETRIEVAL,
    annotation_questions,
    load_questions,
    read_jsonl,
    sample,
)

logger = logging.getLogger(__name__)

MANUAL = Path("evaluation/manual")
CLAIM_ANSWERS = {"news_tools": 10, "full": 10}
LABEL_ARTICLES = {"guardian": 20, "coindesk": 20}
POOL_CONFIGS = ("news_only", "news_tools", "full")
SNIPPET = 600


def public(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _articles(engine: Engine, ids: set[str]) -> dict[str, dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, source, channel, title, coalesce(standfirst, '') AS standfirst, "
                "left(coalesce(body_text, ''), :n) AS body, url, published_at "
                "FROM articles WHERE id = ANY(:ids)"
            ),
            {"ids": list(ids), "n": SNIPPET},
        ).mappings()
        return {r["id"]: {**r, "published_at": r["published_at"].isoformat()} for r in rows}


def relevance_items(engine: Engine, seed: int) -> list[dict[str, Any]]:
    pool: dict[str, set[str]] = defaultdict(set)
    for r in read_jsonl(RETRIEVAL):
        pool[r["question_id"]].update(r["articles"])
    for a in read_jsonl(ANSWERS):
        if a["config"] in POOL_CONFIGS:
            pool[a["question_id"]].update(a["retrieved"])
    items = []
    for q in annotation_questions(seed):
        arts = _articles(engine, pool[q["id"]])
        for aid in sorted(arts, key=lambda i: arts[i]["published_at"]):
            items.append({
                "task": "relevance", "item_id": f"{q['id']}|{aid}",
                "payload": {
                    "question_id": q["id"], "question": q["question"], "asset": q["asset"],
                    "move_pct": q["move_pct"], "window_start": q["window_start"],
                    "window_end": q["window_end"], "article": arts[aid],
                },
            })  # fmt: skip
    return items


def claim_items(engine: Engine, seed: int) -> list[dict[str, Any]]:
    answers = {(a["question_id"], a["config"]): a for a in read_jsonl(ANSWERS)}
    questions = {q["id"]: q for q in load_questions()}
    judged = [j for j in read_jsonl(JUDGEMENTS) if j["claims"]]
    rng = np.random.default_rng(seed)
    items = []
    for config, n in CLAIM_ANSWERS.items():
        for j in sample([j for j in judged if j["config"] == config], n, rng):
            a = answers[(j["question_id"], config)]
            items.append({
                "task": "claims", "item_id": f"{j['question_id']}|{config}",
                "payload": {
                    "question_id": j["question_id"],
                    "question": questions[j["question_id"]]["question"],
                    "as_of": questions[j["question_id"]]["as_of"],
                    "answer": a["answer"],
                    "context": context_text(tool_results_of(engine, a["query_id"])),
                    "claims": [c["claim"] for c in j["claims"]],
                    "_config": config,
                    "_judge_supported": [c["supported"] for c in j["claims"]],
                },
            })  # fmt: skip
    return items


def label_items(engine: Engine, seed: int) -> list[dict[str, Any]]:
    items = []
    with engine.connect() as conn:
        for source, n in LABEL_ARTICLES.items():
            rows = conn.execute(
                text(
                    "SELECT a.id, a.source, a.title, coalesce(a.standfirst, '') AS standfirst, "
                    "left(coalesce(a.body_text, ''), :snip) AS body, a.url, a.published_at, "
                    "l.topic, l.tone FROM articles a JOIN article_labels l ON l.article_id = a.id "
                    "WHERE a.source = :s AND NOT a.is_liveblog ORDER BY a.id"
                ),
                {"s": source, "snip": SNIPPET},
            ).mappings().all()  # fmt: skip
            for r in sample(list(rows), n, np.random.default_rng(seed)):
                items.append({
                    "task": "labels", "item_id": r["id"],
                    "payload": {
                        "source": r["source"], "title": r["title"], "standfirst": r["standfirst"],
                        "body": r["body"], "url": r["url"],
                        "published_at": r["published_at"].isoformat(),
                        "_model_topic": r["topic"], "_model_tone": r["tone"],
                    },
                })  # fmt: skip
    return items


def build(engine: Engine) -> dict[str, int]:
    seed = get_settings().random_seed
    counts = {}
    for task, items in (("relevance", relevance_items(engine, seed)),
                        ("claims", claim_items(engine, seed)),
                        ("labels", label_items(engine, seed))):  # fmt: skip
        if items:
            with engine.begin() as conn:
                conn.execute(insert(Annotation).values(items).on_conflict_do_nothing())
        counts[task] = len(items)
    logger.info("annotation items: %s", counts)
    return counts


def labelled(engine: Engine, task: str) -> list[Annotation]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(Annotation)
                .where(Annotation.task == task, Annotation.label.is_not(None))
                .order_by(Annotation.item_id)
            )
        )


def export(engine: Engine) -> None:
    MANUAL.mkdir(parents=True, exist_ok=True)
    for task in ("relevance", "claims", "labels"):
        rows = [{"item_id": a.item_id, "label": a.label,
                 "labelled_at": a.labelled_at.isoformat() if a.labelled_at else None}
                for a in labelled(engine, task)]  # fmt: skip
        (MANUAL / f"{task}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        )
        logger.info("%s: %d labelled items exported", task, len(rows))


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["build", "export"])
    args = parser.parse_args()
    (build if args.command == "build" else export)(get_engine())


if __name__ == "__main__":
    main()
