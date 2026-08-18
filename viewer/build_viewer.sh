#!/usr/bin/env bash
# Build the static wasm replay viewer into viewer/dist:
#
#   index.html                 board page (client/replay_broadcast.html)
#   factorio_replay.{js,wasm,data}   Nim -> emscripten renderer (replay-viewer/)
#                              + preloaded viewer/assets (Factorio sprite atlas)
#   static_replay.js, static_replay_worker.js   page <-> Worker glue (ctf)
#   broadcast_core.js          Bitworld sprite-protocol compositor (ctf, verbatim)
#   chrome_common.js           shared replay chrome (ctf port)
#   replay_doc.js              replay document parsing for the page
#
# Runs locally (nim + emcc on PATH, packages synced with
# `nimby --global sync nimby.lock`) and inside the Dockerfile's wasm-builder
# stage (cwd = repo root). Ends with the same test -f / negative-grep guard
# chain style as coworld-ctf's Dockerfile.replay-viewer so a half-built
# bundle never ships.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

NIM="${NIM:-nim}"
if ! command -v "$NIM" >/dev/null 2>&1; then
    if [ -x "$HOME/.nimby/nim/bin/nim" ]; then NIM="$HOME/.nimby/nim/bin/nim";
    else echo "error: nim not found on PATH (nimby use 2.2.4)" >&2; exit 1; fi
fi
if ! command -v emcc >/dev/null 2>&1; then
    echo "error: emcc not found on PATH - install emscripten (brew install emscripten)" >&2
    exit 1
fi
if [ ! -f viewer/assets/atlas.png ] || [ ! -f viewer/assets/atlas.json ]; then
    echo "error: viewer/assets/atlas.{png,json} missing (see viewer/tools/build_atlas.py)" >&2
    exit 1
fi

DIST=viewer/dist
mkdir -p "$DIST"
rm -f "$DIST"/*.js "$DIST"/*.wasm "$DIST"/*.data "$DIST"/*.html
rm -rf replay-viewer/dist

"$NIM" c --hints:off -d:emscripten replay-viewer/factorio_replay.nim
cp replay-viewer/dist/factorio_replay.js replay-viewer/dist/factorio_replay.wasm \
   replay-viewer/dist/factorio_replay.data "$DIST"/
rm -rf replay-viewer/dist/nimcache
cp client/broadcast_core.js "$DIST"/broadcast_core.js
cp client/chrome_common.js "$DIST"/chrome_common.js
cp client/replay_doc.js "$DIST"/replay_doc.js
cp client/static_replay.js "$DIST"/static_replay.js
cp client/static_replay_worker.js "$DIST"/static_replay_worker.js
cp client/replay_broadcast.html "$DIST"/index.html

# Guard chain (ctf style): every file the page loads, the wiring between them,
# and the things that must NOT be there.
test -f "$DIST"/factorio_replay.wasm
test -f "$DIST"/factorio_replay.js
test -f "$DIST"/factorio_replay.data
test -f "$DIST"/static_replay_worker.js
test -f "$DIST"/index.html
test -s "$DIST"/chrome_common.js
grep -q 'window.ChromeCommon' "$DIST"/chrome_common.js
grep -q 'chrome_common.js' "$DIST"/index.html
test -s "$DIST"/broadcast_core.js
grep -q 'window.BroadcastCore' "$DIST"/broadcast_core.js
grep -q 'window.ReplayDoc\|root.ReplayDoc' "$DIST"/replay_doc.js
grep -q 'replay_doc.js' "$DIST"/index.html
grep -q 'static_replay.js' "$DIST"/index.html
grep -q 'static_replay_worker.js' "$DIST"/static_replay.js
grep -q "importScripts('./broadcast_core.js', './factorio_replay.js')" "$DIST"/static_replay_worker.js
grep -q '_factorio_load_replay' "$DIST"/factorio_replay.js
grep -q '_factorio_stage_ptr' "$DIST"/factorio_replay.js
# The page must fetch the replay itself (?replay= / /replay-data) and never
# load the runtime on the main thread.
grep -q 'params.get("replay")' "$DIST"/index.html
! grep -q '<script src="./broadcast_core.js"></script>' "$DIST"/index.html
! grep -q '<script src="./factorio_replay.js"></script>' "$DIST"/index.html
! grep -q 'viewer.js' "$DIST"/index.html
! grep -q 'replay_pack.js' "$DIST"/index.html
# Relative asset paths only (the bundle is served under /client/replay/ and
# from the Observatory's own host).
! grep -Eq 'src="/[^/]' "$DIST"/index.html

ls -la "$DIST"
echo "build_viewer: OK"
