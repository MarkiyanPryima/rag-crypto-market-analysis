"""Topic and tone labels for each article (one LLM call per article).

Prompt and schema live in label_prompt.py. Input: title + standfirst + the
first 1000 characters of body text.

Usage:
    python -m crypto_rag.processing.label run
    python -m crypto_rag.processing.label sample --n 50     # CSV for manual labels
    python -m crypto_rag.processing.label agreement --csv evaluation/manual_labels.csv
"""

import argparse
import csv
import logging
from collections.abc import Sequence
from pathlib import Path

from openai import OpenAI
from sqlalchemy import Engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.models import Article, ArticleLabel
from crypto_rag.db.session import get_engine
from crypto_rag.llm import parse_structured
from crypto_rag.processing.clean import clean_text
from crypto_rag.processing.label_prompt import LABEL_INSTRUCTIONS, ArticleLabelOut

logger = logging.getLogger(__name__)

BODY_CHARS = 1000


def label_input(article: Article) -> str:
    parts = [f"Title: {article.title}"]
    if article.standfirst:
        parts.append(f"Standfirst: {clean_text(article.standfirst)}")
    body = clean_text(article.body_text)
    if body:
        parts.append(f"Body (start): {body[:BODY_CHARS]}")
    return "\n".join(parts)


def label_article(
    engine: Engine, article: Article, client: OpenAI | None = None
) -> ArticleLabelOut:
    return parse_structured(
        engine,
        purpose="label",
        model=get_settings().label_model,
        instructions=LABEL_INSTRUCTIONS,
        user_input=label_input(article),
        schema=ArticleLabelOut,
        client=client,
    )


def label_pending(
    engine: Engine,
    client: OpenAI | None = None,
    limit: int | None = None,
    article_ids: Sequence[str] | None = None,
) -> int:
    """Label articles without a label. Failures are logged and retried on the next run."""
    model = get_settings().label_model
    done = failed = 0
    with Session(engine) as session:
        stmt = (
            select(Article)
            .outerjoin(ArticleLabel, ArticleLabel.article_id == Article.id)
            .where(ArticleLabel.article_id.is_(None))
            .order_by(Article.published_at)
        )
        if article_ids is not None:
            stmt = stmt.where(Article.id.in_(article_ids))
        if not get_settings().label_hf_articles:
            # Dataset articles are left unlabelled unless enabled (see config.label_hf_articles).
            stmt = stmt.where(Article.channel != "hf")
        if limit:
            stmt = stmt.limit(limit)
        for article in session.scalars(stmt):
            try:
                out = label_article(engine, article, client)
            except Exception:
                failed += 1
                logger.exception("labelling failed for %s; will retry next run", article.id)
                continue
            session.execute(
                insert(ArticleLabel)
                .values(article_id=article.id, topic=out.topic, tone=out.tone, model=model)
                .on_conflict_do_nothing()
            )
            session.commit()
            done += 1
    logger.info("labelled %d articles (%d failed)", done, failed)
    return done


def export_sample(engine: Engine, n: int, path: Path) -> None:
    """Random labelled articles to CSV with empty manual_* columns for the author."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.id, a.source, a.published_at::date AS date, a.title, "
                "left(coalesce(a.standfirst, ''), 300) AS standfirst, l.topic, l.tone "
                "FROM articles a JOIN article_labels l ON l.article_id = a.id "
                "ORDER BY md5(a.id) LIMIT :n"
            ),
            {"n": n},
        ).mappings().all()  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(
            fh, fieldnames=[*rows[0].keys(), "manual_topic", "manual_tone"] if rows else ["id"]
        )
        w.writeheader()
        for r in rows:
            w.writerow({**r, "manual_topic": "", "manual_tone": ""})
    logger.info("wrote %d rows to %s", len(rows), path)


def agreement(path: Path) -> dict[str, float]:
    with path.open() as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("manual_topic") and r.get("manual_tone")]
    if not rows:
        raise ValueError("no manually labelled rows found")
    topic = sum(r["topic"] == r["manual_topic"].strip() for r in rows) / len(rows)
    tone = sum(r["tone"] == r["manual_tone"].strip() for r in rows) / len(rows)
    return {"n": len(rows), "topic_agreement": round(topic, 3), "tone_agreement": round(tone, 3)}


def main(argv: Sequence[str] | None = None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Article topic/tone labelling")
    parser.add_argument("command", choices=["run", "sample", "agreement"])
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--csv", type=Path, default=Path("evaluation/manual_labels.csv"))
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    engine = get_engine()
    if args.command == "run":
        label_pending(engine, limit=args.limit)
    elif args.command == "sample":
        export_sample(engine, args.n, args.csv)
    else:
        logger.info("agreement: %s", agreement(args.csv))


if __name__ == "__main__":
    main()
