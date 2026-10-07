"""Audio providers: OpenAI TTS narration (Emergent key) + FFmpeg-synthesized
horror BGM and SFX. Every asset is a real audio file on disk, honestly labeled
by provider."""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from providers import ProviderBlocked, ProviderCreditsExhausted, ProviderError, is_credit_error

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
TTS_MODEL = os.environ.get("TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "onyx")
TTS_INSTRUCTIONS = (
    "You are a horror film narrator. Speak slowly, in a low, controlled, dread-filled tone. "
    "Pause slightly at commas and periods. Never sound cheerful or rushed."
)
_TTS_LIMIT = 4000


def _require_key() -> None:
    if not EMERGENT_LLM_KEY:
        raise ProviderBlocked("tts", "EMERGENT_LLM_KEY is not configured; narration is disabled")


def clean_for_tts(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
    text = re.sub(r"[*_#>~|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _chunk(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for sentence in sentences:
        if len(current) + len(sentence) + 1 > _TTS_LIMIT and current:
            chunks.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current.strip())
    return chunks or [text[:_TTS_LIMIT]]


def probe_duration(path: str) -> float:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return round(float(proc.stdout.strip()), 2)
    except ValueError:
        return 0.0


async def generate_narration(text: str, out_path: str, job_id: str) -> dict[str, Any]:
    _require_key()
    from emergentintegrations.llm.openai import OpenAITextToSpeech

    tts = OpenAITextToSpeech(api_key=EMERGENT_LLM_KEY)
    cleaned = clean_for_tts(text)
    chunks = _chunk(cleaned)
    part_paths: list[str] = []
    try:
        for index, chunk in enumerate(chunks):
            try:
                audio = await tts.generate_speech(text=chunk, model=TTS_MODEL, voice=TTS_VOICE, instructions=TTS_INSTRUCTIONS)
            except ValueError:
                # model/voice incompatibility — fall back once to tts-1 defaults
                audio = await tts.generate_speech(text=chunk, model="tts-1", voice=TTS_VOICE)
            except Exception as exc:
                if is_credit_error(exc):
                    raise ProviderCreditsExhausted("tts", "TTS provider budget exhausted — top up the key balance, then resume the job") from exc
                raise ProviderError("tts", f"TTS generation failed: {exc}") from exc
            part = f"{out_path}.part{index}.mp3"
            Path(part).write_bytes(audio)
            part_paths.append(part)
        if len(part_paths) == 1:
            Path(part_paths[0]).replace(out_path)
        else:
            list_file = f"{out_path}.concat.txt"
            Path(list_file).write_text("".join(f"file '{p}'\n" for p in part_paths))
            proc = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list_file, "-c:a", "libmp3lame", out_path],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise ProviderError("ffmpeg", proc.stderr.strip()[-300:])
    finally:
        for part in part_paths:
            Path(part).unlink(missing_ok=True)
        Path(f"{out_path}.concat.txt").unlink(missing_ok=True)
    return {"provider": "openai-tts", "model": TTS_MODEL, "voice": TTS_VOICE, "duration_seconds": probe_duration(out_path)}


def _synth(args: list[str], out_path: str, timeout: int = 180) -> None:
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args, out_path], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ProviderError("ffmpeg-synth", proc.stderr.strip()[-300:] or "audio synthesis failed")


async def generate_bgm(duration: float, out_path: str) -> dict[str, Any]:
    """Layered horror drone: sub sine + fifth + brown noise bed with slow tremolo."""
    dur = max(int(duration) + 4, 12)
    await asyncio.to_thread(_synth, [
        "-f", "lavfi", "-i", f"sine=frequency=49:duration={dur}",
        "-f", "lavfi", "-i", f"sine=frequency=73.4:duration={dur}",
        "-f", "lavfi", "-i", f"anoisesrc=color=brown:duration={dur}:amplitude=0.14",
        "-filter_complex",
        "[0:a]volume=0.5[a0];[1:a]volume=0.16[a1];[2:a]lowpass=f=300[a2];"
        f"[a0][a1][a2]amix=inputs=3:normalize=0,tremolo=f=0.16:d=0.55,afade=t=in:d=3,afade=t=out:st={dur - 4}:d=4,volume=0.8[aout]",
        "-map", "[aout]", "-ar", "44100", "-ac", "2",
    ], out_path)
    return {"provider": "ffmpeg-synth", "duration_seconds": probe_duration(out_path)}


async def generate_sfx(duration: float, out_dir: str) -> list[dict[str, Any]]:
    """Two horror cues: a sub-bass impact and a rising noise swell."""
    boom_path = str(Path(out_dir) / "sfx_impact.wav")
    swell_path = str(Path(out_dir) / "sfx_swell.wav")
    await asyncio.to_thread(_synth, [
        "-f", "lavfi", "-i", "sine=frequency=36:duration=1.8",
        "-af", "volume='if(lt(t,0.03),t/0.03,exp(-2.6*(t-0.03)))':eval=frame,lowpass=f=140,volume=1.4",
        "-ar", "44100", "-ac", "2",
    ], boom_path)
    await asyncio.to_thread(_synth, [
        "-f", "lavfi", "-i", "anoisesrc=color=pink:duration=3.2:amplitude=0.5",
        "-af", "volume='min(t/3.0,1)*exp(-1.2*max(t-3.0,0))':eval=frame,bandpass=f=800:w=600,volume=0.9",
        "-ar", "44100", "-ac", "2",
    ], swell_path)
    place_a = round(duration * 0.3, 2)
    place_b = round(duration * 0.72, 2)
    return [
        {"provider": "ffmpeg-synth", "path": boom_path, "at_seconds": place_a, "duration_seconds": probe_duration(boom_path), "label": "sub-bass impact"},
        {"provider": "ffmpeg-synth", "path": swell_path, "at_seconds": place_b, "duration_seconds": probe_duration(swell_path), "label": "rising swell"},
    ]
