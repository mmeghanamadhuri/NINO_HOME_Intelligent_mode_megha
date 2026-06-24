#!/usr/bin/env bash
# Create NiNO memory PostgreSQL database and apply schema.
set -euo pipefail

DB_NAME="${NINO_DB_NAME:-nino_memory}"
DB_USER="${NINO_DB_USER:-nino}"
DB_PASS="${NINO_DB_PASS:-nino}"
DB_HOST="${NINO_DB_HOST:-127.0.0.1}"
DB_PORT="${NINO_DB_PORT:-5432}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="${SCRIPT_DIR}/memory_schema.sql"

if ! command -v psql >/dev/null 2>&1; then
  echo "Install PostgreSQL client: sudo apt install postgresql postgresql-contrib"
  exit 1
fi

echo ">>> Creating role/database (may require sudo postgres user)..."
if command -v sudo >/dev/null 2>&1 && id postgres >/dev/null 2>&1; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
SELECT 'CREATE DATABASE ${DB_NAME} OWNER ${DB_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB_NAME}')\\gexec
GRANT ALL PRIVILEGES ON DATABASE ${DB_NAME} TO ${DB_USER};
SQL
else
  echo "Run as postgres superuser or set DATABASE_URL to an existing database."
fi

export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
echo ">>> Applying schema to ${DATABASE_URL}"
psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -f "${SCHEMA}"

echo ">>> Done. Add to your environment:"
echo "export DATABASE_URL=\"${DATABASE_URL}\""
echo "export MEMORY_EXTRACTION=1   # Phase B — on by default when DATABASE_URL is set"
