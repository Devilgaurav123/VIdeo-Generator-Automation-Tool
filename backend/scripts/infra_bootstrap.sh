#!/bin/bash
# SpectreAI infrastructure bootstrap — idempotent, safe to run on every container start.
# System packages live on the ephemeral container layer, so ffmpeg/PostgreSQL/Redis
# must be reinstalled whenever the container is rebuilt. Python deps persist in /root/.venv.
export DEBIAN_FRONTEND=noninteractive
MARKER=/tmp/.spectreai_infra_ready
rm -f "$MARKER"

if ! command -v psql >/dev/null 2>&1 || ! command -v redis-server >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[infra] installing postgresql, redis-server, ffmpeg..."
  apt-get update -qq
  apt-get install -y -qq postgresql redis-server ffmpeg
fi

# Hand services to supervisor: stop any service-managed instances first.
service redis-server stop >/dev/null 2>&1 || true

# Provision role + database (idempotent).
service postgresql start
su postgres -s /bin/bash -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='spectre'\" | grep -q 1 || psql -c \"CREATE USER spectre WITH PASSWORD 'spectre'\""
su postgres -s /bin/bash -c "psql -lqt | cut -d'|' -f1 | grep -qw spectreai || createdb -O spectre spectreai"
service postgresql stop
rm -f /var/lib/postgresql/15/main/postmaster.pid

mkdir -p /app/backend/media/jobs
touch "$MARKER"
echo "[infra] ready"
