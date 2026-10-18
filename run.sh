#!/bin/sh
# Starts the Polar H10 recorder. Creates the environment on first run.
# Run this from Terminal: macOS grants Bluetooth access to the launching app.
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
  uv venv --python 3.12 .venv && uv sync
fi
[ "${POLAR_FAKE:-}" = "1" ] && set -- --fake "$@"
exec uv run python -m polar_recorder "$@"
