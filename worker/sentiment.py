"""Per-article sentiment classification with VADER.

VADER is a lexicon-and-rule classifier: it scores text by looking words up in
a hand-built lexicon and applying rules for negation, intensifiers and
punctuation. No model is trained and no network call is made, so a thousand
articles cost nothing and take milliseconds.

The limitation is narrower and more interesting than "it does not understand
crypto", as measured in tests/test_sentiment.py:

  * Missing vocabulary — "rugpull", "moon", "rekt", "hodl", "bullish",
    "bearish" score exactly 0.0. But "fud", "crash" and "dump" ARE in the
    lexicon, so the gap is specific rather than total.
  * No stemming — "crash" is scored, "crashes" is not.
  * **Inverted labels on financial phrasing.** "Bitcoin crashes below
    support" scores +0.402, i.e. positive, because "crashes" is unknown while
    "support" means help in general English rather than a price floor.

The third is the serious one: the classifier does not merely miss signal, it
reverses it on ordinary market language. Step 50 quantifies how often, and it
predicts where FinBERT — trained on financial text — should win in step 51.

The model name is recorded with every score. `news.sentiment_model` exists so
a second classifier can be run over the same corpus and compared, which is
what the FinBERT baseline in step 51 does.
"""

import logging
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

logger = logging.getLogger(__name__)

# Thresholds from VADER's own documentation. The compound score is normalised
# to [-1, 1] and ±0.05 is the author's recommended cut-off for calling text
# non-neutral.
POSITIVE_THRESHOLD = 0.05
NEGATIVE_THRESHOLD = -0.05

# news.sentiment_score is NUMERIC(4,3): three decimal places.
_SCORE_PRECISION = 3


def _model_name() -> str:
    """Record the exact library version, not a hardcoded string.

    Reproducibility: a lexicon change between versions would alter scores, and
    the stored name must say which version produced them.
    """
    try:
        return f"vader-{version('vaderSentiment')}"
    except PackageNotFoundError:  # pragma: no cover - only if not installed
        return "vader-unknown"


MODEL = _model_name()

_analyzer = SentimentIntensityAnalyzer()


def classify(text: str | None) -> dict[str, Any] | None:
    """Classify `text`, or return None if there is nothing to classify.

    None is distinct from "neutral". An article with no usable text — which
    happens, since one of the three feeds supplies no body at all — is left
    unclassified so it is excluded from accuracy metrics, rather than counted
    as a neutral prediction it never made.

    The schema enforces this distinction: news_sentiment_complete requires all
    three sentiment columns to be set together or all be NULL.
    """
    if text is None or not text.strip():
        return None

    compound = _analyzer.polarity_scores(text)["compound"]

    if compound >= POSITIVE_THRESHOLD:
        label = "positive"
    elif compound <= NEGATIVE_THRESHOLD:
        label = "negative"
    else:
        label = "neutral"

    return {
        "label": label,
        "score": round(compound, _SCORE_PRECISION),
        "model": MODEL,
    }


def classify_article(title: str | None, content: str | None) -> dict[str, Any] | None:
    """Classify an article from its title and body.

    Both are used when available. For the feed that supplies no body, this is
    effectively headline-only classification — step 50 reports accuracy split
    by whether a body was present, so the cost of that gap is measured rather
    than guessed at.
    """
    parts = [part.strip() for part in (title, content) if part and part.strip()]
    if not parts:
        return None
    return classify(" ".join(parts))
