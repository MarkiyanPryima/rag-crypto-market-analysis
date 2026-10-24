"""APScheduler processes (stage 16). All times are UTC.

collector:
    CoinDesk RSS        every 15 min, then processing of new articles
    Binance candles     hourly at :01 (after the candle closes)
    Binance funding     every 8 h at 00:02 / 08:02 / 16:02
    Guardian            daily 01:00 (new) and 01:30 (modified), then processing
    Episodes            hourly at :05
forecaster:
    Features + forecast daily 00:05
    Retraining          weekly, Monday 02:00

Each process touches a heartbeat file after every job; the compose
healthcheck fails if it is older than 20 minutes.

Usage:
    python -m crypto_rag.scheduler.jobs collector | forecaster
    python -m crypto_rag.scheduler.jobs run-once <job>
"""

import argparse
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from crypto_rag.config import get_settings, setup_logging
from crypto_rag.db.session import get_engine
from crypto_rag.ingestion import binance, coindesk_rss, guardian
from crypto_rag.ingestion.http import PoliteClient
from crypto_rag.processing.pipeline import run as run_pipeline
from crypto_rag.tools.episodes import build_episodes

logger = logging.getLogger(__name__)

HEARTBEAT = Path("/tmp/crypto_rag_heartbeat")  # noqa: S108 - container-local liveness file


def _process_new_articles() -> None:
    """Chunk always; embed and label only when an OpenAI key is configured."""
    run_pipeline(skip_llm=not get_settings().openai_api_key.get_secret_value())


def job_rss() -> None:
    with PoliteClient(min_interval=1.0) as client:
        new = coindesk_rss.poll_once(get_engine(), client)
    if new:
        _process_new_articles()


def job_candles() -> None:
    engine = get_engine()
    with PoliteClient(min_interval=0.25) as client:
        now = datetime.now(UTC)
        for symbol in get_settings().symbols:
            last = binance.last_candle_ts(engine, symbol)
            start = (last + binance.HOUR) if last else get_settings().start_date
            n = binance.upsert_candles(engine, binance.fetch_api_klines(client, symbol, start, now))
            logger.info("%s: +%d candles", symbol, n)


def job_funding() -> None:
    engine = get_engine()
    with PoliteClient(min_interval=0.25) as client:
        for symbol in get_settings().symbols:
            last = binance.last_funding_ts(engine, symbol)
            start = (last + timedelta(milliseconds=1)) if last else get_settings().start_date
            n = binance.upsert_funding(engine, binance.fetch_funding(client, symbol, start))
            logger.info("%s: +%d funding", symbol, n)


def _guardian(command: str) -> None:
    if not get_settings().guardian_api_key.get_secret_value():
        logger.warning("GUARDIAN_API_KEY not set; skipping Guardian %s", command)
        return
    engine = get_engine()
    if command == "update":
        guardian.fetch(engine, guardian.last_published(engine) or get_settings().start_date.date())
    else:
        guardian.fetch(
            engine, (datetime.now(UTC) - timedelta(days=1)).date(), use_date="last-modified"
        )
    _process_new_articles()


def job_episodes() -> None:
    for symbol in get_settings().symbols:
        build_episodes(get_engine(), symbol)


def job_daily_forecast() -> None:
    from crypto_rag.forecasting import features

    features.build(get_engine())
    try:
        from crypto_rag.forecasting.train import daily_forecast
    except ImportError:
        logger.warning(
            "forecasting models not implemented yet; features built, no forecast written"
        )
        return
    daily_forecast(get_engine())


def job_retrain() -> None:
    try:
        from crypto_rag.forecasting.train import retrain
    except ImportError:
        logger.warning("forecasting models not implemented yet; skipping retrain")
        return
    retrain(get_engine())


JOBS: dict[str, Callable[[], None]] = {
    "rss": job_rss,
    "candles": job_candles,
    "funding": job_funding,
    "guardian_update": lambda: _guardian("update"),
    "guardian_modified": lambda: _guardian("modified"),
    "episodes": job_episodes,
    "daily_forecast": job_daily_forecast,
    "retrain": job_retrain,
}

SCHEDULES: dict[str, list[tuple[str, CronTrigger]]] = {
    "collector": [
        ("rss", CronTrigger(minute="*/15", timezone=UTC)),
        ("candles", CronTrigger(minute=1, timezone=UTC)),
        ("funding", CronTrigger(hour="0,8,16", minute=2, timezone=UTC)),
        ("guardian_update", CronTrigger(hour=1, minute=0, timezone=UTC)),
        ("guardian_modified", CronTrigger(hour=1, minute=30, timezone=UTC)),
        ("episodes", CronTrigger(minute=5, timezone=UTC)),
    ],
    "forecaster": [
        ("daily_forecast", CronTrigger(hour=0, minute=5, timezone=UTC)),
        ("retrain", CronTrigger(day_of_week="mon", hour=2, timezone=UTC)),
    ],
}
# Jobs that also run once at start-up, so a fresh container catches up immediately.
ON_START = {"collector": ["candles", "funding", "rss", "episodes"], "forecaster": []}


def _heartbeat(event: JobExecutionEvent) -> None:
    HEARTBEAT.touch()
    if event.exception:
        logger.error("job %s failed: %r", event.job_id, event.exception)


def run_process(name: str) -> None:
    sched = BlockingScheduler(timezone=UTC)
    for job_id, trigger in SCHEDULES[name]:
        # coalesce + max_instances=1: a slow job is never run twice in parallel,
        # and missed runs (e.g. after downtime) collapse into one.
        sched.add_job(JOBS[job_id], trigger, id=job_id, coalesce=True, max_instances=1,
                      misfire_grace_time=600)  # fmt: skip
    for job_id in ON_START[name]:
        sched.add_job(JOBS[job_id], id=f"{job_id}_startup", next_run_time=datetime.now(UTC))
    # Keeps the heartbeat fresh even between long-interval jobs.
    sched.add_job(HEARTBEAT.touch, CronTrigger(minute="*/5", timezone=UTC), id="heartbeat")
    sched.add_listener(_heartbeat, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    HEARTBEAT.touch()
    logger.info("%s scheduler started: %s", name, [j for j, _ in SCHEDULES[name]])
    sched.start()


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Scheduler processes")
    parser.add_argument("process", choices=["collector", "forecaster", "run-once"])
    parser.add_argument("job", nargs="?", choices=sorted(JOBS))
    args = parser.parse_args()
    if args.process == "run-once":
        JOBS[args.job]()
    else:
        run_process(args.process)


if __name__ == "__main__":
    main()
