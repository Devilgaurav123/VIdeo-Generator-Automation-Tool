# Architecture

The mobile app talks to FastAPI through `EXPO_PUBLIC_BACKEND_URL`. Projects and production jobs use UUIDs and are persisted in MongoDB in the supplied workspace runtime. Mongo documents are always projected without `_id` before being returned.

`ProductionJob` is a resumable state machine. Its `stage`, `progress`, `events`, clips, audio layers, timeline, credit status, and quality report are written independently. The local runner uses asyncio to keep the workspace runnable; the same `run_pipeline` boundary can be moved to Celery tasks backed by Redis without changing the API contract.

The pipeline never claims provider success when a real provider is unavailable. Provider names are stored on each generated asset, and the credit gate stops video generation with `WAITING_FOR_FREE_CREDITS` when credits cannot be confirmed.

## Domain boundaries

- Project: creator prompt and production settings.
- Job: one resumable orchestration run.
- Story/characters/style/scenes/shots: structured planning artifacts.
- Clips/audio/subtitles/timeline: synchronized production assets.
- Quality report: the only gate that can move a job to `COMPLETED`.
