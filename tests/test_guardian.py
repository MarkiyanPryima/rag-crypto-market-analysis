"""Guardian parsing on a result shaped like the documented /search output."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from crypto_rag.ingestion import guardian

FETCHED = datetime(2026, 10, 23, tzinfo=UTC)


def _result(**over):
    r = {
        "id": "technology/2025/mar/03/bitcoin-strategic-reserve-trump",
        "type": "article",
        "sectionName": "Technology",
        "webPublicationDate": "2025-03-03T18:00:00Z",
        "webTitle": "Bitcoin jumps after Trump reserve plan",
        "webUrl": "https://www.theguardian.com/technology/2025/mar/03/bitcoin-strategic-reserve-trump",
        "pillarName": "News",
        "fields": {
            "headline": "Bitcoin jumps after Trump unveils crypto reserve plan",
            "trailText": "Prices rise as the president names five tokens",
            "bodyText": "Bitcoin rose sharply on Sunday...",
            "firstPublicationDate": "2025-03-02T21:10:00Z",
            "lastModified": "2025-03-03T18:00:00Z",
        },
        "tags": [{"id": "technology/bitcoin"}, {"id": "technology/cryptocurrencies"}],
    }
    r.update(over)
    return r


def test_first_publication_date_is_used():
    row = guardian.parse_result(_result(), FETCHED)
    assert row["published_at"] == datetime(2025, 3, 2, 21, 10, tzinfo=UTC)
    assert row["raw"]["webPublicationDate"] == "2025-03-03T18:00:00Z"
    assert row["title"].startswith("Bitcoin jumps after Trump unveils")
    assert row["tags"] == ["technology/bitcoin", "technology/cryptocurrencies"]
    assert (row["source"], row["channel"]) == ("guardian", "api")
    assert row["is_liveblog"] is False


def test_liveblog_is_flagged():
    assert guardian.parse_result(_result(type="liveblog"), FETCHED)["is_liveblog"] is True


@pytest.mark.integration
def test_upsert_is_idempotent_and_refreshes_body(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles WHERE source = 'guardian'"))
    row = guardian.parse_result(_result(), FETCHED)
    assert guardian.upsert_guardian(engine, [row]) == 1
    changed = guardian.parse_result(
        _result(fields={**_result()["fields"], "bodyText": "Corrected text"}), FETCHED
    )
    assert guardian.upsert_guardian(engine, [changed]) == 0
    with engine.connect() as conn:
        body, n = conn.execute(text(
            "SELECT body_text, count(*) OVER () FROM articles WHERE source = 'guardian'"
        )).one()  # fmt: skip
    assert (body, n) == ("Corrected text", 1)
