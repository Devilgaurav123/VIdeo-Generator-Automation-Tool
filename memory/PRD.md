# SpectreAI — Product Requirements & Architecture Record

## Original Problem Statement
Build a complete, production-ready AI horror video automation platform across Expo (frontend) and FastAPI (backend). Strip all development mocks from the production path; implement real integrations for LLM (story, characters), images (character references), video (Google Veo/Flow architecture), TTS, BGM, SFX, FFmpeg rendering, and object storage — backed by PostgreSQL and Celery/Redis for background jobs. Strict credit protection, resumability, and true MP4 generation. No fake completions.

## User Personas
- **Operator (creator)**: prompts a horror premise, launches a production, monitors the pipeline, previews/downloads the final MP4.

## Architecture (as built)
- **Frontend**: Expo SDK 57 (expo-router), React Query, JWT auth (secure storage), dark horror command-center UI per design_guidelines.json.
- **API**: FastAPI (`backend/server.py`) — thin layer over PostgreSQL; dispatches Celery tasks; serves media with Bearer-or-`?token=` auth.
- **DB**: PostgreSQL 15 via SQLAlchemy 2 (`db/session.py`, `models/`: User, Project, Job, JobEvent, Character, Scene, Shot, MediaAsset).
- **Workers**: Celery + Redis (`workers/celery_app.py`, `workers/pipeline.py`) — 15-stage idempotent pipeline; every stage checks Postgres before spending credits; cancel flag honored between stages/shots.
- **Providers** (`providers/`):
  - `llm.py` — story, character bible, style bible, shot plan via Emergent LLM key (gpt-5.4, strict JSON parsing).
  - `image.py` — Gemini image gen (gemini-3.1-flash-image-preview): character reference sheets + per-shot keyframes with reference images attached for continuity.
  - `video.py` — Google Veo adapter (google-genai SDK, image-to-video from keyframes) gated by GOOGLE_API_KEY; honest `ffmpeg-animatic` fallback (Ken Burns zoompan over real AI keyframes) when no key; `ProviderBlocked` → job parks in BLOCKED_PROVIDER. Never fakes output.
  - `audio.py` — OpenAI TTS (gpt-4o-mini-tts, voice onyx, horror instructions, 4096-char chunking + ffmpeg concat); FFmpeg-synthesized horror BGM drone and SFX cues.
  - `storage.py` — Emergent Managed Object Storage (durable backup copy of final MP4; best-effort, non-fatal).
- **Assembly**: real FFmpeg concat → audio mix (narration + BGM + SFX with adelay) → mov_text subtitle mux → H.264/AAC MP4, verified by ffprobe quality gate.
- **Infra**: postgres, redis, celery-worker run under supervisor (`/etc/supervisor/conf.d/spectreai.conf`).

## Core Requirements (static)
- JWT auth; project CRUD; one active job per project.
- 15-stage pipeline: story → characters → bible → references → style → scenes → prompts → video → voice → music → sfx → subtitles → timeline → assembly → QC.
- Strict credit gate before video generation; BLOCKED_PROVIDER on missing keys.
- Resume skips completed stages (idempotency); cancel anytime.
- Download/stream endpoints return real bytes or 404 — never fake success.

## Implemented (2026-10-07)
- Full mock→production overhaul: Postgres schema, Celery/Redis workers, all provider adapters, FFmpeg assembly, object-storage upload.
- Verified end-to-end: 64s / 5.8MB MP4 (h264+aac+mov_text), 8 AI keyframes, 8 animatic clips, TTS narration, synth BGM/SFX, SRT subtitles, QC passed; uploaded to object storage.
- Frontend: production states (BLOCKED_PROVIDER card, provider-gated credit card), live shot progress, real MP4 preview (expo-video) + download, image format sniffing, polling backoff on auth errors.
- Frontend (2026-10-07): per-shot "Shot reel" strip on the job screen — horizontal keyframe thumbnails (expo-image via tokenized asset URLs) with per-shot status (pending / RENDERING / rendered check), filling in live as clips complete.
- Backend (2026-10-07): provider credit-exhaustion handling — budget/rate-limit errors from LLM/image/TTS/Veo park the job in WAITING_FOR_CREDITS (resumable, no re-spend) instead of FAILED.
- Infra (2026-10-07): container-rebuild resilience — `scripts/infra_bootstrap.sh` (idempotent apt install + Postgres provisioning) runs under supervisor; postgres/redis/celery wait on a ready marker; backend retries DB init at startup.
- Testing: iteration_2 — backend 16/16 pytest pass, all frontend flows pass. Testing agent fixed job-scoped scene/shot PKs (regression: second job previously failed with UniqueViolation).

## Backlog
- **P0**: User supplies GOOGLE_API_KEY → Veo motion clips replace animatic fallback (adapter already implemented).
- **P1**: Alembic migration scaffold (currently `create_all`); SRT/VTT burn-in option; per-clip preview thumbnails in job screen; regenerate single shot.
- **P2**: YouTube upload via OAuth; subtitle styling; multi-language voices (ElevenLabs for native accents).

## Test Credentials
See `/app/memory/test_credentials.md` (account lives in PostgreSQL).
