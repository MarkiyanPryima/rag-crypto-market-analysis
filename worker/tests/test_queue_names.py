"""The collectors and the worker must agree on every queue name.

worker/config.py deliberately repeats these constants instead of importing
from collectors: the two services ship as separate images and neither contains
the other's package. That duplication is safe only while something checks it.

If it ever diverges, nothing errors. The collector keeps pushing to
`news_queue` and the worker keeps blocking on `news-queue`, so the queue grows
without bound while the worker reports a healthy idle loop. These tests exist
to turn that silent failure into a failing build.
"""

from collectors import config as collectors_config
from worker import config as worker_config

QUEUE_ATTRS = ("PRICES_QUEUE", "NEWS_QUEUE", "SENTIMENT_QUEUE")


def test_every_queue_name_matches():
    for attr in QUEUE_ATTRS:
        produced = getattr(collectors_config, attr)
        consumed = getattr(worker_config, attr)
        assert produced == consumed, (
            f"{attr} differs: collectors push to {produced!r}, "
            f"worker reads {consumed!r} — the queue would never be drained"
        )


def test_no_queue_constant_was_added_on_one_side_only():
    """A fourth queue added to one module and not the other is the same bug."""
    collectors_queues = {a for a in dir(collectors_config) if a.endswith("_QUEUE")}
    worker_queues = {a for a in dir(worker_config) if a.endswith("_QUEUE")}

    assert collectors_queues == worker_queues, (
        f"queue constants differ: only in collectors {collectors_queues - worker_queues}, "
        f"only in worker {worker_queues - collectors_queues}"
    )


def test_queue_names_are_distinct():
    """Two constants pointing at one queue would silently interleave payloads."""
    names = [getattr(worker_config, attr) for attr in QUEUE_ATTRS]
    assert len(set(names)) == len(names), f"duplicate queue names: {names}"
