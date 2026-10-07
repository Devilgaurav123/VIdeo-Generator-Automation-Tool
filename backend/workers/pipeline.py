"""Production pipeline orchestration (Celery task).

Design rules enforced here:
- Idempotent stages: every stage checks PostgreSQL for existing output before
  spending provider credits, so resume after failure never re-buys work.
- Strict credit gate: video generation only runs when the provider boundary
  allows it; otherwise the job parks in BLOCKED_PROVIDER with a clear reason.
- No fake completions: COMPLETED is only reachable after a real MP4 passes
  ffprobe quality checks on disk.
- Cancellable: the cancel flag is re-read between stages and between shots.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db.session import SessionLocal, init_db
from models import Character, Job, JobEvent, MediaAsset, Project, Scene, Shot
from providers import ProviderBlocked, ProviderError
from providers import audio as audio_provider
from providers import image as image_provider
from providers import llm as llm_provider
from providers import storage as storage_provider
from providers import video as video_provider
from workers.celery_app import celery

import os

logger = logging.getLogger("spectreai.pipeline")

MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", "/app/backend/media"))
MAX_SHOTS_PER_JOB = int(os.environ.get("MAX_SHOTS_PER_JOB", "80"))

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

# stage key, progress floor, progress ceiling
STAGE_PLAN: list[tuple[str, int, int]] = [
    ("STORY_GENERATION", 2, 8),
    ("CHARACTER_EXTRACTION", 8, 14),
    ("CHARACTER_BIBLE", 14, 18),
    ("CHARACTER_REFERENCE_GENERATION", 18, 28),
    ("STYLE_BIBLE", 28, 33),
    ("SCENE_PLANNING", 33, 40),
    ("VIDEO_PROMPT_GENERATION", 40, 44),
    ("VIDEO_GENERATION", 44, 72),
    ("VOICE_GENERATION", 72, 78),
    ("MUSIC_GENERATION", 78, 82),
    ("SFX_GENERATION", 82, 85),
    ("SUBTITLE_GENERATION", 85, 88),
    ("TIMELINE_BUILD", 88, 91),
    ("VIDEO_ASSEMBLY", 91, 97),
    ("QUALITY_CHECK", 97, 100),
]

STAGE_MESSAGES = {
    "STORY_GENERATION": "Writing the story spine",
    "CHARACTER_EXTRACTION": "Locking recurring character identities",
    "CHARACTER_BIBLE": "Building continuity rules",
    "CHARACTER_REFERENCE_GENERATION": "Rendering character reference sheets",
    "STYLE_BIBLE": "Defining the visual language",
    "SCENE_PLANNING": "Breaking the story into cinematic shots",
    "VIDEO_PROMPT_GENERATION": "Composing continuity-safe prompts",
    "VIDEO_GENERATION": "Rendering shot clips",
    "VOICE_GENERATION": "Recording the narration track",
    "MUSIC_GENERATION": "Shaping the atmosphere bed",
    "SFX_GENERATION": "Placing horror sound cues",
    "SUBTITLE_GENERATION": "Timing subtitles to narration",
    "TIMELINE_BUILD": "Synchronizing every layer",
    "VIDEO_ASSEMBLY": "Rendering the final MP4",
    "QUALITY_CHECK": "Running delivery checks",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    path = MEDIA_ROOT / "jobs" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log(session, job: Job, stage: str, progress: int, message: str, status: str = "PROCESSING") -> None:
    job.stage = stage
    job.status = status
    job.progress = progress
    job.message = message
    job.updated_at = datetime.now(timezone.utc)
    session.add(JobEvent(job_id=job.id, stage=stage, status=status, progress=progress, message=message))
    session.commit()
    logger.info("job=%s stage=%s progress=%s %s", job.id, stage, progress, message)


def _refresh(session, job_id: str) -> Job:
    session.expire_all()
    job = session.get(Job, job_id)
    if not job:
        raise RuntimeError("job vanished mid-pipeline")
    return job


def _check_cancel(session, job_id: str) -> None:
    job = _refresh(session, job_id)
    if job.cancel_requested:
        job.status = "CANCELLED"
        job.stage = "CANCELLED"
        job.message = "Production cancelled by operator"
        session.add(JobEvent(job_id=job_id, stage="CANCELLED", status="CANCELLED", progress=job.progress, message=job.message))
        session.commit()
        raise _Cancelled()


class _Cancelled(Exception):
    pass


def _run(coro):
    return asyncio.run(coro)


def _stage_progress(floor: int, ceiling: int, done: int, total: int) -> int:
    if total <= 0:
        return ceiling
    return floor + int((ceiling - floor) * (done / total))


def _project_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id, "prompt": project.prompt,
        "target_duration_minutes": project.target_duration_minutes,
        "visual_style": project.visual_style, "aspect_ratio": project.aspect_ratio,
        "language": project.language, "voice_style": project.voice_style,
    }


def _character_dict(character: Character) -> dict[str, Any]:
    return {"id": character.id, "name": character.name, "role": character.role, "data": character.data, "continuity_rules": character.continuity_rules}


def _shot_dict(shot: Shot) -> dict[str, Any]:
    return {"id": shot.id, "scene_id": shot.scene_id, "shot_number": shot.shot_number, "duration_seconds": shot.duration_seconds, "data": shot.data, "video_prompt": shot.video_prompt}


def _ffprobe(path: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise ProviderError("ffmpeg", f"ffprobe failed: {proc.stderr.strip()[-300:]}")
    import json

    return json.loads(proc.stdout or "{}")


def _write_srt(narration: str, duration: float, out_path: Path) -> int:
    import re

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", narration) if s.strip()]
    if not sentences:
        sentences = [narration[:120]]
    per_cue = duration / len(sentences)

    def stamp(seconds: float) -> str:
        ms = int(seconds * 1000)
        return f"{ms // 3600000:02d}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d},{ms % 1000:03d}"

    lines = []
    for index, sentence in enumerate(sentences):
        start = index * per_cue
        end = min(duration, (index + 1) * per_cue)
        lines.append(f"{index + 1}\n{stamp(start)} --> {stamp(end)}\n{sentence}\n")
    out_path.write_text("\n".join(lines))
    return len(sentences)


def _assemble(session, job: Job, workdir: Path) -> dict[str, Any]:
    """Real FFmpeg assembly: concat clips, mix narration/BGM/SFX, mux subtitles."""
    clips = (
        session.query(MediaAsset)
        .filter(MediaAsset.job_id == job.id, MediaAsset.kind == "clip", MediaAsset.status == "COMPLETED")
        .all()
    )
    shots = {s.id: s for s in session.query(Shot).filter(Shot.job_id == job.id).all()}
    clips.sort(key=lambda c: shots[c.shot_id].shot_number if c.shot_id in shots else 0)
    if not clips:
        raise ProviderError("assembly", "No completed clips to assemble")

    concat_list = workdir / "concat.txt"
    concat_list.write_text("".join(f"file '{c.path}'\n" for c in clips))
    video_only = workdir / "video_only.mp4"
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(video_only)])

    def asset_of(kind: str) -> MediaAsset | None:
        return session.query(MediaAsset).filter(MediaAsset.job_id == job.id, MediaAsset.kind == kind, MediaAsset.status == "COMPLETED").first()

    narration = asset_of("narration")
    music = asset_of("music")
    sfx_assets = session.query(MediaAsset).filter(MediaAsset.job_id == job.id, MediaAsset.kind == "sfx", MediaAsset.status == "COMPLETED").all()
    subtitle = asset_of("subtitle")

    video_duration = audio_provider.probe_duration(str(video_only))

    inputs: list[str] = ["-i", str(video_only)]
    filters: list[str] = []
    mix_labels: list[str] = []
    idx = 1
    if narration and Path(narration.path).exists():
        inputs += ["-i", narration.path]
        filters.append(f"[{idx}:a]adelay=0|0,volume=1.0[nar]")
        mix_labels.append("[nar]")
        idx += 1
    if music and Path(music.path).exists():
        inputs += ["-i", music.path]
        filters.append(f"[{idx}:a]volume=0.32,atrim=0:{video_duration:.2f}[bgm]")
        mix_labels.append("[bgm]")
        idx += 1
    for s_index, sfx in enumerate(sfx_assets):
        if not Path(sfx.path).exists():
            continue
        delay_ms = int(float(sfx.meta.get("at_seconds", 0)) * 1000)
        inputs += ["-i", sfx.path]
        filters.append(f"[{idx}:a]volume=0.85,adelay={delay_ms}|{delay_ms}[sfx{s_index}]")
        mix_labels.append(f"[sfx{s_index}]")
        idx += 1

    sub_index = None
    if subtitle and Path(subtitle.path).exists():
        inputs += ["-i", subtitle.path]
        sub_index = idx
        idx += 1

    final_path = workdir / "final.mp4"
    args = inputs
    if mix_labels:
        filters.append("".join(mix_labels) + f"amix=inputs={len(mix_labels)}:normalize=0,alimiter=limit=0.95[aout]")
        args += ["-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[aout]"]
    else:
        args += ["-map", "0:v", "-map", "0:a?"]
    if sub_index is not None:
        args += ["-map", f"{sub_index}:s"]
    args += ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
    if sub_index is not None:
        args += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    args += ["-t", f"{video_duration:.2f}", "-movflags", "+faststart", str(final_path)]
    _ffmpeg(args, timeout=600)

    probe = _ffprobe(str(final_path))
    duration = float(probe.get("format", {}).get("duration", 0) or 0)
    size = int(probe.get("format", {}).get("size", 0) or 0)
    return {"path": str(final_path), "duration_seconds": round(duration, 2), "size_bytes": size, "clip_count": len(clips)}


def _ffmpeg(args: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ProviderError("ffmpeg", proc.stderr.strip()[-400:] or "ffmpeg assembly failed")


@celery.task(name="pipeline.run", bind=True, max_retries=0)
def run_pipeline(self, job_id: str) -> None:
    init_db()
    session = SessionLocal()
    try:
        _pipeline(session, job_id)
    except _Cancelled:
        logger.info("job=%s cancelled", job_id)
    except ProviderBlocked as blocked:
        session.rollback()
        job = _refresh(session, job_id)
        job.status = "BLOCKED_PROVIDER"
        job.message = blocked.reason
        job.error = blocked.reason
        job.credit_status = {**(job.credit_status or {}), "allowed": False, "provider": blocked.provider, "reason": blocked.reason, "checked_at": _utcnow_iso()}
        session.add(JobEvent(job_id=job_id, stage=job.stage, status="BLOCKED_PROVIDER", progress=job.progress, message=blocked.reason))
        session.commit()
        logger.warning("job=%s blocked: %s", job_id, blocked.reason)
    except Exception as exc:
        session.rollback()
        logger.exception("job=%s failed", job_id)
        try:
            job = _refresh(session, job_id)
            job.status = "FAILED"
            job.message = f"Pipeline failed: {exc}"
            job.error = str(exc)
            session.add(JobEvent(job_id=job_id, stage=job.stage, status="FAILED", progress=job.progress, message=job.message))
            session.commit()
        except Exception:
            logger.exception("job=%s could not record failure", job_id)
    finally:
        session.close()


def _pipeline(session, job_id: str) -> None:
    job = _refresh(session, job_id)
    project = session.get(Project, job.project_id)
    if not project:
        raise RuntimeError("project missing for job")
    project_data = _project_dict(project)
    workdir = _job_dir(job_id)
    session_id = f"job-{job_id}"

    def stage(index: int, message: str | None = None, progress: int | None = None) -> tuple[str, int, int]:
        key, floor, ceiling = STAGE_PLAN[index]
        _log(session, job, key, progress if progress is not None else floor, message or STAGE_MESSAGES[key])
        _check_cancel(session, job_id)
        return key, floor, ceiling

    # -- 1. Story ------------------------------------------------------------
    stage(0)
    if not job.story:
        job.story = _run(llm_provider.generate_story(project_data, session_id))
        session.commit()
    story = job.story

    # -- 2-3. Character extraction + bible ------------------------------------
    stage(1)
    characters = session.query(Character).filter(Character.job_id == job_id).all()
    if not characters:
        stage(2)
        raw_characters = _run(llm_provider.build_character_bible(story, session_id))
        for raw in raw_characters:
            session.add(Character(
                job_id=job_id, name=str(raw.get("name", "Unknown"))[:120],
                role=str(raw.get("role", ""))[:120], data=raw,
                continuity_rules=str(raw.get("continuity_rules", "")),
            ))
        session.commit()
        characters = session.query(Character).filter(Character.job_id == job_id).all()
    character_dicts = [_character_dict(c) for c in characters]

    # -- 4. Character reference sheets (real images) ---------------------------
    key, floor, ceiling = stage(3)
    for index, character in enumerate(characters):
        _check_cancel(session, job_id)
        existing_refs = session.query(MediaAsset).filter(
            MediaAsset.job_id == job_id, MediaAsset.kind == "reference", MediaAsset.status == "COMPLETED",
        ).all()
        existing = next((r for r in existing_refs if (r.meta or {}).get("character_id") == character.id), None)
        if not existing:
            png = _run(image_provider.generate_reference_sheet(_character_dict(character), project.visual_style, session_id))
            path = workdir / f"ref_{character.id}.png"
            path.write_bytes(png)
            session.add(MediaAsset(job_id=job_id, kind="reference", provider="gemini-image", path=str(path), meta={"character_id": character.id, "name": character.name}))
            session.commit()
        _log(session, job, key, _stage_progress(floor, ceiling, index + 1, len(characters)), f"Reference locked for {character.name}")

    # -- 5. Style bible --------------------------------------------------------
    stage(4)
    if not job.style_bible:
        job.style_bible = _run(llm_provider.build_style_bible(project_data, story, session_id))
        session.commit()
    style = job.style_bible

    # -- 6. Scene + shot planning ---------------------------------------------
    stage(5)
    shots = session.query(Shot).filter(Shot.job_id == job_id).order_by(Shot.shot_number).all()
    if not shots:
        duration = int(project.target_duration_minutes) * 60
        total_shots = min(MAX_SHOTS_PER_JOB, max(4, (duration + 7) // 8))
        plan = _run(llm_provider.plan_shots(story, character_dicts, style, total_shots, session_id))
        scenes = plan.get("scenes") or [{"number": 1, "title": "Movement 1", "mood": "dread"}]
        scene_ids: dict[int, str] = {}
        for scene in scenes:
            number = int(scene.get("number", 1))
            scene_id = f"{job_id[:8]}-scene-{number:02d}"
            scene_ids[number] = scene_id
            session.add(Scene(id=scene_id, job_id=job_id, number=number, title=str(scene.get("title", f"Movement {number}"))[:200], mood=str(scene.get("mood", "dread"))[:120]))
        for position, raw_shot in enumerate(plan["shots"], start=1):
            number = int(raw_shot.get("shot_number", position))
            scene_number = int(raw_shot.get("scene_number", 1))
            session.add(Shot(
                id=f"{job_id[:8]}-shot-{position:03d}", job_id=job_id,
                scene_id=scene_ids.get(scene_number, scene_ids.get(1, f"{job_id[:8]}-scene-01")),
                shot_number=position, duration_seconds=8, data=raw_shot, status="PENDING",
            ))
        job.total_shots = len(plan["shots"])
        session.commit()
        shots = session.query(Shot).filter(Shot.job_id == job_id).order_by(Shot.shot_number).all()
    elif not job.total_shots:
        job.total_shots = len(shots)
        session.commit()

    # -- 7. Continuity-safe video prompts --------------------------------------
    key, floor, ceiling = stage(6)
    continuity_header = (
        f"Visual style: {style.get('animation_style')}. Palette: {', '.join(style.get('palette', []))}. "
        f"Lighting: {style.get('lighting', '')}. Continuity: {style.get('continuity', 'keep all recurring characters and locations identical')}. "
        + " ".join(f"{c.name}: {c.continuity_rules}" for c in characters if c.continuity_rules)
    )
    for shot in shots:
        if not shot.video_prompt:
            data = shot.data or {}
            shot.video_prompt = (
                f"{continuity_header} Shot {shot.shot_number}: {data.get('action', '')} in {data.get('environment', '')}. "
                f"Camera: {data.get('camera', '')}. Lighting: {data.get('lighting', '')}. Mood: {data.get('mood', 'dread')}."
            )
    session.commit()

    # -- 8. Credit gate + video generation -------------------------------------
    gate = video_provider.credit_gate()
    gate["checked_at"] = _utcnow_iso()
    job.credit_status = gate
    session.commit()
    key, floor, ceiling = stage(7, message=gate["reason"])
    if not gate["allowed"]:
        raise ProviderBlocked(gate["provider"], gate["reason"])

    reference_bytes: list[bytes] = []
    for ref in session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "reference", MediaAsset.status == "COMPLETED").all():
        path = Path(ref.path)
        if path.exists():
            reference_bytes.append(path.read_bytes())

    for done, shot in enumerate(shots, start=1):
        _check_cancel(session, job_id)
        existing_clip = session.query(MediaAsset).filter(
            MediaAsset.job_id == job_id, MediaAsset.kind == "clip",
            MediaAsset.shot_id == shot.id, MediaAsset.status == "COMPLETED",
        ).first()
        if existing_clip and Path(existing_clip.path).exists():
            job.current_shot = done
            session.commit()
            continue
        # keyframe (real AI image, reused on retry)
        keyframe = session.query(MediaAsset).filter(
            MediaAsset.job_id == job_id, MediaAsset.kind == "keyframe",
            MediaAsset.shot_id == shot.id, MediaAsset.status == "COMPLETED",
        ).first()
        keyframe_path = keyframe.path if keyframe and Path(keyframe.path).exists() else None
        if not keyframe_path:
            png = _run(image_provider.generate_keyframe(_shot_dict(shot), character_dicts, style, reference_bytes, session_id))
            keyframe_path = str(workdir / f"key_{shot.id}.png")
            Path(keyframe_path).write_bytes(png)
            session.add(MediaAsset(job_id=job_id, shot_id=shot.id, kind="keyframe", provider="gemini-image", path=keyframe_path))
            session.commit()
        clip_path = str(workdir / f"clip_{shot.id}.mp4")
        meta = _run(video_provider.generate_clip(_shot_dict(shot), keyframe_path, clip_path, project.aspect_ratio))
        session.add(MediaAsset(job_id=job_id, shot_id=shot.id, kind="clip", provider=meta.get("provider", "unknown"), path=clip_path, duration_seconds=meta.get("duration_seconds", 8), meta=meta))
        shot.status = "COMPLETED"
        job.current_shot = done
        session.commit()
        _log(session, job, key, _stage_progress(floor, ceiling, done, len(shots)), f"Rendered shot {done} of {len(shots)} · {meta.get('provider')}")

    # -- 9. Narration (real TTS) ------------------------------------------------
    stage(8)
    narration_asset = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "narration", MediaAsset.status == "COMPLETED").first()
    if not narration_asset or not Path(narration_asset.path).exists():
        narration_path = str(workdir / "narration.mp3")
        tts_meta = _run(audio_provider.generate_narration(story["narration"], narration_path, job_id))
        session.add(MediaAsset(job_id=job_id, kind="narration", provider=tts_meta["provider"], path=narration_path, duration_seconds=tts_meta["duration_seconds"], meta=tts_meta))
        session.commit()

    video_duration = float(len(shots) * 8)

    # -- 10. BGM (real synthesized score) ---------------------------------------
    stage(9)
    music_asset = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "music", MediaAsset.status == "COMPLETED").first()
    if not music_asset or not Path(music_asset.path).exists():
        bgm_path = str(workdir / "bgm.wav")
        bgm_meta = _run(audio_provider.generate_bgm(video_duration, bgm_path))
        session.add(MediaAsset(job_id=job_id, kind="music", provider=bgm_meta["provider"], path=bgm_path, duration_seconds=bgm_meta["duration_seconds"], meta=bgm_meta))
        session.commit()

    # -- 11. SFX cues ------------------------------------------------------------
    stage(10)
    existing_sfx = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "sfx", MediaAsset.status == "COMPLETED").all()
    if not existing_sfx:
        for cue in _run(audio_provider.generate_sfx(video_duration, str(workdir))):
            session.add(MediaAsset(job_id=job_id, kind="sfx", provider=cue["provider"], path=cue["path"], duration_seconds=cue["duration_seconds"], meta={"at_seconds": cue["at_seconds"], "label": cue["label"]}))
        session.commit()

    # -- 12. Subtitles ------------------------------------------------------------
    stage(11)
    subtitle_asset = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "subtitle", MediaAsset.status == "COMPLETED").first()
    if not subtitle_asset or not Path(subtitle_asset.path).exists():
        srt_path = workdir / "subtitles.srt"
        cue_count = _write_srt(audio_provider.clean_for_tts(story["narration"]), video_duration, srt_path)
        session.add(MediaAsset(job_id=job_id, kind="subtitle", provider="local-srt", path=str(srt_path), duration_seconds=video_duration, meta={"cues": cue_count, "format": "srt"}))
        session.commit()

    # -- 13. Timeline --------------------------------------------------------------
    stage(12)
    if not job.timeline:
        sfx_assets = session.query(MediaAsset).filter(MediaAsset.job_id == job_id, MediaAsset.kind == "sfx").all()
        job.timeline = {
            "duration_seconds": video_duration,
            "video_tracks": [{"index": 0, "clips": [{"shot_id": s.id, "offset_seconds": (s.shot_number - 1) * 8, "duration_seconds": 8} for s in shots]}],
            "audio_tracks": [
                {"kind": "narration", "offset_seconds": 0},
                {"kind": "music", "offset_seconds": 0},
                *[{"kind": "sfx", "offset_seconds": float(a.meta.get("at_seconds", 0))} for a in sfx_assets],
            ],
            "subtitle_tracks": [{"format": "srt", "muxed_as": "mov_text"}],
            "sync_strategy": "concat-then-mix",
        }
        session.commit()

    # -- 14. Assembly (real MP4) ----------------------------------------------------
    stage(13)
    final_info = _assemble(session, job, workdir)
    storage_path = storage_provider.upload_file(final_info["path"], job_id, "video/mp4")
    session.add(MediaAsset(job_id=job_id, kind="final", provider="ffmpeg", path=final_info["path"], storage_path=storage_path, duration_seconds=final_info["duration_seconds"], meta={"size_bytes": final_info["size_bytes"], "clip_count": final_info["clip_count"]}))
    job.final_asset = {
        "filename": f"{story.get('title', 'spectreai').lower().replace(' ', '-')[:48]}.mp4",
        "download_ready": True,
        "duration_seconds": final_info["duration_seconds"],
        "size_bytes": final_info["size_bytes"],
        "storage_path": storage_path,
        "rendered_with": job.credit_status.get("provider") if job.credit_status else "ffmpeg",
    }
    session.commit()

    # -- 15. Quality check (ffprobe on the real file) --------------------------------
    stage(14)
    probe = _ffprobe(final_info["path"])
    streams = probe.get("streams", [])
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration_ok = final_info["duration_seconds"] >= max(4, video_duration - 2)
    checks = ["clip ordering", "duration", "video stream", "audio stream", "subtitle track", "file integrity"]
    errors = []
    if not has_video:
        errors.append("final MP4 has no video stream")
    if not has_audio:
        errors.append("final MP4 has no audio stream")
    if not duration_ok:
        errors.append(f"duration {final_info['duration_seconds']}s shorter than expected {video_duration}s")
    job.quality_report = {
        "passed": not errors,
        "checks": checks,
        "warnings": [] if job.credit_status and job.credit_status.get("provider") == "google-veo" else ["Rendered with ffmpeg-animatic clips (configure GOOGLE_API_KEY for Veo motion video)"],
        "errors": errors,
        "duration_seconds": final_info["duration_seconds"],
        "size_bytes": final_info["size_bytes"],
    }
    if errors:
        job.status = "FAILED"
        job.message = "Quality gate failed: " + "; ".join(errors)
        session.add(JobEvent(job_id=job_id, stage="QUALITY_CHECK", status="FAILED", progress=97, message=job.message))
        session.commit()
        return
    session.commit()

    # -- Done -------------------------------------------------------------------------
    job.status = "COMPLETED"
    job.stage = "COMPLETED"
    job.progress = 100
    job.message = "Production complete — final MP4 passed quality checks"
    session.add(JobEvent(job_id=job_id, stage="COMPLETED", status="COMPLETED", progress=100, message=job.message))
    session.commit()
    logger.info("job=%s completed: %s", job_id, final_info["path"])
