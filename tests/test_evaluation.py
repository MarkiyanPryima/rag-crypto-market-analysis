from crypto_rag.evaluation.metrics import answer_numbers, numeric_check, recall
from crypto_rag.evaluation.runs import retrieved_articles


def _metric(mid, value, decimals):
    return {"id": mid, "value": value, "decimals": decimals, "display": str(value)}


def test_dates_durations_and_word_dates_are_not_checked():
    text = (
        "BTC fell over 4–5 November and on 2026-11-05 at 15:00, within 48 h, 5 листопада, in 2025."
    )
    assert answer_numbers(text, {}) == []


def test_numbers_match_reference_or_cited_news():
    tool_results = {"search_news": [[{"id": "c_1", "published_at": "2026-11-05T10:00:00+00:00",
                                      "text": "Liquidations reached $1 billion.", "article_id": "a1"}]]}  # fmt: skip
    reference = [_metric("m1", -2.8812, 2)]
    ok = numeric_check("BTC fell 2.88% [m1]. A wave of $1 billion [c_1].", tool_results, reference)
    assert ok == {"numbers": 2, "wrong": [], "accurate": True}
    bad = numeric_check("BTC fell 4% overnight.", {}, reference)  # 3% would match (rounded)
    assert bad["wrong"] == ["4%"] and not bad["accurate"]


def test_recall_and_retrieved_order():
    tr = {"search_news": [[{"article_id": "a"}, {"article_id": "b"}]],
          "news_around_move": [[{"article_id": "b"}, {"article_id": "c"}]]}  # fmt: skip
    assert retrieved_articles(tr) == ["a", "b", "c"]
    assert recall(["a", "b", "c"], {"c", "d"}) == 0.5
    assert recall(["a", "b", "c"], {"c"}, k=2) == 0
    assert recall(["a"], set()) is None
