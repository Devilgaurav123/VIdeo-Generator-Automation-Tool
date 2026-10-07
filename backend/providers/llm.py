"""LLM provider: story generation, character bible, style bible, shot planning.

Uses the Emergent universal key (EMERGENT_LLM_KEY) via emergentintegrations.
Backend batch calls are explicit non-streaming requests, so send_message() is
used deliberately. Every call demands strict JSON and is validated.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from providers import ProviderBlocked, ProviderCreditsExhausted, ProviderError, is_credit_error

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

EMERGENT_LLM_KEY = os.environ.get("EMERGENT_LLM_KEY", "")
LLM_PROVIDER = "openai"
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.4")

_SYSTEM = (
    "You are the narrative engine of SpectreAI, a professional horror short-film "
    "automation studio. You write tight, visual, production-ready horror material. "
    "You ALWAYS respond with a single valid JSON value and nothing else: no prose, "
    "no markdown fences, no commentary."
)


def _require_key() -> None:
    if not EMERGENT_LLM_KEY:
        raise ProviderBlocked("llm", "EMERGENT_LLM_KEY is not configured; story engine is disabled")


def _parse_json(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(cleaned):
            if char in "{[":
                try:
                    obj, _ = decoder.raw_decode(cleaned[index:])
                    return obj
                except json.JSONDecodeError:
                    continue
        raise ProviderError("llm", "LLM returned malformed JSON")


async def _ask(prompt: str, session_id: str) -> Any:
    _require_key()
    from emergentintegrations.llm.chat import LlmChat, UserMessage

    chat = LlmChat(api_key=EMERGENT_LLM_KEY, session_id=session_id, system_message=_SYSTEM)
    chat.with_model(LLM_PROVIDER, LLM_MODEL)
    try:
        response = await chat.send_message(UserMessage(text=prompt))
    except Exception as exc:  # provider/network failure — surface honestly
        if is_credit_error(exc):
            raise ProviderCreditsExhausted("llm", "LLM provider budget exhausted — top up the key balance, then resume the job") from exc
        raise ProviderError("llm", f"LLM request failed: {exc}") from exc
    text = response if isinstance(response, str) else str(response)
    return _parse_json(text)


async def generate_story(project: dict[str, Any], session_id: str) -> dict[str, Any]:
    duration = int(project["target_duration_minutes"]) * 60
    prompt = (
        "Create an original horror short story as JSON with exactly these keys: "
        "title (string), genre (string), premise (string), setting (string), "
        "narration (string, a voiceover script of roughly "
        f"{max(90, duration * 2)} words written for a slow dread-filled narrator), "
        f"target_duration_seconds (number, {duration}). "
        f"Story premise from the director: {project['prompt']!r}. "
        f"Language: {project['language']}. Keep it cinematic and visual."
    )
    story = await _ask(prompt, f"{session_id}-story")
    if not isinstance(story, dict) or not story.get("title") or not story.get("narration"):
        raise ProviderError("llm", "Story payload missing title/narration")
    story["target_duration_seconds"] = duration
    return story


async def build_character_bible(story: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    prompt = (
        "From this horror story, extract every recurring character (including unseen "
        "entities) as a JSON array. Each element must have exactly these keys: "
        "name, role, age, face, hair, eyes, skin_tone, body_type, clothing, "
        "personality, continuity_rules (one sentence that locks the design). "
        "Maximum 4 characters.\n"
        f"STORY TITLE: {story.get('title')}\nPREMISE: {story.get('premise')}\n"
        f"NARRATION: {story.get('narration')}"
    )
    characters = await _ask(prompt, f"{session_id}-characters")
    if not isinstance(characters, list):
        raise ProviderError("llm", "Character bible is not a JSON array")
    return [c for c in characters if isinstance(c, dict) and c.get("name")][:4]


async def build_style_bible(project: dict[str, Any], story: dict[str, Any], session_id: str) -> dict[str, Any]:
    prompt = (
        "Define the visual style bible for this horror film as JSON with exactly these keys: "
        "animation_style, rendering, line_style, palette (array of 3-5 color names), "
        "lighting, camera, atmosphere, aspect_ratio, continuity (one sentence).\n"
        f"Director requested style: {project['visual_style']!r}, aspect {project['aspect_ratio']}.\n"
        f"STORY: {story.get('title')} — {story.get('setting')}"
    )
    style = await _ask(prompt, f"{session_id}-style")
    if not isinstance(style, dict):
        raise ProviderError("llm", "Style bible is not a JSON object")
    style.setdefault("animation_style", project["visual_style"])
    style.setdefault("aspect_ratio", project["aspect_ratio"])
    return style


async def plan_shots(
    story: dict[str, Any],
    characters: list[dict[str, Any]],
    style: dict[str, Any],
    total_shots: int,
    session_id: str,
) -> dict[str, Any]:
    names = ", ".join(c["name"] for c in characters) or "no named characters"
    prompt = (
        f"Break this horror story into exactly {total_shots} cinematic shots of 8 seconds each. "
        "Respond with JSON: {\"scenes\": [{\"number\": int, \"title\": str, \"mood\": str}], "
        "\"shots\": [{\"shot_number\": int, \"scene_number\": int, \"narration_segment\": str, "
        "\"characters\": [str], \"environment\": str, \"action\": str, \"camera\": str, "
        "\"lighting\": str, \"mood\": str, \"transition\": str}]}. "
        f"Character names available: {names}. Visual style: {style.get('animation_style')}. "
        "Every shot must escalate dread and reference recurring locations for continuity.\n"
        f"STORY: {story.get('title')}\nSETTING: {story.get('setting')}\nNARRATION: {story.get('narration')}"
    )
    plan = await _ask(prompt, f"{session_id}-shots")
    if not isinstance(plan, dict) or not isinstance(plan.get("shots"), list) or not plan["shots"]:
        raise ProviderError("llm", "Shot plan missing shots array")
    plan["shots"] = plan["shots"][:total_shots]
    if not isinstance(plan.get("scenes"), list):
        plan["scenes"] = []
    return plan
