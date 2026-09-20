#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python -m uvicorn stock_selector.api:app --host 0.0.0.0 --port 8002