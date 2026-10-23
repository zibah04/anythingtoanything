#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --limit-concurrency "${HTTP_CONCURRENCY:-20}" --timeout-keep-alive 5
