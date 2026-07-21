#!/usr/bin/env bash
# One-time historical backfill: pull every year in [start..end] for all four
# T-100 flavors via the form replay. Each request makes BTS generate a
# full-table extract, so run sequentially with a pause — this is a one-off,
# not something to parallelise. Re-running any year is safe (replace-years).
#
# Usage: PGHOST=... PGUSER=... PGPASSWORD=... PGDATABASE=... \
#        ingest/backfill.sh [start_year] [end_year]
set -u
cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-.venv/bin/python}
start=${1:-1990}
end=${2:-$(($(date +%Y) - 1))}

for year in $(seq "$start" "$end"); do
  for flavor in domestic_segment domestic_market international_segment international_market; do
    echo "=== $year $flavor $(date +%H:%M:%S)"
    "$PYTHON" ingest/load_t100.py "$flavor" --year "$year" || echo "FAIL $flavor $year"
    sleep 5
  done
done
echo "=== backfill done $(date +%H:%M:%S)"
