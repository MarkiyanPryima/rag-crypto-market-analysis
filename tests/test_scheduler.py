from crypto_rag.scheduler import jobs


def test_every_scheduled_job_exists_and_cadence_matches_plan():
    for process, entries in jobs.SCHEDULES.items():
        for job_id, _ in entries:
            assert job_id in jobs.JOBS, (process, job_id)
    triggers = {j: str(t) for j, t in jobs.SCHEDULES["collector"]}
    assert "minute='*/15'" in triggers["rss"]
    assert "hour='0,8,16'" in triggers["funding"]
    forecaster = {j: str(t) for j, t in jobs.SCHEDULES["forecaster"]}
    assert "hour='0', minute='5'" in forecaster["daily_forecast"]
    assert "day_of_week='mon'" in forecaster["retrain"]


def test_collector_skips_paid_steps_unless_enabled(monkeypatch):
    from pydantic import SecretStr

    from crypto_rag.config import get_settings

    calls = []
    monkeypatch.setattr(jobs, "run_pipeline", lambda skip_llm: calls.append(skip_llm))
    s = get_settings()
    monkeypatch.setattr(s, "openai_api_key", SecretStr("sk-test"))
    monkeypatch.setattr(s, "llm_processing_enabled", False)
    jobs._process_new_articles()
    monkeypatch.setattr(s, "llm_processing_enabled", True)
    jobs._process_new_articles()
    assert calls == [True, False]


def test_jobs_missed_during_sleep_still_run(monkeypatch):
    captured = []

    class FakeScheduler:
        def __init__(self, timezone):
            pass

        def add_job(self, func, trigger=None, **kw):
            captured.append(kw)

        def add_listener(self, *a, **kw):
            pass

        def start(self):
            pass

    monkeypatch.setattr(jobs, "BlockingScheduler", FakeScheduler)
    monkeypatch.setattr(jobs, "HEARTBEAT", type("H", (), {"touch": staticmethod(lambda: None)})())
    jobs.run_process("collector")
    scheduled = [kw for kw in captured if kw.get("id") in dict(jobs.SCHEDULES["collector"])]
    assert scheduled and all(
        kw["misfire_grace_time"] is None and kw["coalesce"] for kw in scheduled
    )
