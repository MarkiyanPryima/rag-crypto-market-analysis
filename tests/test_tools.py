import math
from datetime import timedelta
from itertools import pairwise

import pytest
from sqlalchemy import text

from crypto_rag.tools import episodes, market, news_reaction
from crypto_rag.tools.base import IdAllocator
from tests.synthetic import T0, insert_candles, reset

pytestmark = pytest.mark.integration
SYM = "BTCUSDT"


@pytest.fixture
def market_data(engine):
    reset(engine, SYM)
    # 30 days of flat price at 100 with volume 10 (baseline), then a 10-hour window.
    base = [100.0] * (30 * 24)
    window = [100, 101, 102, 107.1, 107.1, 106, 105, 104, 103, 102]
    closes = base + window
    volumes = [10.0] * len(base) + [20.0] * len(window)
    insert_candles(engine, SYM, closes, volumes=volumes)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO funding_rates VALUES (:s, :t1, 0.0001), (:s, :t2, 0.0003)"),
            {"s": SYM, "t1": T0, "t2": T0 + timedelta(days=30, hours=4)},
        )
    w_start = T0 + timedelta(days=30)
    return w_start, w_start + timedelta(hours=10)


def _by_name(metrics):
    return {m.name: m for m in metrics}


def test_market_stats_known_answers(engine, market_data):
    start, end = market_data
    m = _by_name(market.get_market_stats(engine, SYM, start, end, as_of=end + timedelta(days=1)))
    assert m["price_start"].value == 100.0
    assert m["price_end"].value == 102.0
    assert m["price_change_pct"].value == 2.0
    assert m["sharpest_hour_pct"].value == 5.0  # 102 -> 107.1
    assert m["sharpest_hour_pct"].extra["ts"] == (start + timedelta(hours=3)).isoformat()
    assert m["volume_vs_30d"].value == 2.0
    assert m["taker_buy_share_pct"].value == 60.0
    assert m["funding_rate_pct"].value == 0.03
    assert m["funding_change_pp"].value == 0.02
    assert [x.id for x in m.values()][:3] == ["m1", "m2", "m3"]


def test_market_stats_volatility_formula(engine, market_data):
    start, end = market_data
    m = _by_name(market.get_market_stats(engine, SYM, start, end, as_of=end))
    closes = [100, 100, 101, 102, 107.1, 107.1, 106, 105, 104, 103, 102]
    lr = [math.log(b / a) for a, b in pairwise(closes)]
    mean = sum(lr) / len(lr)
    sd = math.sqrt(sum((x - mean) ** 2 for x in lr) / (len(lr) - 1)) * 100
    assert m["volatility_hourly_pct"].value == round(sd, 3)


def test_market_stats_ignore_data_after_as_of(engine, market_data):
    start, end = market_data
    as_of = start + timedelta(hours=6)
    before = [x.to_dict() for x in market.get_market_stats(engine, SYM, start, end, as_of)]
    # Future candles with absurd values and a future funding print.
    insert_candles(engine, SYM, [1.0, 1e6, 5.0, 9.0], start=as_of, volumes=[1e9] * 4)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO funding_rates VALUES (:s, :t, 0.5)"),
            {"s": SYM, "t": as_of + timedelta(minutes=1)},
        )
    after = [x.to_dict() for x in market.get_market_stats(engine, SYM, start, end, as_of)]
    assert before == after
    assert (
        _by_name(market.get_market_stats(engine, SYM, start, end, as_of))["price_end"].value
        == 106.0
    )


def test_unclosed_candle_is_invisible(engine, market_data):
    start, _ = market_data
    # At 03:30 the 03:00 candle has not closed yet: last visible close is 102 (02:00 candle).
    m = _by_name(
        market.get_market_stats(
            engine, SYM, start, start + timedelta(hours=5), start + timedelta(hours=3, minutes=30)
        )
    )
    assert m["price_end"].value == 102.0


def test_news_price_reaction(engine, market_data):
    start, _ = market_data
    pub = start + timedelta(hours=4)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM articles WHERE id = 'n:1'"))
        conn.execute(
            text("INSERT INTO articles (id, source, channel, url, title, published_at) "
                 "VALUES ('n:1', 'coindesk', 'rss', 'https://n/1', 'Bitcoin jumps', :p)"),
            {"p": pub},
        )  # fmt: skip
    m = _by_name(
        news_reaction.news_price_reaction(engine, "n:1", as_of=pub + timedelta(days=1), symbol=SYM)
    )
    # price at 02:00 close (candle 01:00) = 101; at 04:00 = 107.1; at 10:00 = 102
    assert m["move_2h_before_pct"].value == round((107.1 / 101 - 1) * 100, 2)
    assert m["move_6h_after_pct"].value == round((102 / 107.1 - 1) * 100, 2)
    early = _by_name(
        news_reaction.news_price_reaction(engine, "n:1", as_of=pub + timedelta(hours=1), symbol=SYM)
    )
    assert early["move_6h_after_pct"].value is None  # the 6 h have not passed yet
    assert news_reaction.news_price_reaction(engine, "n:1", as_of=pub - timedelta(minutes=1)) == []


def test_episode_shape_and_drawdown():
    import pandas as pd

    closes = [100.0 + i for i in range(48)] + [100.0] * 200
    df = pd.DataFrame(
        {"ts": pd.date_range(T0, periods=len(closes), freq="1h", tz="UTC"), "close": closes}
    )
    eps = episodes.compute_episodes(df)
    first = eps.iloc[0]
    assert first.end_ts - first.start_ts == timedelta(hours=48)
    assert first.return_pct == pytest.approx(47.0)
    assert first.max_drawdown == 0.0
    assert first.return_7d_after_pct == pytest.approx((100 / 147 - 1) * 100)


def test_similar_episodes_never_return_current_window(engine):
    reset(engine, SYM)
    # Periodic series: the best match by shape would be the current window itself.
    closes = [100 + 10 * math.sin(i / 8) for i in range(24 * 40)]
    insert_candles(engine, SYM, closes)
    episodes.build_episodes(engine, SYM)
    as_of = T0 + timedelta(hours=len(closes))
    cur_start = as_of - timedelta(hours=48)
    ids = IdAllocator()
    results = episodes.find_similar_episodes(engine, SYM, as_of, k=3, ids=ids)
    assert len(results) == 3
    for r in results:
        assert r["end_ts"] <= cur_start.isoformat()
    starts = sorted(r["start_ts"] for r in results)
    assert len(set(starts)) == 3
    # 7-day outcome hidden when it would need data after as_of.
    late = episodes.find_similar_episodes(engine, SYM, T0 + timedelta(days=4), k=3)
    for r in late:
        after = next(m for m in r["metrics"] if m["name"] == "episode_return_7d_after_pct")
        assert after["value"] is None
