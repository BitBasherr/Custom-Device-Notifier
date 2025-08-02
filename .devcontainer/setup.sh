#!/usr/bin/env bash
set -e

echo "Updating pip & installing dev dependencies…"
python -m pip install --upgrade "pip<23.2"

# All lint/type/test tools + current HA core & stubs
pip install -r .devcontainer/requirements-dev.txt

echo "Creating an isolated HA config directory…"
mkdir -p .devcontainer/ha
echo "Done!"
