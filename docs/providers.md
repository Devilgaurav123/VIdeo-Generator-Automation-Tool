# Provider adapters

The production service should depend on interfaces, not vendor-specific request shapes:

- `LLMProvider`: structured story, character, style, and shot planning.
- `ImageProvider`: character reference sheets.
- `VideoProvider`: submit, poll, download, and validate clips.
- `TTSProvider`: segmented narration with measured durations.
- `MusicProvider` and `SFXProvider`: licensed audio assets and cue metadata.
- `StorageProvider`: local files or signed S3 objects.

The current `development-mock` path is **MOCKED** by design and is used only when `DEVELOPMENT_MODE=true`. It keeps the same output shapes so tests do not spend credits.

Google Flow does not have a public API contract that this app can safely invent. A Flow adapter must remain disabled until a permitted, authenticated UI automation implementation is supplied. Veo/Gemini API access is a separate adapter and must use explicit provider credentials from environment variables.