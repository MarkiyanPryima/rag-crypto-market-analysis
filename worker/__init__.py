"""Worker service.

Drains the Redis queues filled by the collectors and is the **only** component
that writes to Postgres. Keeping writes in one process means every transaction
boundary, conflict rule and integrity constraint lives in one place, and a
collector crash can never leave the database half-updated.

Run as a module so imports resolve the same way in Docker and under pytest:

    python -m worker.worker
"""
