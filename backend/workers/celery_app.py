"""Celery application bound to Redis. Tasks live in workers.pipeline."""

from __future__ import annotations

import os
from pathlib import Path

from celery import Celery
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery = Celery("spectreai", broker=REDIS_URL, backend=REDIS_URL, include=["workers.pipeline"])
celery.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
)
