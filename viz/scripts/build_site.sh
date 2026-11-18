#!/usr/bin/env bash
# Build the static site into viz/dist. Nothing is deployed.
#
#   npm run build:site                      relative base, works on Cloudflare Pages or any subpath
#   BASE=/glass-chip/ npm run build:site    absolute base for GitHub Pages at user.github.io/glass-chip/
#
# The replays, scene and surrogate in viz/public are gitignored, so they have to be present locally
# (studies/make_replays.py and surrogate/export_onnx.py write them) before this runs.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE="${BASE:-./}"
case "$BASE" in */) ;; *) BASE="$BASE/" ;; esac

[ -f public/replays/index.json ] || { echo "public/replays/index.json is missing, generate replays first" >&2; exit 1; }

node scripts/sync-configs.mjs
npx tsc --noEmit
npx vite build --base "$BASE"
node scripts/stage_site.mjs
node scripts/smoke.mjs --dist --site
echo "site ready in viz/dist (base $BASE)"
