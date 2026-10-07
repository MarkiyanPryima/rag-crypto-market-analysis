"""Collector services.

Each collector fetches from one external source and pushes JSON onto a Redis
queue. None of them writes to Postgres — the worker is the only writer, which
keeps transactions in one place and means a collector crash cannot leave the
database inconsistent.

Run as modules so imports resolve the same way in Docker and under pytest:

    python -m collectors.price_collector
"""
