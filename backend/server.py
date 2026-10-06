"""SpectreAI API.

The workspace runtime ships with MongoDB, so this service keeps the same
provider-neutral domain model while using MongoDB for local persistence. Long
running work is represented by an asyncio worker here; the state machine is
deliberately isolated so it can be moved to Celery/Redis without changing the
mobile API.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "spectreai")
JWT_SECRET = os.environ.get("JWT_SECRET", "spectreai-local-secret-change-me")
DEVELOPMENT_MODE = os.environ.get("DEVELOPMENT_MODE", "true").lower() == "true"
MAX_SHOTS_PER_JOB = int(os.environ.get("MAX_SHOTS_PER_JOB", "80"))

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]
logger = logging.getLogger("spectreai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PIPELINE_STAGES = [
    ("STORY_GENERATION", "Story", "Writing the story spine"),
    ("CHARACTER_EXTRACTION", "Characters", "Locking recurring character identities"),
    ("CHARACTER_BIBLE", "Character Bible", "Building continuity rules"),
    ("CHARACTER_REFERENCE_GENERATION", "References", "Preparing neutral reference sheets"),
    ("STYLE_BIBLE", "Style Bible", "Defining the visual language"),
    ("SCENE_PLANNING", "Scenes", "Breaking the story into cinematic scenes"),
    ("VIDEO_PROMPT_GENERATION", "Shot Prompts", "Composing continuity-safe prompts"),
    ("VIDEO_GENERATION", "Video", "Generating development-mode clips"),
    ("VOICE_GENERATION", "Voice", "Creating the narration track"),
    ("MUSIC_GENERATION", "Music", "Shaping the atmosphere bed"),
    ("SFX_GENERATION", "SFX", "Placing horror sound cues"),
    ("SUBTITLE_GENERATION", "Subtitles", "Timing subtitles to narration"),
    ("TIMELINE_BUILD", "Timeline", "Synchronizing every layer"),
    ("VIDEO_ASSEMBLY", "Assembly", "Rendering the final development preview"),
    ("QUALITY_CHECK", "Quality Check", "Running delivery checks"),
]

app = FastAPI(title="SpectreAI API", version="1.0.0")
api = APIRouter(prefix="/api/v1")
jobs_in_flight: dict[str, asyncio.Task] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid.uuid4())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def token_for(user: dict[str, Any]) -> str:
    return jwt.encode(
        {"sub": user["id"], "email": user["email"], "exp": datetime.now(timezone.utc) + timedelta(days=7)},
        JWT_SECRET,
        algorithm="HS256",
    )


class AuthInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class ProjectInput(BaseModel):
    prompt: str = Field(min_length=12, max_length=2000)
    target_duration_minutes: int = Field(default=1, ge=1, le=10)
    visual_style: str = Field(default="2D horror animation", max_length=120)
    aspect_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    language: str = Field(default="en", min_length=2, max_length=8)
    voice_style: str = Field(default="horror narrator", max_length=120)


class CancelInput(BaseModel):
    reason: str | None = Field(default=None, max_length=240)


class UserView(BaseModel):
    id: str
    email: EmailStr


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserView


class ProjectView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    user_id: str
    prompt: str
    target_duration_minutes: int
    visual_style: str
    aspect_ratio: str
    language: str
    voice_style: str
    created_at: str
    latest_job_id: str | None = None


async def current_user(authorization: str | None = None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Sign in to continue"})
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1], JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "Session expired"}) from exc
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail={"code": "USER_NOT_FOUND", "message": "User not found"})
    return user


@app.middleware("http")
async def inject_bearer(request, call_next):
    """Allow FastAPI dependency-style auth without importing an auth framework."""
    from fastapi.security.utils import get_authorization_scheme_param

    scheme, credentials = get_authorization_scheme_param(request.headers.get("Authorization"))
    request.state.authorization = f"{scheme} {credentials}" if scheme and credentials else None
    return await call_next(request)


async def auth_user(request: Request) -> dict[str, Any]:
    return await current_user(request.state.authorization)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "development_mode": DEVELOPMENT_MODE, "service": "spectreai"}


@api.post("/auth/register", response_model=AuthResponse)
async def register(payload: AuthInput):
    email = str(payload.email).lower()
    if await db.users.find_one({"email": email}, {"_id": 0}):
        raise HTTPException(status_code=409, detail={"code": "EMAIL_EXISTS", "message": "Email already registered"})
    user = {"id": uid(), "email": email, "password_hash": hash_password(payload.password), "created_at": now_iso()}
    await db.users.insert_one(user)
    safe = {"id": user["id"], "email": user["email"]}
    return AuthResponse(access_token=token_for(safe), user=safe)


@api.post("/auth/login", response_model=AuthResponse)
async def login(payload: AuthInput):
    user = await db.users.find_one({"email": str(payload.email).lower()}, {"_id": 0})
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect"})
    safe = {"id": user["id"], "email": user["email"]}
    return AuthResponse(access_token=token_for(safe), user=safe)


@api.get("/auth/me", response_model=UserView)
async def me(user: dict[str, Any] = Depends(auth_user)):
    return user


@api.post("/projects", response_model=ProjectView)
async def create_project(payload: ProjectInput, user: dict[str, Any] = Depends(auth_user)):
    project = {"id": uid(), "user_id": user["id"], **payload.model_dump(), "created_at": now_iso(), "latest_job_id": None}
    await db.projects.insert_one(project)
    return project


@api.get("/projects", response_model=list[ProjectView])
async def list_projects(user: dict[str, Any] = Depends(auth_user)):
    return await db.projects.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(50)


async def owned_project(project_id: str, user_id: str) -> dict[str, Any]:
    project = await db.projects.find_one({"id": project_id, "user_id": user_id}, {"_id": 0})
    if not project:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND", "message": "Project not found"})
    return project


@api.get("/projects/{project_id}", response_model=ProjectView)
async def get_project(project_id: str, user: dict[str, Any] = Depends(auth_user)):
    return await owned_project(project_id, user["id"])


@api.post("/projects/{project_id}/generate")
async def generate(project_id: str, user: dict[str, Any] = Depends(auth_user)):
    project = await owned_project(project_id, user["id"])
    if project.get("latest_job_id"):
        existing = await db.jobs.find_one({"id": project["latest_job_id"], "status": {"$nin": ["COMPLETED", "FAILED", "CANCELLED"]}}, {"_id": 0})
        if existing:
            return {"job_id": existing["id"], "status": existing["status"], "message": "A production job is already running"}
    duration = project["target_duration_minutes"] * 60
    total_shots = min(MAX_SHOTS_PER_JOB, max(4, (duration + 7) // 8))
    job = {
        "id": uid(), "project_id": project_id, "user_id": user["id"], "status": "QUEUED", "stage": "QUEUED",
        "progress": 0, "current_shot": 0, "total_shots": total_shots, "message": "Queued for production",
        "created_at": now_iso(), "updated_at": now_iso(), "cancel_requested": False,
        "events": [{"at": now_iso(), "stage": "QUEUED", "status": "QUEUED", "progress": 0, "message": "Queued for production"}],
        "clips": [], "characters": [], "scenes": [], "style_bible": None, "audio": [], "timeline": None, "quality_report": None,
        "credit_status": {"allowed": DEVELOPMENT_MODE, "provider": "development-mock", "checked_at": now_iso(), "reason": "Development mode uses local adapters" if DEVELOPMENT_MODE else "Provider credits unavailable"},
    }
    await db.jobs.insert_one(job)
    await db.projects.update_one({"id": project_id}, {"$set": {"latest_job_id": job["id"]}})
    jobs_in_flight[job["id"]] = asyncio.create_task(run_pipeline(job["id"], project))
    return {"job_id": job["id"], "status": "QUEUED"}


async def update_job(job_id: str, **updates: Any) -> None:
    updates["updated_at"] = now_iso()
    await db.jobs.update_one({"id": job_id}, {"$set": updates})


async def log_stage(job_id: str, stage: str, label: str, progress: int, message: str, **extra: Any) -> None:
    event = {"at": now_iso(), "stage": stage, "status": "PROCESSING", "progress": progress, "message": message, **extra}
    await db.jobs.update_one({"id": job_id}, {"$set": {"stage": stage, "status": "PROCESSING", "progress": progress, "message": message, "updated_at": now_iso()}, "$push": {"events": event}})
    logger.info("job_id=%s stage=%s progress=%s message=%s", job_id, stage, progress, message)


def mock_story(project: dict[str, Any]) -> dict[str, Any]:
    prompt = project["prompt"]
    duration = project["target_duration_minutes"] * 60
    return {
        "title": "The Last Light in Block C", "genre": "horror", "premise": prompt,
        "setting": "An abandoned college hostel after midnight", "target_duration_seconds": duration,
        "narration": "At 2:13 every night, the corridor lights in Block C blinked in a pattern no one could explain. Mira heard the final light click off behind her. The hostel was empty, yet a door opened. Something inside whispered her name. She ran toward the stairs, but every landing returned her to the same red fire extinguisher. When the lights came back, Mira was gone. Only her phone remained, recording a second voice beneath her own. The next night, the corridor lights blinked again.",
    }


def mock_characters() -> list[dict[str, Any]]:
    return [
        {"id": uid(), "name": "Mira Sen", "role": "protagonist", "age": 21, "face": "sharp oval face", "hair": "shoulder-length black hair", "eyes": "dark brown eyes", "skin_tone": "warm brown", "body_type": "slim", "clothing": "yellow hoodie, charcoal jeans, white sneakers", "personality": "curious, increasingly terrified", "continuity_rules": "Never alter face, hair, hoodie color, or age."},
        {"id": uid(), "name": "The Corridor Voice", "role": "supernatural presence", "age": "unknown", "face": "never fully visible", "hair": "wet strands in silhouette", "eyes": "two dim red pinpoints", "skin_tone": "ashen", "body_type": "too tall and narrow", "clothing": "dark hostel uniform", "personality": "patient, predatory", "continuity_rules": "Keep the face obscured and silhouette unnaturally tall."},
    ]


def mock_style(project: dict[str, Any]) -> dict[str, Any]:
    return {"animation_style": project["visual_style"], "rendering": "hand-painted cel shading", "line_style": "uneven ink contours", "palette": ["obsidian", "dried blood red", "sickly amber"], "lighting": "flickering fluorescent pools", "camera": "slow tracking with sudden locked-off frames", "atmosphere": "humid, claustrophobic, whispering darkness", "aspect_ratio": project["aspect_ratio"], "continuity": "locked character references and recurring corridor geography"}


def build_shots(story: dict[str, Any], characters: list[dict[str, Any]], style: dict[str, Any], total: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    beats = ["empty hostel exterior in rain", "Mira enters Block C", "corridor lights blink in sequence", "a door opens by itself", "the corridor repeats", "Mira sees the tall silhouette", "phone records a second voice", "the lights go out"]
    shots = []
    scenes = []
    for index in range(total):
        scene_id = f"scene-{index // 2 + 1:02d}"
        if not any(s["id"] == scene_id for s in scenes):
            scenes.append({"id": scene_id, "number": index // 2 + 1, "title": f"Movement {index // 2 + 1}", "mood": "dread"})
        beat = beats[index % len(beats)]
        shots.append({"id": f"shot-{index + 1:03d}", "scene_id": scene_id, "shot_number": index + 1, "duration_seconds": 8, "narration_segment": beat, "characters": [characters[0]["name"]] if index not in (0, 2) else [], "environment": "abandoned college hostel corridor", "action": beat, "camera": "slow dolly forward, 35mm lens", "lighting": "flicker and deep shadow", "mood": "quiet panic", "transition": "hard cut", "video_prompt": f"Use exact locked designs from the Character Bible. {style['animation_style']}; {beat}; preserve corridor geography and continuity."})
    return scenes, shots


async def run_pipeline(job_id: str, project: dict[str, Any]) -> None:
    try:
        for index, (stage, label, message) in enumerate(PIPELINE_STAGES):
            job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
            if not job or job.get("cancel_requested"):
                await update_job(job_id, status="CANCELLED", stage="CANCELLED", message="Production cancelled", progress=job.get("progress", 0) if job else 0)
                return
            if stage == "VIDEO_GENERATION" and not job["credit_status"]["allowed"]:
                await update_job(job_id, status="WAITING_FOR_FREE_CREDITS", stage="WAITING_FOR_FREE_CREDITS", message="Generation paused: provider credits could not be confirmed")
                return
            progress = int(8 + (index / len(PIPELINE_STAGES)) * 84)
            await log_stage(job_id, stage, label, progress, message)
            await asyncio.sleep(0.25 if DEVELOPMENT_MODE else 1.0)
            if stage == "STORY_GENERATION":
                await update_job(job_id, story=mock_story(project))
            elif stage == "CHARACTER_EXTRACTION":
                await update_job(job_id, characters=mock_characters())
            elif stage == "CHARACTER_REFERENCE_GENERATION":
                refs = [{"id": uid(), "character_id": c["id"], "provider": "development-mock", "status": "COMPLETED", "description": "Full-body neutral reference sheet"} for c in job.get("characters", [])]
                await update_job(job_id, character_references=refs)
            elif stage == "STYLE_BIBLE":
                await update_job(job_id, style_bible=mock_style(project))
            elif stage == "SCENE_PLANNING":
                latest = await db.jobs.find_one({"id": job_id}, {"_id": 0})
                scenes, shots = build_shots(latest.get("story", mock_story(project)), latest.get("characters", mock_characters()), latest.get("style_bible", mock_style(project)), latest["total_shots"])
                await update_job(job_id, scenes=scenes, shots=shots)
            elif stage == "VIDEO_GENERATION":
                latest = await db.jobs.find_one({"id": job_id}, {"_id": 0})
                clips = [{"id": uid(), "shot_id": shot["id"], "scene_id": shot["scene_id"], "generation_id": uid(), "provider": "development-mock", "status": "COMPLETED", "duration_seconds": 8, "checksum": hashlib.sha256(shot["id"].encode()).hexdigest(), "width": 1920, "height": 1080, "fps": 30} for shot in latest.get("shots", [])]
                await update_job(job_id, clips=clips, current_shot=len(clips), total_shots=len(clips))
            elif stage in {"VOICE_GENERATION", "MUSIC_GENERATION", "SFX_GENERATION"}:
                latest = await db.jobs.find_one({"id": job_id}, {"_id": 0})
                audio = latest.get("audio", [])
                kind = {"VOICE_GENERATION": "narration", "MUSIC_GENERATION": "music", "SFX_GENERATION": "sfx"}[stage]
                audio.append({"id": uid(), "type": kind, "provider": "development-mock", "status": "COMPLETED", "duration_seconds": project["target_duration_minutes"] * 60})
                await update_job(job_id, audio=audio)
            elif stage == "SUBTITLE_GENERATION":
                await update_job(job_id, subtitles={"format": ["srt", "vtt"], "cues": [{"start": 0, "end": 5, "text": "At 2:13 every night, the corridor lights blinked."}]})
            elif stage == "TIMELINE_BUILD":
                await update_job(job_id, timeline={"duration_seconds": project["target_duration_minutes"] * 60, "video_tracks": 1, "audio_tracks": 3, "subtitle_tracks": 1, "sync_strategy": "trim-and-align"})
            elif stage == "VIDEO_ASSEMBLY":
                await update_job(job_id, final_asset={"filename": "spectreai-development-preview.mp4", "provider": "development-mock", "download_ready": False, "note": "Mock assembly metadata; connect FFmpeg worker for binary rendering."})
            elif stage == "QUALITY_CHECK":
                await update_job(job_id, quality_report={"passed": True, "checks": ["clip ordering", "duration", "audio tracks", "subtitle timestamps", "credit gate"], "warnings": ["Development mode preview; no binary render was requested"], "errors": []})
        await db.jobs.update_one({"id": job_id}, {"$set": {"status": "COMPLETED", "stage": "COMPLETED", "progress": 100, "message": "Production complete", "updated_at": now_iso()}, "$push": {"events": {"at": now_iso(), "stage": "COMPLETED", "status": "COMPLETED", "progress": 100, "message": "Production complete"}}})
    except Exception as exc:
        logger.exception("job_id=%s pipeline failed", job_id)
        await update_job(job_id, status="FAILED", stage="FAILED", message=f"Pipeline failed: {exc}")
    finally:
        jobs_in_flight.pop(job_id, None)


async def get_owned_job(job_id: str, user_id: str) -> dict[str, Any]:
    job = await db.jobs.find_one({"id": job_id, "user_id": user_id}, {"_id": 0})
    if not job:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Production job not found"})
    return job


@api.get("/jobs/{job_id}")
async def job_detail(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    return await get_owned_job(job_id, user["id"])


@api.get("/jobs/{job_id}/progress")
async def job_progress(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    job = await get_owned_job(job_id, user["id"])
    return {"job_id": job_id, "stage": job["stage"], "status": job["status"], "progress": job["progress"], "current_shot": job.get("current_shot", 0), "total_shots": job.get("total_shots", 0), "message": job["message"], "events": job.get("events", [])[-30:]}


@api.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, payload: CancelInput | None = None, user: dict[str, Any] = Depends(auth_user)):
    await get_owned_job(job_id, user["id"])
    await update_job(job_id, cancel_requested=True, message=(payload.reason if payload else None) or "Cancellation requested")
    return {"job_id": job_id, "status": "CANCELLING"}


@api.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    job = await get_owned_job(job_id, user["id"])
    if job["status"] not in {"FAILED", "WAITING_FOR_FREE_CREDITS", "CANCELLED"}:
        return {"job_id": job_id, "status": job["status"], "message": "Job is already active"}
    project = await owned_project(job["project_id"], user["id"])
    await update_job(job_id, cancel_requested=False, status="QUEUED", stage="QUEUED", message="Queued to resume")
    jobs_in_flight[job_id] = asyncio.create_task(run_pipeline(job_id, project))
    return {"job_id": job_id, "status": "QUEUED"}


@api.get("/jobs/{job_id}/clips")
async def clips(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    return {"job_id": job_id, "clips": (await get_owned_job(job_id, user["id"])).get("clips", [])}


@api.get("/jobs/{job_id}/audio")
async def audio(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    return {"job_id": job_id, "audio": (await get_owned_job(job_id, user["id"])).get("audio", [])}


@api.get("/jobs/{job_id}/timeline")
async def timeline(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    return {"job_id": job_id, "timeline": (await get_owned_job(job_id, user["id"])).get("timeline")}


@api.get("/jobs/{job_id}/quality-report")
async def quality_report(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    return {"job_id": job_id, "quality_report": (await get_owned_job(job_id, user["id"])).get("quality_report")}


@api.get("/jobs/{job_id}/credit-status")
async def credit_status(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    return {"job_id": job_id, **(await get_owned_job(job_id, user["id"])).get("credit_status", {})}


@api.get("/jobs/{job_id}/download")
async def download(job_id: str, user: dict[str, Any] = Depends(auth_user)):
    job = await get_owned_job(job_id, user["id"])
    final_asset = job.get("final_asset")
    if not final_asset or not final_asset.get("download_ready"):
        raise HTTPException(status_code=409, detail={"code": "ASSET_NOT_READY", "message": "A rendered MP4 is not available in development mode"})
    path = final_asset.get("path")
    return FileResponse(path, media_type="video/mp4", filename=final_asset.get("filename", "spectreai.mp4"))


@app.websocket("/ws/jobs/{job_id}")
async def job_socket(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            job = await db.jobs.find_one({"id": job_id}, {"_id": 0})
            if not job:
                await websocket.send_json({"error": "JOB_NOT_FOUND"})
                return
            await websocket.send_json({"job_id": job_id, "stage": job["stage"], "status": job["status"], "progress": job["progress"], "current_shot": job.get("current_shot", 0), "total_shots": job.get("total_shots", 0), "message": job["message"]})
            if job["status"] in {"COMPLETED", "FAILED", "CANCELLED", "WAITING_FOR_FREE_CREDITS"}:
                return
            await asyncio.sleep(0.8)
    except WebSocketDisconnect:
        return


app.include_router(api)
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("shutdown")
async def shutdown_db_client():
    for task in jobs_in_flight.values():
        task.cancel()
    client.close()