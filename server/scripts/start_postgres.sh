#!/usr/bin/env bash
# Install (if needed), start PostgreSQL, and init NiNO memory DB.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if command -v pg_isready >/dev/null 2>&1; then
  if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    echo ">>> PostgreSQL already running — skipping install/start"
    bash "${SCRIPT_DIR}/init_memory_db.sh" || true
    echo ">>> PostgreSQL ready at postgresql://nino:nino@127.0.0.1:5432/nino_memory"
    exit 0
  fi
fi

if ! command -v psql >/dev/null 2>&1 || ! id postgres >/dev/null 2>&1; then
  echo ">>> Installing PostgreSQL..."
  sudo apt-get update -qq
  sudo apt-get install -y postgresql postgresql-contrib
fi

echo ">>> Starting PostgreSQL..."
sudo systemctl enable postgresql
sudo systemctl start postgresql

echo ">>> Initializing NiNO memory database..."
bash "${SCRIPT_DIR}/init_memory_db.sh"

echo ">>> PostgreSQL ready at postgresql://nino:nino@127.0.0.1:5432/nino_memory"
