"""Stage 17 results: configuration × metric table, T½ curve, judge and label checks.

Reads evaluation/results/{answers,retrieval,judgements}.jsonl and the reference labels
of one annotator:
  author  — the author's labels from the UI (table `annotations`, the default);
  claude  — a model-made reference annotation (evaluation/manual/claude/*.jsonl, see its
            README.json). Its results go to evaluation/results/annotator_claude/ and must be
            reported as model annotation, never as human.
A metric whose labels are not given yet is reported as missing (empty), never estimated.

Outputs (evaluation/results/, or evaluation/results/annotator_<name>/):
    eval_table.csv      configuration × metric
    half_life.csv/.png  Recall@k of the deterministic retrieval run by T½ and window
    eval_summary.json   judge validation, label quality, spend, counts
    run_config.json     models, retrieval settings, seed, package versions, commit

Usage:
    python -m crypto_rag.evaluation.report [--annotator author|claude]
"""

import argparse
import hashlib
import json
import logging
import math
import shutil
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import cohen_kappa_score
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from crypto_rag.agent.graph import CONFIGS
from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Annotation
from crypto_rag.db.session import get_engine
from crypto_rag.evaluation import metrics
from crypto_rag.evaluation.annotate import MANUAL, labelled
from crypto_rag.evaluation.judge import JUDGEMENTS, tool_results_of
from crypto_rag.evaluation.runs import (
    ANSWERS,
    EVAL_PURPOSES,
    QUESTIONS,
    RESULTS,
    RETRIEVAL,
    load_questions,
    read_jsonl,
)

logger = logging.getLogger(__name__)

PACKAGES = ("langchain-core", "langchain-openai", "langgraph", "openai", "pgvector", "lightgbm")
ANNOTATORS = ("author", "claude")


def annotations(engine: Engine, task: str, annotator: str = "author") -> list[Any]:
    """Labelled items (item_id, label, payload with hidden model outputs) of one annotator."""
    if annotator == "author":
        return labelled(engine, task)
    path = MANUAL / annotator / f"{task}.jsonl"
    labels = {r["item_id"]: r["label"] for r in read_jsonl(path)}
    with Session(engine) as session:
        rows = session.scalars(
            select(Annotation).where(Annotation.task == task).order_by(Annotation.item_id)
        ).all()
        return [
            SimpleNamespace(item_id=r.item_id, label=labels[r.item_id], payload=r.payload)
            for r in rows
            if r.item_id in labels
        ]


def relevant_sets(engine: Engine, annotator: str = "author") -> dict[str, set[str]]:
    """question id -> articles marked relevant (only annotated questions)."""
    out: dict[str, set[str]] = {}
    for a in annotations(engine, "relevance", annotator):
        qid, article = a.item_id.split("|", 1)
        # A question with no relevant article still counts as annotated.
        marked = out.setdefault(qid, set())
        if a.label.get("relevant"):
            marked.add(article)
    return out


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None and not math.isnan(v)]
    return float(np.mean(vals)) if vals else None


def config_table(engine: Engine, relevant: dict[str, set[str]]) -> pd.DataFrame:
    questions = {q["id"]: q for q in load_questions()}
    judged = {(j["question_id"], j["config"]): j for j in read_jsonl(JUDGEMENTS)}
    answers_all = read_jsonl(ANSWERS)
    # Compare configurations on the same questions: those answered by every configuration.
    present = {a["config"] for a in answers_all}
    pairs = {(a["question_id"], a["config"]) for a in answers_all}
    common = {qid for qid in questions if all((qid, c) in pairs for c in present)}
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in answers_all:
        if a["question_id"] in common:
            by_config[a["config"]].append(a)
    reference: dict[str, list[dict[str, Any]]] = {}
    rows = []
    for config in CONFIGS:
        answers = by_config.get(config, [])
        if not answers:
            continue
        checks, faith, recalls = [], [], []
        for a in answers:
            if a["refused"]:
                continue
            q = questions[a["question_id"]]
            if q["id"] not in reference:
                reference[q["id"]] = metrics.reference_metrics(engine, q)
            checks.append(metrics.numeric_check(
                a["answer"], tool_results_of(engine, a["query_id"]), reference[q["id"]]))  # fmt: skip
            j = judged.get((a["question_id"], config))
            faith.append(j["faithfulness"] if j else None)
            if a["question_id"] in relevant and config != "no_retrieval":
                recalls.append(metrics.recall(a["retrieved"], relevant[a["question_id"]]))
        rows.append({
            "config": config,
            "answers": len(answers),
            "refusal_share": sum(a["refused"] for a in answers) / len(answers),
            "numeric_accuracy": _mean([float(c["accurate"]) for c in checks]),
            "numbers_per_answer": _mean([float(c["numbers"]) for c in checks]),
            "wrong_numbers": sum(len(c["wrong"]) for c in checks),
            "faithfulness": _mean(faith),
            "judged_answers": sum(f is not None for f in faith),
            "context_recall": _mean(recalls),
            "recall_questions": sum(r is not None for r in recalls),
        })  # fmt: skip
    return pd.DataFrame(rows)


def half_life_table(relevant: dict[str, set[str]]) -> pd.DataFrame:
    windows = {q["id"]: q["window"] for q in load_questions()}
    k = get_settings().retrieval_top_k
    rows = [
        {"question_id": r["question_id"], "window": windows[r["question_id"]],
         "half_life_hours": float(r["half_life_hours"]) if r["half_life_hours"] != "inf" else math.inf,
         "recall": metrics.recall(r["articles"], relevant[r["question_id"]], k)}
        for r in read_jsonl(RETRIEVAL) if r["question_id"] in relevant
    ]  # fmt: skip
    df = pd.DataFrame(rows).dropna(subset=["recall"]) if rows else pd.DataFrame()
    if df.empty:
        return df
    both = df.assign(window="all")
    return (
        pd.concat([df, both])
        .groupby(["window", "half_life_hours"], as_index=False)
        .agg(recall_at_k=("recall", "mean"), questions=("recall", "size"))
    )


def _plot_half_life(df: pd.DataFrame, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4))
    finite = df[np.isfinite(df.half_life_hours)]
    for window, part in finite.groupby("window"):
        line = ax.plot(part.half_life_hours, part.recall_at_k, marker="o", label=f"window {window}")
        none = df[(df.window == window) & ~np.isfinite(df.half_life_hours)]
        if not none.empty:
            ax.axhline(none.recall_at_k.iloc[0], ls="--", lw=0.8, color=line[0].get_color())
    ax.set_xscale("log")
    ax.set_xlabel("decay half-life T½, hours (dashed: no decay)")
    ax.set_ylabel(f"Recall@{get_settings().retrieval_top_k}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "half_life.png", dpi=150)
    plt.close(fig)


def judge_validation(engine: Engine, annotator: str = "author") -> dict[str, Any] | None:
    items = annotations(engine, "claims", annotator)
    if not items:
        return None
    human, judge, answer_diffs = [], [], []
    for a in items:
        h, j = a.label["supported"], a.payload["_judge_supported"]
        human += h
        judge += j
        answer_diffs.append(abs(np.mean(h) - np.mean(j)))
    return {
        "answers": len(items),
        "claims": len(human),
        "agreement": float(np.mean(np.array(human) == np.array(judge))),
        "cohen_kappa": float(cohen_kappa_score(human, judge)),
        "human_supported_share": float(np.mean(human)),
        "judge_supported_share": float(np.mean(judge)),
        # Counts that show the direction of disagreement without a chance-corrected statistic.
        "unsupported_by_reference": int(sum(not h for h in human)),
        "unsupported_by_judge": int(sum(not j for j in judge)),
        "unsupported_by_both": int(sum(not h and not j for h, j in zip(human, judge, strict=True))),
        "faithfulness_mae": float(np.mean(answer_diffs)),
    }


def label_quality(engine: Engine, annotator: str = "author") -> dict[str, Any] | None:
    items = annotations(engine, "labels", annotator)
    if not items:
        return None
    out: dict[str, Any] = {"articles": len(items)}
    for field in ("topic", "tone"):
        human = [a.label[field] for a in items]
        model = [a.payload[f"_model_{field}"] for a in items]
        out[f"{field}_accuracy"] = float(np.mean(np.array(human) == np.array(model)))
        out[f"{field}_kappa"] = float(cohen_kappa_score(human, model))
    return out


def spend(engine: Engine) -> dict[str, Any]:
    ids = [a["query_id"] for a in read_jsonl(ANSWERS)]
    if not ids:
        return {}
    with engine.connect() as conn:
        start = conn.execute(
            text("SELECT min(created_at) FROM query_log WHERE id = ANY(:ids)"), {"ids": ids}
        ).scalar_one()
        rows = conn.execute(
            text(
                "SELECT purpose, model, count(*) AS calls, sum(input_tokens) AS input_tokens, "
                "sum(output_tokens) AS output_tokens, sum(cost_usd)::float8 AS cost_usd "
                "FROM llm_usage WHERE ts >= :s AND purpose = ANY(:p) GROUP BY 1, 2 ORDER BY 1"
            ),
            {"s": start, "p": list(EVAL_PURPOSES)},
        ).mappings().all()  # fmt: skip
    return {"since": start.isoformat(), "by_purpose": [dict(r) for r in rows],
            "total_usd": round(sum(r["cost_usd"] or 0 for r in rows), 4)}  # fmt: skip


def run_config() -> dict[str, Any]:
    s = get_settings()
    git = shutil.which("git")
    commit = (
        subprocess.run(  # noqa: S603 - fixed arguments, git resolved from PATH
            [git, "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if git
        else ""
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": commit or None,
        "questions_sha256": hashlib.sha256(QUESTIONS.read_bytes()).hexdigest(),
        "models": {
            "router": s.router_model,
            "generator": s.generator_model,
            "judge": s.judge_model,
            "embedding": s.embedding_model,
            "embedding_dimensions": s.embedding_dimensions,
        },
        "retrieval": {
            "decay_half_life_hours": s.decay_half_life_hours,
            "rrf_k": s.rrf_k,
            "candidates": s.retrieval_candidates,
            "top_k": s.retrieval_top_k,
            "max_chunks_per_article": s.max_chunks_per_article,
        },
        "configs": {
            k: {
                "tools": sorted(c.tools) if c.tools is not None else "all",
                "verify": c.verify,
                "half_life_hours": "inf"
                if c.half_life_hours == math.inf
                else (c.half_life_hours or s.decay_half_life_hours),
            }
            for k, c in CONFIGS.items()
        },
        "seed": s.random_seed,
        "packages": {p: version(p) for p in PACKAGES},
    }


def build(engine: Engine, annotator: str = "author") -> dict[str, Any]:
    out = RESULTS if annotator == "author" else RESULTS / f"annotator_{annotator}"
    out.mkdir(parents=True, exist_ok=True)
    relevant = relevant_sets(engine, annotator)
    table = config_table(engine, relevant)
    table.to_csv(out / "eval_table.csv", index=False, float_format="%.4f")
    hl = half_life_table(relevant)
    if not hl.empty:
        hl.to_csv(out / "half_life.csv", index=False, float_format="%.4f")
        _plot_half_life(hl, out)
    summary = {
        "annotator": annotator,
        "questions": len(load_questions()),
        "annotated_questions": len(relevant),
        "questions_with_relevant": sum(bool(v) for v in relevant.values()),
        "judge_validation": judge_validation(engine, annotator),
        "label_quality": label_quality(engine, annotator),
        "spend": spend(engine),
    }
    (out / "eval_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    (out / "run_config.json").write_text(json.dumps(run_config(), indent=2) + "\n")
    logger.info("configurations:\n%s", table.to_string(float_format=lambda v: f"{v:.3f}"))
    if not hl.empty:
        logger.info("half-life:\n%s", hl.to_string(float_format=lambda v: f"{v:.3f}"))
    logger.info("summary: %s", json.dumps(summary, default=str)[:3000])
    return summary


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--annotator", choices=ANNOTATORS, default="author")
    build(get_engine(), parser.parse_args().annotator)


if __name__ == "__main__":
    main()
