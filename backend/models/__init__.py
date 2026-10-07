"""SpectreAI relational schema (PostgreSQL via SQLAlchemy)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.session import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    target_duration_minutes: Mapped[int] = mapped_column(Integer, default=1)
    visual_style: Mapped[str] = mapped_column(String(120), default="2D horror animation")
    aspect_ratio: Mapped[str] = mapped_column(String(8), default="16:9")
    language: Mapped[str] = mapped_column(String(8), default="en")
    voice_style: Mapped[str] = mapped_column(String(120), default="horror narrator")
    latest_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="QUEUED")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_shot: Mapped[int] = mapped_column(Integer, default=0)
    total_shots: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="Queued for production")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    story: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    style_bible: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    timeline: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    quality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    credit_status: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    final_asset: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(120), default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    continuity_rules: Mapped[str] = mapped_column(Text, default="")


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200), default="")
    mood: Mapped[str] = mapped_column(String(120), default="")


class Shot(Base):
    __tablename__ = "shots"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), index=True)
    scene_id: Mapped[str] = mapped_column(String(40), default="")
    shot_number: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=8)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    video_prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)  # image|keyframe|clip|narration|music|sfx|subtitle|final
    provider: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(24), default="COMPLETED")
    path: Mapped[str] = mapped_column(Text, default="")
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stage: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
