"""Tests for VADER sentiment classification.

The threshold boundaries are pinned exactly. They decide every label in the
corpus, so an accidental change from >= to > would silently reclassify a slice
of the dataset and invalidate the accuracy metrics computed against it.
"""

import pytest

from worker import sentiment

# --- thresholds ------------------------------------------------------------


def test_thresholds_are_the_documented_values():
    """±0.05 is VADER's own recommended cut-off, not an arbitrary choice."""
    assert sentiment.POSITIVE_THRESHOLD == 0.05
    assert sentiment.NEGATIVE_THRESHOLD == -0.05


@pytest.mark.parametrize(
    ("compound", "expected"),
    [
        (0.05, "positive"),  # exactly at the boundary — inclusive
        (0.0500001, "positive"),
        (0.0499, "neutral"),  # just inside
        (0.0, "neutral"),
        (-0.0499, "neutral"),
        (-0.05, "negative"),  # exactly at the boundary — inclusive
        (-0.0500001, "negative"),
        (1.0, "positive"),
        (-1.0, "negative"),
    ],
    ids=[
        "pos-boundary",
        "just-pos",
        "just-under-pos",
        "zero",
        "just-over-neg",
        "neg-boundary",
        "just-neg",
        "max",
        "min",
    ],
)
def test_label_boundaries(monkeypatch, compound, expected):
    """Boundaries are inclusive: a score of exactly ±0.05 is not neutral."""
    monkeypatch.setattr(sentiment._analyzer, "polarity_scores", lambda _t: {"compound": compound})
    assert sentiment.classify("any text")["label"] == expected


# --- empty input -----------------------------------------------------------


@pytest.mark.parametrize("text", [None, "", "   ", "\n\t "], ids=["none", "empty", "spaces", "ws"])
def test_no_text_returns_none_not_neutral(text):
    """None and "neutral" mean different things.

    One of the three feeds supplies no body text at all. An article with
    nothing to classify must be excluded from accuracy metrics, not counted as
    a neutral prediction the classifier never made. The schema enforces the
    same distinction through news_sentiment_complete.
    """
    assert sentiment.classify(text) is None


def test_article_with_neither_title_nor_body_is_unclassified():
    assert sentiment.classify_article(None, None) is None
    assert sentiment.classify_article("", "   ") is None


# --- score shape -----------------------------------------------------------


def test_score_is_rounded_to_three_decimals(monkeypatch):
    """news.sentiment_score is NUMERIC(4,3)."""
    monkeypatch.setattr(sentiment._analyzer, "polarity_scores", lambda _t: {"compound": 0.123456})
    assert sentiment.classify("text")["score"] == 0.123


def test_score_stays_within_the_column_range():
    """news_sentiment_score_range is CHECK (score BETWEEN -1 AND 1)."""
    for text in ("terrible awful disaster", "wonderful excellent amazing", "the"):
        result = sentiment.classify(text)
        assert -1 <= result["score"] <= 1


def test_every_result_carries_all_three_fields():
    """news_sentiment_complete requires label, score and model together."""
    result = sentiment.classify("Bitcoin surges to a new high")
    assert set(result) == {"label", "score", "model"}
    assert all(value is not None for value in result.values())


def test_model_name_records_the_library_version():
    """A lexicon change between versions would alter scores silently."""
    assert sentiment.MODEL.startswith("vader-")
    assert sentiment.MODEL != "vader-unknown", "vaderSentiment is not installed"


def test_model_name_fits_the_column():
    """news.sentiment_model is VARCHAR(50)."""
    assert len(sentiment.MODEL) <= 50


# --- real classifications --------------------------------------------------


def test_clearly_positive_headline():
    result = sentiment.classify("Bitcoin soars to record high as investors celebrate huge gains")
    assert result["label"] == "positive"
    assert result["score"] > 0


def test_clearly_negative_headline():
    result = sentiment.classify(
        "Crypto exchange collapses after devastating hack, investors lose everything"
    )
    assert result["label"] == "negative"
    assert result["score"] < 0


def test_factual_headline_is_neutral():
    result = sentiment.classify("CFTC publishes rule on event contracts")
    assert result["label"] == "neutral"


def test_title_and_body_are_combined():
    """Both fields feed the classifier when available."""
    title_only = sentiment.classify_article("Market update", None)
    with_body = sentiment.classify_article(
        "Market update", "Excellent gains and wonderful news for holders"
    )
    assert with_body["score"] > title_only["score"]


def test_headline_only_still_classifies():
    """27% of collected articles have no body — they must still be scored."""
    result = sentiment.classify_article("Bitcoin falls on regulatory warning", None)
    assert result is not None
    assert result["label"] == "negative"


# --- the documented limitation, measured rather than assumed ---------------
#
# These tests were written expecting VADER to miss all crypto slang. Measuring
# it showed something more specific and more interesting, so they now record
# what it actually does. Each is a concrete, citable limitation.


@pytest.mark.parametrize(
    "term",
    ["rugpull", "moon", "rekt", "hodl", "bullish", "bearish", "pump", "whale"],
)
def test_crypto_vocabulary_is_absent_from_the_lexicon(term):
    """Terms carrying clear market sentiment that VADER scores at exactly zero.

    Note which words are NOT in this list: "fud" IS in VADER's lexicon at
    -1.1, as are "crash" and "dump". The gap is narrower than "it does not
    know crypto" — it is specific vocabulary, and step 50 quantifies the cost.
    """
    assert term not in sentiment._analyzer.lexicon
    assert sentiment.classify(term)["score"] == 0.0


def test_fud_is_in_the_lexicon():
    """Guards against overstating the limitation in the write-up.

    Claiming VADER misses "FUD" would be wrong, and an examiner could check it
    in one line.
    """
    assert "fud" in sentiment._analyzer.lexicon
    assert sentiment.classify("FUD is spreading")["label"] == "negative"


@pytest.mark.parametrize(
    "term",
    ["plunge", "plunges", "surge", "soars", "rally", "fell", "falls", "high", "hack"],
)
def test_price_movement_vocabulary_is_absent(term):
    """The most consequential gap, and the most quantifiable.

    VADER knows emotional words — "fraud" (-2.8), "scam" (-2.7), "panic"
    (-2.3), "fears" (-1.8), "losses" (-1.7) are all in its lexicon. It knows
    almost no word describing a price *movement*, which is the vocabulary
    financial headlines are actually written in.

    Consequence: "Bitcoin plunges after exchange hack" scores exactly 0.000 —
    perfectly neutral, for unambiguously bad news. The classifier is not
    merely imprecise on market text; on the most common headline form it has
    no signal at all.

    This is the single strongest argument for the FinBERT comparison in step
    51, and the sharpest sentence available for the limitations slide.
    """
    assert term not in sentiment._analyzer.lexicon


def test_catastrophic_headline_scores_exactly_neutral():
    """Concrete demonstration of the gap above, for the write-up."""
    result = sentiment.classify("Bitcoin plunges after exchange hack")
    assert result["score"] == 0.0
    assert result["label"] == "neutral"


def test_emotional_vocabulary_is_present():
    """The other half of the finding: the lexicon is not useless, just skewed."""
    for term in ("fraud", "scam", "panic", "fears", "losses", "warning", "gains"):
        assert term in sentiment._analyzer.lexicon

    assert sentiment.classify("Exchange fraud sparks fears of collapse")["label"] == "negative"


def test_no_stemming_so_inflections_are_missed():
    """ "crash" is scored, "crashes" is not — VADER does no morphology."""
    assert "crash" in sentiment._analyzer.lexicon
    assert "crashes" not in sentiment._analyzer.lexicon

    assert sentiment.classify("a crash")["label"] == "negative"
    assert sentiment.classify("it crashes")["label"] == "neutral"


def test_financial_jargon_can_invert_the_label():
    """The sharpest limitation found: a negative headline scored positive.

    "Bitcoin crashes below support" reads as clearly bearish. VADER returns
    +0.402 — positive — because "crashes" is not in its lexicon while
    "support" is, and in general English "support" means help or backing
    rather than a price floor.

    This is worth more than the slang gap in the report: it shows the failure
    is not merely missing signal, but inverting it on ordinary financial
    phrasing. It also predicts where FinBERT, trained on financial text,
    should win in step 51.
    """
    result = sentiment.classify("Bitcoin crashes below support")

    assert result["label"] == "positive", (
        "the inversion no longer reproduces — re-check the limitation section"
    )
    assert result["score"] == pytest.approx(0.402, abs=0.01)
