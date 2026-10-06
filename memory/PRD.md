# SpectreAI Product Requirements

## Problem statement

Build a mobile-first AI horror video automation platform where a creator enters one simple horror premise and the system orchestrates story generation, character continuity, style, short video shots, narration, music, SFX, subtitles, timeline synchronization, assembly, and quality checks.

## Personas

- **Independent horror creator:** wants a repeatable production pipeline without writing every shot prompt.
- **Short-form producer:** needs progress visibility, credit safety, and export-ready production metadata.
- **Platform operator:** needs resumable jobs, provider boundaries, logs, QC, and clear provider limitations.

## Architecture

- Expo SDK 57 mobile command center.
- FastAPI backend on port 8001 with JWT auth and MongoDB persistence in the supplied workspace.
- Persisted job state machine with stage events, credit gate, cancellation, resume, clip/audio/timeline/QC metadata, and WebSocket progress.
- Provider-neutral development adapters. **MOCKED** local providers run under `DEVELOPMENT_MODE=true`.
- Asyncio runner keeps the workspace executable; production deployment can move the same pipeline boundary to Celery/Redis.

## Core requirements (static)

1. One prompt creates a production project and returns a queued job immediately.
2. All production stages are persisted and observable.
3. Characters and visual style remain continuity-locked.
4. Short clips are planned and tracked individually.
5. Video generation is credit-gated and never purchases, upgrades, rotates accounts, or bypasses quotas.
6. Audio, subtitles, and timeline metadata are synchronized.
7. QC gates completion and reports warnings/errors.
8. Provider integrations are adapters; unsupported Flow APIs are not fabricated.
9. Mobile UI supports auth, create, progress, cancellation/resume, archive, and result review.
10. Secrets stay in environment variables and test credentials are documented separately.

## Implemented — 2026-10-06

- Added FastAPI JWT registration/login/me endpoints and protected project/job APIs.
- Added persisted mock-first pipeline with story, characters, references, style bible, scenes, shots, clips, audio, subtitles, timeline, QC, events, credit status, cancellation, resume, and WebSocket progress.
- Added Expo dark-first SpectreAI command center with sign-in, credit protection card, create prompt form, stage stepper, shot counters, result card, archive, and responsive safe-area layout.
- Added API/provider documentation, compose setup, Docker worker slot, environment example, and acceptance test support.
- Verified the public API acceptance path and phone-sized Expo flow; added stable development credentials.

## Prioritized backlog

### P0 — required for production video delivery

- Implement real StorageProvider with local file validation and S3 signed URLs.
- Add a real FFmpeg renderer worker that outputs and serves a playable MP4.
- Move long-running execution to Celery/Redis and add durable retry/idempotency records.
- Add real provider credentials through environment configuration and implement only official APIs.

### P1 — production hardening

- Replace workspace Mongo persistence with the requested PostgreSQL/SQLAlchemy/Alembic deployment profile.
- Add provider operation, usage/cost, job event, and audit collections with indexes.
- Add automated media corruption, blank-frame, duplicate-clip, FPS, and sync QC checks.
- Add secure signed downloads, project package export, and subtitle file downloads.

### P2 — creator expansion

- Add editable character/style/scene review before generation.
- Add provider-approved Flow UI adapter and optional Veo adapter behind feature flags.
- Add optional YouTube OAuth upload only after explicit user enablement.
- Add richer video preview and subtitle/SFX review controls.

## Next tasks

1. Connect a licensed video provider adapter and implement the FFmpeg renderer.
2. Add a real storage backend and binary MP4 download flow.
3. Introduce Celery/Redis task transport and PostgreSQL migration for production deployment.