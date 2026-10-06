# SpectreAI

SpectreAI is a mobile-first AI horror production command center. A creator enters one premise and receives a persisted, observable production job: story, locked characters, style bible, shot plan, video clips, audio layers, subtitles, timeline, and quality report.

## Current build

- Expo SDK 57 mobile UI with JWT sign-in, project creation, project archive, live job polling, cancellation/resume controls, credit-gate messaging, and quality-gated result review.
- FastAPI + MongoDB API in the supplied runtime. Every job stage and event is persisted, and the API exposes both polling and `/ws/jobs/{job_id}` progress.
- Provider-neutral pipeline boundaries. `DEVELOPMENT_MODE=true` runs **MOCKED** local story, character, video, audio, subtitle, timeline, and assembly adapters; no provider credits are spent.
- Google Flow is deliberately not treated as an API. See `docs/flow-integration.md` for the supported integration boundary.

## Run locally

```bash
cd /app
sudo supervisorctl restart backend
sudo supervisorctl restart expo
```

The API listens on `0.0.0.0:8001` and the Expo preview uses the configured workspace URL. The backend reads `MONGO_URL` and `DB_NAME` from `backend/.env`.

## Development acceptance flow

Use the development credentials in `/app/memory/test_credentials.md`, create a project, and submit a one-minute prompt. The local pipeline produces eight-second mock clip metadata, narration/music/SFX layers, SRT/VTT cue metadata, a synchronized timeline, and a passing QC report.

```bash
curl http://127.0.0.1:8001/api/health
```

## API overview

See `docs/api.md`. Long-running production requests return immediately with a `job_id`; inspect `/api/v1/jobs/{job_id}` or subscribe to `/ws/jobs/{job_id}`.

## Provider and production notes

See `docs/providers.md` and `docs/architecture.md`. Real video, image, TTS, music, SFX, storage, Celery/Redis, FFmpeg rendering, PostgreSQL migration, and YouTube upload credentials are intentionally not fabricated in development mode. The final binary MP4 download endpoint returns `409 ASSET_NOT_READY` until a real renderer/storage provider is configured.