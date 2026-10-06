"""Acceptance coverage for auth, projects, mock production, cancellation, and persisted outputs."""
import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL")
if not BASE_URL:
    pytest.skip("EXPO_PUBLIC_BACKEND_URL is required", allow_module_level=True)
BASE_URL = BASE_URL.rstrip("/")


def test_health_and_production_acceptance():
    session = requests.Session()
    health = session.get(f"{BASE_URL}/api/health", timeout=15)
    assert health.status_code == 200
    assert health.json()["development_mode"] is True

    email = f"TEST_{uuid.uuid4().hex}@example.com"
    credentials = {"email": email, "password": "SpectreTest123!"}
    registered = session.post(f"{BASE_URL}/api/v1/auth/register", json=credentials, timeout=15)
    assert registered.status_code == 200
    token = registered.json()["access_token"]
    session.headers.update({"Authorization": f"Bearer {token}"})
    assert session.get(f"{BASE_URL}/api/v1/auth/me", timeout=15).json()["email"] == email.lower()

    project = session.post(
        f"{BASE_URL}/api/v1/projects",
        json={"prompt": "A haunted college classroom where the lights whisper at midnight."},
        timeout=15,
    )
    assert project.status_code == 200
    project_data = project.json()
    project_id = project_data["id"]
    started = session.post(f"{BASE_URL}/api/v1/projects/{project_id}/generate", timeout=15)
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    assert started.json()["status"] == "QUEUED"

    # Cancel an active job, then verify resume and terminal persisted artifacts.
    cancelled = session.post(f"{BASE_URL}/api/v1/jobs/{job_id}/cancel", json={}, timeout=15)
    assert cancelled.status_code == 200
    deadline = time.time() + 10
    job = {}
    while time.time() < deadline:
        job = session.get(f"{BASE_URL}/api/v1/jobs/{job_id}", timeout=15).json()
        if job["status"] == "CANCELLED":
            break
        time.sleep(0.25)
    assert job["status"] == "CANCELLED"
    assert session.post(f"{BASE_URL}/api/v1/jobs/{job_id}/resume", timeout=15).status_code == 200

    deadline = time.time() + 20
    while time.time() < deadline:
        job = session.get(f"{BASE_URL}/api/v1/jobs/{job_id}", timeout=15).json()
        if job["status"] == "COMPLETED":
            break
        time.sleep(0.5)
    assert job["status"] == "COMPLETED"
    assert job["progress"] == 100
    assert len(job["clips"]) == 8
    assert len(job["audio"]) == 3
    assert job["quality_report"]["passed"] is True
    assert job["timeline"]["audio_tracks"] == 3
    assert job["credit_status"]["allowed"] is True
    assert session.get(f"{BASE_URL}/api/v1/jobs/{job_id}/download", timeout=15).status_code == 409