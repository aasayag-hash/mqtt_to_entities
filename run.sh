#!/usr/bin/env bash
set -e

exec python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 8099 --app-dir /app
