#!/usr/bin/env bash
# ABG Guard - one shot bootstrap: directories + database schema.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "!! .env not found - copy .env.example first" >&2
  exit 1
fi

# .env supplies the defaults; anything already exported wins, so the same
# script works for the docker-style (db/redis) and the localhost setup.
while IFS= read -r line; do
  [[ "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
  key="${line%%=*}"
  value="${line#*=}"
  value="${value%"${value##*[![:space:]]}"}"   # strip trailing whitespace
  [[ -v "$key" ]] && continue
  export "$key=$value"
done < .env

mkdir -p data dataset

# prefer the interpreter that can actually import the project deps
PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python >/dev/null 2>&1; then PYTHON_BIN=python; else PYTHON_BIN=python3; fi
fi
echo "using $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

"$PYTHON_BIN" - <<'PY'
import asyncio
from abgguard.core.db import close_db, init_db

async def main():
    await init_db()
    await close_db()
    print("database schema ready")

asyncio.run(main())
PY

echo "bootstrap complete -> run:  python run.py"
