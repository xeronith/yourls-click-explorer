#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but was not found in PATH." >&2
    exit 1
fi

# Create the venv if it does not exist yet.
if [[ ! -x "$PYTHON" ]]; then
    echo "Creating virtual environment in $VENV_DIR ..." >&2
    python3 -m venv "$VENV_DIR"
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet rich
fi

# Activate it if it is not already the active environment.
if [[ "${VIRTUAL_ENV:-}" != "$VENV_DIR" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
fi

# A venv created before this script existed may not have Rich.
if ! python -c "import rich" >/dev/null 2>&1; then
    echo "Installing Rich ..." >&2
    python -m pip install --quiet rich
fi

cd "$SCRIPT_DIR"
exec python "$SCRIPT_DIR/yourls_cli.py" "$@"
