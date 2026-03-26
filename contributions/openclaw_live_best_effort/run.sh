#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$DIR/build_live_atlas.py" "$DIR/atlas_from_phosphosignor.json"
