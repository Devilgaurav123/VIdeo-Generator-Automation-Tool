"""SpectreAI API — production service.

FastAPI surface over PostgreSQL (SQLAlchemy) with Celery/Redis dispatching the
long-running generation pipeline. No mocks: jobs only reach COMPLETED after a
real MP4 exists on disk and passes ffprobe quality checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from pydantic import BaseModel, ConfigDict, EmailStr, Field

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from db.session import SessionLocal, init_db  # noqa: E402
from models import Character, Job, JobEvent, MediaAsset, Project, Scene, Shot, User  # noqa: E402

JWT_SECRET = os.environ["JWT_SECRET"]
MAX_SHOTS_PER_JOB = int(os.environ.get("MAX_SHOTS_PER_JOB", "80"))

logger = logging.getLogger("spectreai")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED_PROVIDER"}

app = FastAPI(title="SpectreAI API", version="2.0.0")
api = APIRouter(prefix="/api/v1")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False


def token_for(user: User) -> str:
    return jwt.encode(
        {"sub": user.id, "email": user.email, "exp": datetime.now(timezone.utc) + timedelta(days=7)},
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


@app.middleware("http")
async def inject_bearer(request, call_next):
    from fastapi.security.utils import get_authorization_scheme_param

    scheme, credentials = get_authorization_scheme_param(request.headers.get("Authorization"))
    request.state.authorization = f"{scheme} {credentials}" if scheme and credentials else None
    return await call_next(request)


def _user_from_token(authorization: str | None) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Sign in to continue"})
    try:
        payload = jwt.decode(authorization.split(" ", 1)[1], JWT_SECRET, algorithms=["HS256"])
        user_id = payload.get("sub")
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail={"code": "INVALID_TOKEN", "message": "Session expired"}) from exc
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if not user:
            raise HTTPException(status_code=401, detail={"code": "USER_NOT_FOUND", "message": "User not found"})
        session.expunge(user)
        return user


async def auth_user(request: Request) -> User:
    return _user_from_token(request.state.authorization)


def project_view(project: Project) -> dict[str, Any]:
    return {
        "id": project.id, "user_id": project.user_id, "prompt": project.prompt,
        "target_duration_minutes": project.target_duration_minutes,
        "visual_style": project.visual_style, "aspect_ratio": project.aspect_ratio,
        "language": project.language, "voice_style": project.voice_style,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "latest_job_id": project.latest_job_id,
    }


def _asset_view(asset: MediaAsset) -> dict[str, Any]:
    return {
        "id": asset.id, "kind": asset.kind, "provider": asset.provider, "status": asset.status,
        "shot_id": asset.shot_id, "duration_seconds": asset.duration_seconds,
        "storage_path": asset.storage_path, "meta": asset.meta or {},
        "download_ready": bool(asset.path and Path(asset.path).exists()),
    }


def job_view(session, job: Job) -> dict[str, Any]:
    characters = session.query(Character).filter(Character.job_id == job.id).all()
    scenes = session.query(Scene).filter(Scene.job_id == job.id).order_by(Scene.number).all()
    shots = session.query(Shot).filter(Shot.job_id == job.id).order_by(Shot.shot_number).all()
    assets = session.query(MediaAsset).filter(MediaAsset.job_id == job.id).all()
    events = (
        session.query(JobEvent).filter(JobEvent.job_id == job.id)
        .order_by(JobEvent.id.desc()).limit(30).all()
    )
    events.reverse()
    clips = [a for a in assets if a.kind == "clip"]
    audio = [a for a in assets if a.kind in ("narration", "music", "sfx")]
    return {
        "id": job.id, "project_id": job.project_id, "user_id": job.user_id,
        "status": job.status, "stage": job.stage, "progress": job.progress,
        "current_shot": job.current_shot, "total_shots": job.total_shots,
        "message": job.message, "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "story": job.story, "style_bible": job.style_bible, "timeline": job.timeline,
        "quality_report": job.quality_report, "credit_status": job.credit_status,
        "final_asset": job.final_asset,
        "characters": [{"id": c.id, "name": c.name, "role": c.role, "continuity_rules": c.continuity_rules, **{k: v for k, v in (c.data or {}).items() if k not in {"name", "role", "continuity_rules"}}} for c in characters],
        "scenes": [{"id": s.id, "number": s.number, "title": s.title, "mood": s.mood} for s in scenes],
        "shots": [{"id": s.id, "shot_number": s.shot_number, "scene_id": s.scene_id, "status": s.status, "duration_seconds": s.duration_seconds} for s in shots],
        "clips": [_asset_view(a) for a in clips],
        "audio": [_asset_view(a) for a in audio],
        "assets": [_asset_view(a) for a in assets],
        "events": [
            {"at": e.at.isoformat() if e.at else None, "stage": e.stage, "status": e.status, "progress": e.progress, "message": e.message}
            for e in events
        ],
    }


def owned_project(session, project_id: str, user_id: str) -> Project:
    project = session.get(Project, project_id)
    if not project or project.user_id != user_id:
        raise HTTPException(status_code=404, detail={"code": "PROJECT_NOT_FOUND", "message": "Project not found"})
    return project


def owned_job(session, job_id: str, user_id: str) -> Job:
    job = session.get(Job, job_id)
    if not job or job.user_id != user_id:
        raise HTTPException(status_code=404, detail={"code": "JOB_NOT_FOUND", "message": "Production job not found"})
    return job


def dispatch_pipeline(job_id: str) -> None:
    from workers.celery_app import celery

    celery.send_task("pipeline.run", args=[job_id])


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from providers.video import credit_gate

    gate = credit_gate()
    return {"status": "ok", "service": "spectreai", "mode": "production", "video_provider": gate["provider"], "video_credits_allowed": gate["allowed"]}


@api.post("/auth/register", response_model=AuthResponse)
async def register(payload: AuthInput):
    email = str(payload.email).lower()
    with SessionLocal() as session:
        if session.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=409, detail={"code": "EMAIL_EXISTS", "message": "Email already registered"})
        user = User(email=email, password_hash=hash_password(payload.password))
        session.add(user)
        session.commit()
        session.refresh(user)
        return AuthResponse(access_token=token_for(user), user=UserView(id=user.id, email=user.email))


@api.post("/auth/login", response_model=AuthResponse)
async def login(payload: AuthInput):
    with SessionLocal() as session:
        user = session.query(User).filter(User.email == str(payload.email).lower()).first()
        if not user or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect"})
        return AuthResponse(access_token=token_for(user), user=UserView(id=user.id, email=user.email))


@api.get("/auth/me", response_model=UserView)
async def me(user: User = Depends(auth_user)):
    return UserView(id=user.id, email=user.email)


@api.post("/projects", response_model=ProjectView)
async def create_project(payload: ProjectInput, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        project = Project(user_id=user.id, **payload.model_dump())
        session.add(project)
        session.commit()
        session.refresh(project)
        return project_view(project)


@api.get("/projects", response_model=list[ProjectView])
async def list_projects(user: User = Depends(auth_user)):
    with SessionLocal() as session:
        projects = session.query(Project).filter(Project.user_id == user.id).order_by(Project.created_at.desc()).limit(50).all()
        return [project_view(p) for p in projects]


@api.get("/projects/{project_id}", response_model=ProjectView)
async def get_project(project_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        return project_view(owned_project(session, project_id, user.id))


@api.post("/projects/{project_id}/generate")
async def generate(project_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        project = owned_project(session, project_id, user.id)
        if project.latest_job_id:
            existing = session.get(Job, project.latest_job_id)
            if existing and existing.status not in TERMINAL_STATUSES:
                return {"job_id": existing.id, "status": existing.status, "message": "A production job is already running"}
        duration = project.target_duration_minutes * 60
        total_shots = min(MAX_SHOTS_PER_JOB, max(4, (duration + 7) // 8))
        job = Job(
            id=str(uuid.uuid4()), project_id=project_id, user_id=user.id,
            status="QUEUED", stage="QUEUED", progress=0, current_shot=0,
            total_shots=total_shots, message="Queued for production",
        )
        session.add(job)
        session.add(JobEvent(job_id=job.id, stage="QUEUED", status="QUEUED", progress=0, message="Queued for production"))
        project.latest_job_id = job.id
        session.commit()
    dispatch_pipeline(job.id)
    return {"job_id": job.id, "status": "QUEUED"}


@api.get("/jobs/{job_id}")
async def job_detail(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        return job_view(session, owned_job(session, job_id, user.id))


@api.get("/jobs/{job_id}/progress")
async def job_progress(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        job = owned_job(session, job_id, user.id)
        events = session.query(JobEvent).filter(JobEvent.job_id == job.id).order_by(JobEvent.id.desc()).limit(30).all()
        events.reverse()
        return {
            "job_id": job.id, "stage": job.stage, "status": job.status, "progress": job.progress,
            "current_shot": job.current_shot, "total_shots": job.total_shots, "message": job.message,
            "events": [{"at": e.at.isoformat() if e.at else None, "stage": e.stage, "status": e.status, "progress": e.progress, "message": e.message} for e in events],
        }


@api.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, payload: CancelInput | None = None, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        job = owned_job(session, job_id, user.id)
        if job.status in TERMINAL_STATUSES:
            return {"job_id": job_id, "status": job.status, "message": "Job already finished"}
        job.cancel_requested = True
        job.message = (payload.reason if payload else None) or "Cancellation requested"
        session.commit()
    return {"job_id": job_id, "status": "CANCELLING"}


@api.post("/jobs/{job_id}/resume")
async def resume_job(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        job = owned_job(session, job_id, user.id)
        if job.status not in {"FAILED", "BLOCKED_PROVIDER", "WAITING_FOR_CREDITS", "WAITING_FOR_FREE_CREDITS", "CANCELLED"}:
            return {"job_id": job_id, "status": job.status, "message": "Job is already active"}
        job.cancel_requested = False
        job.status = "QUEUED"
        job.stage = "QUEUED"
        job.error = None
        job.message = "Queued to resume — completed stages are skipped, no credits re-spent"
        session.add(JobEvent(job_id=job_id, stage="QUEUED", status="QUEUED", progress=job.progress, message=job.message))
        session.commit()
    dispatch_pipeline(job_id)
    return {"job_id": job_id, "status": "QUEUED"}


@api.get("/jobs/{job_id}/clips")
async def clips(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        owned_job(session, job_id, user.id)
        assets = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "clip").all()
        return {"job_id": job_id, "clips": [_asset_view(a) for a in assets]}


@api.get("/jobs/{job_id}/audio")
async def audio(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        owned_job(session, job_id, user.id)
        assets = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind.in_(["narration", "music", "sfx"])).all()
        return {"job_id": job_id, "audio": [_asset_view(a) for a in assets]}


@api.get("/jobs/{job_id}/timeline")
async def timeline(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        return {"job_id": job_id, "timeline": owned_job(session, job_id, user.id).timeline}


@api.get("/jobs/{job_id}/quality-report")
async def quality_report(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        return {"job_id": job_id, "quality_report": owned_job(session, job_id, user.id).quality_report}


@api.get("/jobs/{job_id}/credit-status")
async def credit_status(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        return {"job_id": job_id, **(owned_job(session, job_id, user.id).credit_status or {})}


@api.get("/jobs/{job_id}/download")
async def download(job_id: str, user: User = Depends(auth_user)):
    with SessionLocal() as session:
        job = owned_job(session, job_id, user.id)
        final_asset = job.final_asset or {}
        if not final_asset.get("download_ready"):
            raise HTTPException(status_code=404, detail={"code": "ASSET_NOT_READY", "message": "No rendered MP4 exists for this job"})
        final = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "final", MediaAsset.status == "COMPLETED").order_by(MediaAsset.created_at.desc()).first()
        if not final or not Path(final.path).exists():
            raise HTTPException(status_code=404, detail={"code": "ASSET_MISSING", "message": "Rendered file is missing from storage"})
        return FileResponse(final.path, media_type="video/mp4", filename=final_asset.get("filename", "spectreai.mp4"))


@api.get("/assets/{asset_id}/file")
async def asset_file(asset_id: str, request: Request, token: str | None = None):
    """Stream a generated media file. Accepts Bearer header or ?token= for native players."""
    authorization = request.state.authorization or (f"Bearer {token}" if token else None)
    user = _user_from_token(authorization)
    with SessionLocal() as session:
        asset = session.get(MediaAsset, asset_id)
        if not asset:
            raise HTTPException(status_code=404, detail={"code": "ASSET_NOT_FOUND", "message": "Asset not found"})
        job = session.get(Job, asset.job_id)
        if not job or job.user_id != user.id:
            raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Not your asset"})
        if not asset.path or not Path(asset.path).exists():
            raise HTTPException(status_code=404, detail={"code": "FILE_MISSING", "message": "File missing from storage"})
        media_type = {
            "clip": "video/mp4", "final": "video/mp4",
            "narration": "audio/mpeg", "music": "audio/wav", "sfx": "audio/wav",
            "subtitle": "application/x-subrip",
        }.get(asset.kind)
        if media_type is None:  # images: sniff real format (Gemini may return JPEG)
            header = Path(asset.path).read_bytes()[:4]
            media_type = "image/jpeg" if header[:2] == b"\xff\xd8" else "image/png"
        return FileResponse(asset.path, media_type=media_type)


@app.websocket("/ws/jobs/{job_id}")
async def job_socket(websocket: WebSocket, job_id: str):
    await websocket.accept()
    try:
        while True:
            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if not job:
                    await websocket.send_json({"error": "JOB_NOT_FOUND"})
                    return
                payload = {
                    "job_id": job.id, "stage": job.stage, "status": job.status, "progress": job.progress,
                    "current_shot": job.current_shot, "total_shots": job.total_shots, "message": job.message,
                }
                done = job.status in TERMINAL_STATUSES
            await websocket.send_json(payload)
            if done:
                return
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return


@app.on_event("startup")
async def startup() -> None:
    for attempt in range(15):
        try:
            init_db()
            break
        except Exception as exc:
            logger.warning("database not ready (attempt %s): %s", attempt + 1, exc)
            await asyncio.sleep(2)
    else:
        raise RuntimeError("PostgreSQL unavailable after retries")
    try:
        from providers.storage import init_storage

        init_storage()
        logger.info("object storage initialized")
    except Exception as exc:
        logger.warning("object storage init deferred: %s", exc)


app.include_router(api)
app.add_middleware(CORSMiddleware, allow_credentials=True, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
