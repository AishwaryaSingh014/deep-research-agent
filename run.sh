#!/usr/bin/env bash
# Start the FastAPI backend and the Streamlit frontend together.
#
#   ./run.sh          both services
#   ./run.sh api      backend only  (http://localhost:8000/docs)
#   ./run.sh ui       frontend only (http://localhost:8501)
#
# Ctrl-C stops everything.

set -euo pipefail
cd "$(dirname "$0")"

# src/ layout: the packages are not importable unless src is on the path. A pyproject.toml
# plus `pip install -e .` would be the usual answer; this keeps the repo dependency-free.
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

PY=".venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "No virtualenv found. Create one first:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "warning: no .env found — copy .env.example and add at least one LLM key" >&2
fi

MODE="${1:-both}"
# Override if something already owns these ports: API_PORT=8010 ./run.sh
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-8501}"
# Loopback by default. The API has no authentication and will happily spend your API quota
# for anyone who can reach it, so binding to all interfaces must be a deliberate choice:
#   BIND=0.0.0.0 ./run.sh
BIND="${BIND:-127.0.0.1}"
export RESEARCH_API="http://localhost:${API_PORT}"

start_api() {
  echo "API  -> http://localhost:${API_PORT}/docs"
  "$PY" -m uvicorn backend.main:app --host "$BIND" --port "$API_PORT" "$@"
}

start_ui() {
  echo "UI   -> http://localhost:${UI_PORT}"
  "$PY" -m streamlit run src/frontend/main.py \
    --server.port "$UI_PORT" --server.address "$BIND" \
    --server.headless true --browser.gatherUsageStats false
}

case "$MODE" in
  api) start_api --reload ;;
  ui)  start_ui ;;
  both)
    start_api &
    API_PID=$!
    # Stop the backend too when the foreground UI exits or Ctrl-C arrives.
    trap 'kill $API_PID 2>/dev/null || true' EXIT INT TERM

    # Wait for the backend to answer before launching the UI, so the sidebar does not
    # open showing a connection error on a perfectly healthy startup.
    for _ in $(seq 1 40); do
      if "$PY" -c "import httpx,os,sys; sys.exit(0 if httpx.get(os.environ['RESEARCH_API']+'/health',timeout=1).status_code==200 else 1)" 2>/dev/null; then
        break
      fi
      sleep 0.5
    done

    start_ui
    ;;
  *)
    echo "usage: ./run.sh [api|ui|both]" >&2
    exit 1
    ;;
esac
