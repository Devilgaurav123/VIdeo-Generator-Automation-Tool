# API

All protected endpoints use `Authorization: Bearer <JWT>`.

## Auth

- `POST /api/v1/auth/register` `{email,password}`
- `POST /api/v1/auth/login` `{email,password}`
- `GET /api/v1/auth/me`

## Projects and jobs

- `POST /api/v1/projects`
- `GET /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `POST /api/v1/projects/{project_id}/generate` → `{job_id,status}` immediately
- `GET /api/v1/jobs/{job_id}`
- `GET /api/v1/jobs/{job_id}/progress`
- `POST /api/v1/jobs/{job_id}/cancel`
- `POST /api/v1/jobs/{job_id}/resume`
- `GET /api/v1/jobs/{job_id}/clips`
- `GET /api/v1/jobs/{job_id}/audio`
- `GET /api/v1/jobs/{job_id}/timeline`
- `GET /api/v1/jobs/{job_id}/quality-report`
- `GET /api/v1/jobs/{job_id}/credit-status`
- `GET /api/v1/jobs/{job_id}/download`

WebSocket progress is available at `/ws/jobs/{job_id}`. It sends stage, status, percentage, shot counters, and the latest message, then closes on a terminal state.