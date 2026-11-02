from datetime import UTC, datetime

from crypto_rag.tools.base import IdAllocator, metric
from crypto_rag.verification.verify import number_matches, verify

AS_OF = datetime(2025, 9, 10, 12, tzinfo=UTC)
MOVE = datetime(2025, 9, 10, 3, tzinfo=UTC)


def _results():
    ids = IdAllocator()
    stats = [
        metric(ids, "price_change_pct", -3.214, "%", 2, "Price change"),
        metric(ids, "price_end", 84416.47, "USDT", 2, "Price at the end"),
        metric(ids, "sharpest_hour_pct", -2.05, "%", 2, "Largest hourly move", ts=MOVE.isoformat()),
    ]
    fc = [metric(ids, "buy_signal", 41.2, "%", 1, "Probability of rise", prefix="f")]
    news = [
        {"id": "c_1", "title": "SEC delays ETF decision", "published_at": "2025-09-10T01:30:00+00:00",
         "text": "The SEC delayed its decision on 3 spot ETF applications."},
        {"id": "c_2", "title": "Bitcoin slides", "published_at": "2025-09-10T05:00:00+00:00", "text": "Bitcoin slid."},
    ]  # fmt: skip
    return {
        "get_market_stats": [[m.to_dict() for m in stats]],
        "get_forecast": [[m.to_dict() for m in fc]],
        "search_news": [news],
    }


GOOD = """## Що сталося
BTC fell 3.21% [m1] over the last 24 h and closed at 84,416.47 [m2] USDT. The sharpest hour moved -2.05% [m3] at 03:00 UTC on 2025-09-10.

## Ймовірні причини
In time this coincided with news that the SEC delayed its decision on 3 spot ETF applications [c_1].

## Прогноз
The model puts the probability of a rise at 41.2% [f1].

Не є фінансовою порадою."""


def test_correct_answer_passes():
    result = verify(GOOD, _results(), AS_OF)
    assert result.ok, result.errors


def test_changed_number_is_caught():
    bad = GOOD.replace("3.21% [m1]", "3.51% [m1]")
    result = verify(bad, _results(), AS_OF)
    assert [e.check for e in result.errors] == ["numbers"]


def test_number_without_reference_is_caught():
    bad = GOOD.replace("41.2% [f1]", "41.2%")
    assert any(
        e.check == "numbers" and e.token == "41.2%" for e in verify(bad, _results(), AS_OF).errors
    )


def test_invented_reference_is_caught():
    bad = GOOD.replace("[c_1]", "[c_9]")
    assert any(
        e.check == "references" and e.token == "c_9" for e in verify(bad, _results(), AS_OF).errors
    )


def test_future_article_is_caught():
    res = _results()
    res["search_news"][0][0]["published_at"] = "2025-09-11T00:00:00+00:00"
    errors = verify(GOOD, res, AS_OF).errors
    assert any(e.check == "source_dates" for e in errors)


def test_cause_after_move_is_caught():
    bad = GOOD.replace("delayed its decision on 3 spot ETF applications [c_1]", "slid [c_2]")
    errors = verify(bad, _results(), AS_OF).errors
    assert [e.check for e in errors] == ["temporal_order"]


def test_rounding_rules():
    assert number_matches("3.2%", -3.214, 2)
    assert number_matches("3%", -3.214, 2)
    assert not number_matches("3.214%", -3.214, 2)  # more precise than the metric
    assert number_matches("84,416.47", 84416.47, 2)


def test_ukrainian_durations_are_not_numbers():
    text = GOOD.replace(
        "over the last 24 h", "порівняно з 30-денного середнього та 48-годинний епізод за 7 днів"
    )
    assert verify(text, _results(), AS_OF).ok


def test_later_news_outside_causes_section_passes():
    # Real case (query 1): post-move coverage described as such, placed under "Що сталося".
    text = GOOD.replace(
        "The sharpest hour moved -2.05% [m3] at 03:00 UTC on 2025-09-10.",
        "The sharpest hour moved -2.05% [m3] at 03:00 UTC on 2025-09-10. Later coverage reported the slide [c_2].",
    )
    assert verify(text, _results(), AS_OF).ok


def test_cause_feedback_says_where_to_move_it():
    bad = GOOD.replace("delayed its decision on 3 spot ETF applications [c_1]", "slid [c_2]")
    (err,) = verify(bad, _results(), AS_OF).errors
    assert "Що сталося" in err.message


def test_news_figure_cited_later_in_paragraph_passes():
    # Real case (query 10): "понад 6 млрд доларів" quoted from news, [c_1] in the next sentence.
    res = _results()
    res["search_news"][0][0]["text"] = (
        "The FBI tied the theft of more than $6bn in crypto assets to a North Korean group."
    )
    text = GOOD.replace(
        "In time this coincided with news that the SEC delayed its decision on 3 spot ETF applications [c_1].",
        "Перед рухом повідомили про викрадення понад 6 млрд доларів. Це передувало руху [c_1].",
    )
    assert verify(text, res, AS_OF).ok


def test_uncited_news_figure_still_fails_with_actionable_hint():
    text = GOOD.replace(
        "In time this coincided with news that the SEC delayed its decision on 3 spot ETF applications [c_1].",
        "Перед рухом повідомили про викрадення понад 6 млрд доларів.",
    )
    errors = verify(text, _results(), AS_OF).errors
    assert any(e.token == "6" and "same paragraph" in e.message for e in errors)
