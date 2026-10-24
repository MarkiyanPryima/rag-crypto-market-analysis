import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from crypto_rag.ingestion import coindesk_rss, gdelt
from tests.conftest import FIXTURES


def _gkg_line(
    url: str, title: str | None, ts: str = "20250303173000", source: str = "coindesk.com"
) -> str:
    cols = [""] * 27
    cols[0], cols[1], cols[2], cols[3], cols[4] = "rec-1", ts, "1", source, url
    cols[26] = f"<PAGE_TITLE>{title}</PAGE_TITLE>" if title else ""
    return "\t".join(cols)


def test_doc_api_payload_is_parsed():
    payload = json.loads((FIXTURES / "gdelt_doc_api_coindesk.json").read_text())
    rows = gdelt.parse_doc_api(payload)
    assert rows
    assert all(r["channel"] == "gdelt" and r["raw"]["time_source"] == "gdelt_seen" for r in rows)
    assert all(r["published_at"].tzinfo is UTC for r in rows)


def test_gkg_keeps_only_coindesk_articles_and_recovers_titles():
    content = "\n".join(
        [
            _gkg_line(
                "https://www.coindesk.com/markets/2025/03/03/xrp-back-at-support",
                "XRP Back at Support",
            ),
            _gkg_line("https://www.coindesk.com/business/2025/03/03/tether-names-new-cfo", None),
            _gkg_line("https://www.coindesk.com/price/bitcoin", "Bitcoin price"),  # not an article
            _gkg_line("https://example.com/2025/03/03/other", "Other", source="example.com"),
        ]
    ).encode()
    rows = gdelt.parse_gkg(content)
    assert [r["title"] for r in rows] == ["XRP Back at Support", "Tether names new cfo"]
    assert rows[1]["raw"]["title_from_slug"] is True
    assert rows[0]["published_at"] == datetime(2025, 3, 3, 17, 30, tzinfo=UTC)
    assert rows[0]["section"] == "markets"


def test_live_updates_are_flagged():
    row = gdelt.make_row(
        "https://www.coindesk.com/markets/2026/10/20/live-updates-market-jitters",
        "Live updates",
        datetime(2026, 10, 20, tzinfo=UTC),
        "doc_api",
    )
    assert row["is_liveblog"] is True


@pytest.mark.integration
def test_gdelt_never_overwrites_rss_and_dedupes(engine):
    url = "https://www.coindesk.com/policy/2026/10/19/rss-and-gdelt-story"
    rss_row = coindesk_rss.parse_feed(
        f"""<rss><channel><item><title>RSS title</title><link>{url}</link>
        <pubDate>Mon, 19 Oct 2026 09:00:00 +0000</pubDate><description>d</description>
        </item></channel></rss>""".encode(),
        datetime(2026, 10, 19, 9, 5, tzinfo=UTC),
    )
    coindesk_rss.upsert_rss(engine, rss_row)
    sighting = gdelt.make_row(url, "GDELT title", datetime(2026, 10, 19, 12, 30, tzinfo=UTC), "gkg")
    assert gdelt.upsert_gdelt(engine, [sighting, sighting]) == 0
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT channel, title, published_at, raw FROM articles WHERE url = :u"),
            {"u": url},
        ).one()
    assert row.channel == "rss"
    assert row.title == "RSS title"
    assert row.published_at == datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    assert row.raw["gdelt_seen"].startswith("2026-10-19T12:30")
