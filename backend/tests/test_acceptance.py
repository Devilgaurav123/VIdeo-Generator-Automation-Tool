"""Production acceptance suite for SpectreAI (PostgreSQL + Celery/Redis pipeline).

Covers: health mode, auth (register conflict/login/guards), project listing,
the completed reference job structure, credit-status gate, real MP4 download
(ffprobe-verified), media asset streaming with token, and 404 boundaries.
Heavy pipeline E2E (generate -> cancel -> resume -> complete) is exercised via
the UI-driven run and verified through these same endpoints.
"""
import os
import subprocess
import uuid

import pytest
import requests

BASE_URL = (os.environ.get("EXPO_PUBLIC_BACKEND_URL") or os.environ.get("EXPO_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    pytest.skip("EXPO_PUBLIC_BACKEND_URL is required", allow_module_level=True)

EMAIL = "spectreai.tester@example.com"
PASSWORD = "SpectreTest123!"
# Reference job that completed successfully during bring-up (kept for regression).
REFERENCE_JOB = "2b258f0a-e58e-465c-9778-b020e7f06353"


@pytest.fixture(scope="module")
def api():
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    return session


@pytest.fixture(scope="module")
def token(api):
    resp = api.post(f"{BASE_URL}/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=20)
    assert resp.status_code == 200, f"login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def auth(api, token):
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    return session


# ---------------- health ----------------
def test_health_reports_production_and_animatic_provider(api):
    resp = api.get(f"{BASE_URL}/api/health", timeout=15)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "production"
    assert body["video_provider"] == "ffmpeg-animatic"
    assert body["video_credits_allowed"] is True


# ---------------- auth ----------------
def test_register_existing_email_conflicts(api):
    resp = api.post(f"{BASE_URL}/api/v1/auth/register", json={"email": EMAIL, "password": PASSWORD}, timeout=20)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "EMAIL_EXISTS"


def test_login_success_and_me(api, token):
    me = api.get(f"{BASE_URL}/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL


def test_login_wrong_password_rejected(api):
    resp = api.post(f"{BASE_URL}/api/v1/auth/login", json={"email": EMAIL, "password": "WrongPass123!"}, timeout=15)
    assert resp.status_code == 401
    assert resp.json()["detail"]["code"] == "INVALID_CREDENTIALS"


def test_projects_require_auth(api):
    resp = api.get(f"{BASE_URL}/api/v1/projects", timeout=15)
    assert resp.status_code == 401


# ---------------- projects ----------------
def test_projects_list_contains_reference_project(auth):
    resp = auth.get(f"{BASE_URL}/api/v1/projects", timeout=15)
    assert resp.status_code == 200
    projects = resp.json()
    assert isinstance(projects, list) and projects, "expected at least one project"
    assert all("id" in p and "prompt" in p for p in projects)
    assert not any("_id" in p for p in projects)


# ---------------- reference job (completed) ----------------
def test_reference_job_completed_structure(auth):
    job = auth.get(f"{BASE_URL}/api/v1/jobs/{REFERENCE_JOB}", timeout=20).json()
    assert job["status"] == "COMPLETED"
    assert job["progress"] == 100
    assert job["total_shots"] == 8
    assert len(job["clips"]) == job["total_shots"], "clips must match total shots (no missing/dupes)"
    assert all(c["download_ready"] for c in job["clips"])
    assert job["quality_report"]["passed"] is True
    assert job["final_asset"]["download_ready"] is True
    assert job["final_asset"]["size_bytes"] > 1_000_000
    assert job["final_asset"]["rendered_with"] == "ffmpeg-animatic"


def test_credit_status_gate(auth):
    resp = auth.get(f"{BASE_URL}/api/v1/jobs/{REFERENCE_JOB}/credit-status", timeout=15)
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "ffmpeg-animatic"
    assert body["allowed"] is True
    assert body["reason"], "expected an explanatory reason"


def test_progress_endpoint_shape(auth):
    resp = auth.get(f"{BASE_URL}/api/v1/jobs/{REFERENCE_JOB}/progress", timeout=15)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "COMPLETED" and body["progress"] == 100
    stages_seen = {e["stage"] for e in body["events"]}
    assert "QUALITY_CHECK" in stages_seen or "COMPLETED" in stages_seen


# ---------------- download ----------------
def test_download_streams_real_mp4(auth, tmp_path):
    resp = auth.get(f"{BASE_URL}/api/v1/jobs/{REFERENCE_JOB}/download", timeout=120)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("video/mp4")
    payload = resp.content
    assert len(payload) > 1_000_000, "final MP4 must be non-trivial (>1MB)"
    mp4 = tmp_path / "final.mp4"
    mp4.write_bytes(payload)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(mp4)],
        capture_output=True, text=True, timeout=60,
    )
    assert probe.returncode == 0, "downloaded file must be a valid MP4"
    import json

    info = json.loads(probe.stdout)
    codecs = {(s["codec_type"], s["codec_name"]) for s in info["streams"]}
    assert ("video", "h264") in codecs
    assert ("audio", "aac") in codecs
    assert ("subtitle", "mov_text") in codecs
    assert float(info["format"]["duration"]) >= 56, "1-minute production should be ~64s"


def test_download_requires_auth(api):
    resp = api.get(f"{BASE_URL}/api/v1/jobs/{REFERENCE_JOB}/download", timeout=15)
    assert resp.status_code == 401


# ---------------- asset streaming ----------------
def _asset_id(auth, kind):
    job = auth.get(f"{BASE_URL}/api/v1/jobs/{REFERENCE_JOB}", timeout=20).json()
    return next(a["id"] for a in job["assets"] if a["kind"] == kind)


def test_asset_stream_keyframe_image(api, token, auth):
    asset_id = _asset_id(auth, "keyframe")
    resp = api.get(f"{BASE_URL}/api/v1/assets/{asset_id}/file?token={token}", timeout=60)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("image/")
    assert len(resp.content) > 50_000, "AI keyframe should be a real image"


def test_asset_stream_clip_video(api, token, auth):
    asset_id = _asset_id(auth, "clip")
    resp = api.get(f"{BASE_URL}/api/v1/assets/{asset_id}/file?token={token}", timeout=60)
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("video/mp4")
    assert len(resp.content) > 100_000


def test_asset_stream_requires_token(api, auth):
    asset_id = _asset_id(auth, "clip")
    resp = api.get(f"{BASE_URL}/api/v1/assets/{asset_id}/file", timeout=15)
    assert resp.status_code == 401


# ---------------- boundaries ----------------
def test_unknown_job_404(auth):
    resp = auth.get(f"{BASE_URL}/api/v1/jobs/{uuid.uuid4()}", timeout=15)
    assert resp.status_code == 404


def test_download_of_non_completed_job_404(auth):
    """Create a project+job then immediately cancel: download must 404 while not completed."""
    project = auth.post(
        f"{BASE_URL}/api/v1/projects",
        json={"prompt": "TEST_boundary: a corridor that breathes when unobserved.", "target_duration_minutes": 1},
        timeout=20,
    )
    assert project.status_code == 200
    started = auth.post(f"{BASE_URL}/api/v1/projects/{project.json()['id']}/generate", timeout=20)
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    try:
        resp = auth.get(f"{BASE_URL}/api/v1/jobs/{job_id}/download", timeout=15)
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] in ("ASSET_NOT_READY", "ASSET_MISSING")
    finally:
        auth.post(f"{BASE_URL}/api/v1/jobs/{job_id}/cancel", json={"reason": "TEST boundary cleanup"}, timeout=15)
