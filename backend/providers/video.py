"""Video provider boundary: Google Veo adapter + honest animatic fallback.

Credit protection contract:
- GOOGLE_API_KEY present  -> real Veo generation (spends the user's own quota).
- No key + RENDER_FALLBACK_MODE=animatic -> real MP4 clips rendered locally by
  FFmpeg from AI-generated keyframes. Provider is honestly labeled
  "ffmpeg-animatic"; no paid video credits are touched and nothing is faked.
- No key + fallback disabled -> ProviderBlocked; the job parks in
  BLOCKED_PROVIDER until a key is configured. Never a fake completion.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from providers import ProviderBlocked, ProviderError

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
VEO_MODEL = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-preview")
RENDER_FALLBACK_MODE = os.environ.get("RENDER_FALLBACK_MODE", "animatic").strip().lower()

ASPECT_SIZES = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960)}


def credit_gate() -> dict[str, Any]:
    """Decide whether the VIDEO_GENERATION stage may run, and with which provider."""
    if GOOGLE_API_KEY:
        return {"allowed": True, "provider": "google-veo", "reason": "Veo API key configured; per-clip generation enabled"}
    if RENDER_FALLBACK_MODE == "animatic":
        return {"allowed": True, "provider": "ffmpeg-animatic", "reason": "No Veo key configured: rendering real animatic clips from AI keyframes (no paid video credits spent)"}
    return {"allowed": False, "provider": "google-veo", "reason": "GOOGLE_API_KEY is missing and animatic fallback is disabled; video generation is blocked to protect provider credits"}


def _run_ffmpeg(args: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ProviderError("ffmpeg", proc.stderr.strip()[-400:] or "ffmpeg render failed")


def _animatic_clip(keyframe_path: str, out_path: str, duration: int, aspect: str) -> dict[str, Any]:
    """Ken-Burns style motion clip rendered from a real generated keyframe."""
    width, height = ASPECT_SIZES.get(aspect, ASPECT_SIZES["16:9"])
    frames = max(25 * duration, 25)
    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(zoom+0.0009,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={width}x{height}:fps=25,"
        "format=yuv420p"
    )
    _run_ffmpeg([
        "-loop", "1", "-i", keyframe_path,
        "-f", "lavfi", "-t", str(duration), "-i", "anullsrc=r=44100:cl=stereo",
        "-vf", vf, "-t", str(duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-c:a", "aac", "-shortest", out_path,
    ], timeout=120 + duration * 20)
    return {"provider": "ffmpeg-animatic", "width": width, "height": height, "fps": 25, "duration_seconds": duration}


async def _veo_clip(shot: dict[str, Any], keyframe_path: str | None, out_path: str, aspect: str) -> dict[str, Any]:
    """Real Google Veo generation via the google-genai SDK (long-running op)."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ProviderBlocked("google-veo", "google-genai SDK is not installed") from exc

    client = genai.Client(api_key=GOOGLE_API_KEY)
    config = types.GenerateVideosConfig(aspect_ratio=aspect if aspect in ("16:9", "9:16") else "16:9")
    image = None
    if keyframe_path and Path(keyframe_path).exists():
        image = types.Image.from_file(location=keyframe_path)

    def _generate() -> Any:
        operation = client.models.generate_videos(model=VEO_MODEL, prompt=shot["video_prompt"], image=image, config=config)
        return operation

    try:
        operation = await asyncio.to_thread(_generate)
        while not operation.done:
            await asyncio.sleep(10)
            operation = await asyncio.to_thread(client.operations.get, operation)
        if operation.error:
            raise ProviderError("google-veo", str(operation.error))
        generated = operation.response.generated_videos[0]
        await asyncio.to_thread(client.files.download, file=generated.video)
        await asyncio.to_thread(generated.video.save, out_path)
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError("google-veo", f"Veo generation failed: {exc}") from exc
    return {"provider": "google-veo", "model": VEO_MODEL, "duration_seconds": shot.get("duration_seconds", 8)}


async def generate_clip(shot: dict[str, Any], keyframe_path: str | None, out_path: str, aspect: str) -> dict[str, Any]:
    """Generate one shot clip with the gated provider. Raises ProviderBlocked when gated off."""
    if GOOGLE_API_KEY:
        return await _veo_clip(shot, keyframe_path, out_path, aspect)
    if RENDER_FALLBACK_MODE == "animatic":
        if not keyframe_path or not Path(keyframe_path).exists():
            raise ProviderError("ffmpeg-animatic", "Keyframe image missing; cannot render animatic clip")
        duration = int(shot.get("duration_seconds", 8))
        return await asyncio.to_thread(_animatic_clip, keyframe_path, out_path, duration, aspect)
    raise ProviderBlocked("google-veo", "GOOGLE_API_KEY is missing; video generation blocked to protect credits")
