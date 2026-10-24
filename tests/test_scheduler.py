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
