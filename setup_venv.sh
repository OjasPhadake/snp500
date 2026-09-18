#!/usr/bin/env bash
# Create an isolated virtual environment for the project and install everything into it.
# Usage: ./setup_venv.sh   then   source .venv/bin/activate
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt -e .
echo "Done. Activate with: source .venv/bin/activate"
