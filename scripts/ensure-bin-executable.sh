#!/bin/bash
# Ensure all lsstack bin scripts are executable. Run after clone or copy (e.g. from repo root: ./lsstack/scripts/ensure-bin-executable.sh).

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$(dirname "$SCRIPT_DIR")/bin"
for f in "$BIN_DIR"/*; do
  [ -f "$f" ] && chmod +x "$f"
done
echo "Ensured execute bit on $BIN_DIR/*"
