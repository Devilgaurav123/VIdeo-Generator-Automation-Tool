"""Storage provider: Emergent Managed Object Storage.

Durable copy of generated media. Local disk (MEDIA_ROOT) stays the primary
serving path for the preview environment; object storage is the production
backup. All failures are logged and non-fatal to the pipeline.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

logger = logging.getLogger("spectreai.storage")

STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
EMERGENT_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
APP_NAME = os.environ.get("APP_NAME", "spectreai")

_storage_key: str | None = None


def init_storage() -> str:
    global _storage_key
    if _storage_key:
        return _storage_key
    resp = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": EMERGENT_KEY}, timeout=30)
    resp.raise_for_status()
    _storage_key = resp.json()["storage_key"]
    return _storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    resp = requests.put(
        f"{STORAGE_URL}/objects/{path}",
        headers={"X-Storage-Key": key, "Content-Type": content_type},
        data=data,
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()


def upload_file(local_path: str, job_id: str, content_type: str) -> str | None:
    """Best-effort durable copy. Returns the storage path or None on failure."""
    global _storage_key
    try:
        file_path = Path(local_path)
        if not file_path.exists():
            return None
        storage_path = f"{APP_NAME}/renders/{job_id}/{file_path.name}"
        try:
            result = put_object(storage_path, file_path.read_bytes(), content_type)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 503:
                _storage_key = None  # stale key: reset and retry once
                result = put_object(storage_path, file_path.read_bytes(), content_type)
            else:
                raise
        return result.get("path", storage_path)
    except Exception as exc:  # never let backup storage kill a finished render
        logger.warning("object storage upload failed for %s: %s", local_path, exc)
        return None
