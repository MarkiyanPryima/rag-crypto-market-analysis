"""Metrics of stage 17 that need no model: numeric accuracy and retrieval recall.

Numeric accuracy checks the numbers an answer states against values computed by
code, regardless of whether the answer cites ids (the bare model cites none):
  * reference values: get_market_stats for the question's 24 h window, the 24 h and
    72 h before as_of, plus every metric in the answer's own tool results;
  * a number is correct if it equals one of them at the answer's rounding, or is
    quoted verbatim from a news chunk cited in the same paragraph;
  * dates, clock times, years and durations are not checked (as in the verifier),
    including dates written with a month name ("4–5 November", "5 листопада"): the
    bare model is not told to use YYYY-MM-DD.
An answer is numerically accurate when all its checked numbers are correct.
"""

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine

from crypto_rag.processing.assets import ASSET_SYMBOL
from crypto_rag.tools.market import get_market_stats
from crypto_rag.verification.verify import (
    DATE_RE,
    DURATION_RE,
    NUM_RE,
    REF_RE,
    _is_year,
    _sentences,
    collect_metrics,
    collect_sources,
    number_matches,
)

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    "січ|лют|берез|квіт|трав|черв|лип|серп|верес|жовт|листопад|груд"
)
WORD_DATE_RE = re.compile(
    rf"\b\d{{1,2}}(?:\s*[–-]\s*\d{{1,2}})?\s+(?:{_MONTHS})\w*"
    rf"|\b(?:{_MONTHS})\w*\s+\d{{1,2}}(?:\s*[–-]\s*\d{{1,2}})?\b",
    re.IGNORECASE,
)


def answer_numbers(answer: str, sources: dict[str, dict[str, Any]]) -> list[tuple[str, bool]]:
    """(token, quoted_from_cited_news) for every checked number in the answer."""
    out = []
    for paragraph in re.split(r"\n\s*\n", answer):
        cited = [
            sources[r]["text"] for r in REF_RE.findall(paragraph) if "text" in sources.get(r, {})
        ]
        for sentence in _sentences(paragraph):
            cleaned = REF_RE.sub(" ", WORD_DATE_RE.sub(" ", DATE_RE.sub(" ", sentence)))
            for m in NUM_RE.finditer(cleaned):
                token = m.group(0)
                core = token.lstrip("+-−$")
                if _is_year(core) or DURATION_RE.match(cleaned[m.end() :]):
                    continue
                out.append((token, any(core.rstrip("%") in chunk for chunk in cited)))
    return out


def reference_metrics(engine: Engine, q: dict[str, Any]) -> list[dict[str, Any]]:
    as_of = datetime.fromisoformat(q["as_of"])
    end = datetime.fromisoformat(q["window_end"])
    symbol = ASSET_SYMBOL[q["asset"]]
    windows = [(datetime.fromisoformat(q["window_start"]), end),
               (as_of - timedelta(hours=24), as_of), (as_of - timedelta(hours=72), as_of)]  # fmt: skip
    return [
        m.to_dict()
        for start, stop in windows
        for m in get_market_stats(engine, symbol, start, stop, as_of)
    ]


def numeric_check(
    answer: str, tool_results: dict[str, Any], reference: list[dict[str, Any]]
) -> dict[str, Any]:
    sources = collect_sources(tool_results)
    values = [
        m for m in [*collect_metrics(tool_results).values(), *reference] if m["value"] is not None
    ]
    wrong = []
    numbers = answer_numbers(answer, sources)
    for token, quoted in numbers:
        if quoted or any(number_matches(token, m["value"], m["decimals"]) for m in values):
            continue
        wrong.append(token)
    return {"numbers": len(numbers), "wrong": wrong, "accurate": not wrong}


def recall(retrieved: list[str], relevant: set[str], k: int | None = None) -> float | None:
    """Share of relevant articles among the first k retrieved; None if nothing is relevant."""
    if not relevant:
        return None
    top = retrieved[:k] if k else retrieved
    return len(set(top) & relevant) / len(relevant)
