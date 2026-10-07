"""Image provider: character reference sheets and per-shot keyframes.

Uses Gemini image generation (Nano Banana class models) through the Emergent
universal key. Character reference sheets are fed back as visual context when
rendering shot keyframes so the cast stays on-model.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from providers import ProviderBlocked, ProviderError

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
IMAGE_MODEL = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image-preview")


def _require_key() -> None:
    if not EMERGENT_LLM_KEY:
        raise ProviderBlocked("image", "EMERGENT_LLM_KEY is not configured; image generation is disabled")


async def _generate(prompt: str, session_id: str, reference_images: list[bytes] | None = None) -> bytes:
    _require_key()
    from emergentintegrations.llm.chat import ImageContent, LlmChat, UserMessage

    chat = LlmChat(api_key=EMERGENT_LLM_KEY, session_id=session_id, system_message="You generate production concept art exactly as briefed.")
    chat.with_model("gemini", IMAGE_MODEL).with_params(modalities=["image", "text"])
    files = [ImageContent(base64.b64encode(blob).decode()) for blob in (reference_images or [])]
    message = UserMessage(text=prompt, file_contents=files or None)
    try:
        _text, images = await chat.send_message_multimodal_response(message)
    except Exception as exc:
        raise ProviderError("image", f"Image generation failed: {exc}") from exc
    if not images:
        raise ProviderError("image", "Image provider returned no image")
    return base64.b64decode(images[0]["data"])


def _character_brief(character: dict[str, Any]) -> str:
    data = character.get("data", character)
    parts = [
        f"Name: {character['name']}", f"Role: {character.get('role', data.get('role', ''))}",
        f"Age: {data.get('age', 'unknown')}", f"Face: {data.get('face', '')}",
        f"Hair: {data.get('hair', '')}", f"Eyes: {data.get('eyes', '')}",
        f"Skin: {data.get('skin_tone', '')}", f"Build: {data.get('body_type', '')}",
        f"Clothing: {data.get('clothing', '')}",
    ]
    return "; ".join(p for p in parts if not p.endswith(": "))


async def generate_reference_sheet(character: dict[str, Any], visual_style: str, session_id: str) -> bytes:
    prompt = (
        f"Full-body character reference sheet for a horror film, style: {visual_style}. "
        f"{_character_brief(character)}. Neutral standing pose, plain dark background, "
        f"front view, evenly lit so the design reads clearly. Continuity rule: "
        f"{character.get('continuity_rules', 'keep the design consistent')}."
    )
    return await _generate(prompt, f"{session_id}-ref-{character['name'][:16]}")


async def generate_keyframe(
    shot: dict[str, Any],
    characters: list[dict[str, Any]],
    style: dict[str, Any],
    reference_images: list[bytes],
    session_id: str,
) -> bytes:
    data = shot.get("data", shot)
    cast = [c for c in characters if c["name"] in (data.get("characters") or [])]
    cast_brief = " ".join(_character_brief(c) + "." for c in cast)
    prompt = (
        f"Cinematic horror film still, {style.get('animation_style', '2D horror animation')}. "
        f"Scene action: {data.get('action', '')}. Environment: {data.get('environment', '')}. "
        f"Camera: {data.get('camera', '')}. Lighting: {data.get('lighting', '')}. "
        f"Mood: {data.get('mood', 'dread')}. Palette: {', '.join(style.get('palette', []))}. "
        f"{cast_brief} If reference images are attached, keep those exact character designs. "
        f"Aspect ratio {style.get('aspect_ratio', '16:9')}. No text, no watermark."
    )
    return await _generate(prompt, f"{session_id}-key-{shot['id']}", reference_images or None)
