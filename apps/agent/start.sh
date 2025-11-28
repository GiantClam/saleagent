#!/usr/bin/env sh
set -eu
PORT="${PORT:-8000}"
uvicorn main:app --host 0.0.0.0 --port "$PORT" --workers 1 --timeout-keep-alive 180
