#!/usr/bin/env bash
# Hype Radar: motor + dashboard → http://localhost:8090
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/uvicorn engine.main:app --host 127.0.0.1 --port "${PORT:-8090}"
