#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD="python"
else
  echo "Python was not found. Install Python 3.10 or newer, then try again."
  exit 1
fi

if [ ! -f "$SCRIPT_DIR/citeverify_web.py" ]; then
  echo "CiteVerify could not be found in this folder."
  exit 1
fi

KEY_ARGS=()
if [ -f "$SCRIPT_DIR/openalex.txt" ]; then
  KEY_ARGS+=(--openalex-key-file "$SCRIPT_DIR/openalex.txt")
fi
if [ -f "$SCRIPT_DIR/S2.txt" ]; then
  KEY_ARGS+=(--s2-api-key-file "$SCRIPT_DIR/S2.txt")
fi

if [ "${#KEY_ARGS[@]}" -eq 0 ]; then
  echo "No API-key files were found beside this script."
  echo "CiteVerify will run without OpenAlex and Semantic Scholar cross-checks."
fi

echo "Starting CiteVerify..."
echo "Leave this terminal window open while using the browser page."
"$PYTHON_CMD" "$SCRIPT_DIR/citeverify_web.py" "${KEY_ARGS[@]}"
