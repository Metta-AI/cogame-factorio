#!/usr/bin/env bash
# Build the static wasm replay viewer:
#
#   viewer/dist/index.html          (page: fetches + parses the replay,
#                                    UI chrome, drives the wasm)
#   viewer/dist/replay_pack.js      (JS parsing/packing module)
#   viewer/dist/viewer.{js,wasm}    (raylib map renderer, MODULARIZE,
#                                    createFactorioViewer)
#   build/viewer_core.{js,wasm}     (headless core, no raylib, ENVIRONMENT=
#                                    node — viewer/smoke.cjs + tests)
#
# Runs locally (emcc on PATH) and inside the Dockerfile's wasm-builder
# stage (emscripten/emsdk image, cwd = repo root). raylib is the prebuilt
# 5.5 web artifact, fetched once into build/raylib-web/ (sha256-verified,
# stamp-file cached).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v emcc >/dev/null 2>&1; then
    echo "error: emcc not found on PATH - install emscripten (brew install emscripten)" >&2
    exit 1
fi

# -- raylib 5.5 web (prebuilt, cached) ---------------------------------------
RAYLIB_DIR=build/raylib-web
RAYLIB_ZIP_URL="https://github.com/raysan5/raylib/releases/download/5.5/raylib-5.5_webassembly.zip"
RAYLIB_ZIP_SHA256="798b6bea650e78a60fe49f106a15d92ea4e33efd3aa1b3efa34b0438a14bbf2c"

# Cache guard includes the pin: a RAYLIB_ZIP_SHA256 bump invalidates a
# stale build/raylib-web/ (the stamp file records which zip it came from).
RAYLIB_STAMP="$RAYLIB_DIR/.zip-sha256"
mkdir -p build
if [ ! -f "$RAYLIB_DIR/lib/libraylib.a" ] || \
   [ "$(cat "$RAYLIB_STAMP" 2>/dev/null)" != "$RAYLIB_ZIP_SHA256" ]; then
    echo "Fetching raylib-5.5_webassembly ..."
    # trailing X's for BSD/GNU mktemp portability; unzip ignores the
    # missing .zip extension, and the trap reaps failed downloads
    tmpzip="$(mktemp "${TMPDIR:-/tmp}/raylib-web.zip.XXXXXX")"
    trap 'rm -f "$tmpzip"' EXIT
    curl -fsSL --retry 3 "$RAYLIB_ZIP_URL" -o "$tmpzip"
    echo "$RAYLIB_ZIP_SHA256  $tmpzip" | shasum -a 256 -c - >/dev/null
    rm -rf "$RAYLIB_DIR" build/raylib-5.5_webassembly
    (cd build && unzip -q "$tmpzip")
    mv build/raylib-5.5_webassembly "$RAYLIB_DIR"
    printf '%s\n' "$RAYLIB_ZIP_SHA256" > "$RAYLIB_STAMP"
    rm -f "$tmpzip"
    trap - EXIT
fi

# Exports shared by the browser bundle and the headless node build.
CORE_EXPORTS=_viewer_set_terrain,_viewer_set_step,_viewer_set_entity_palette,_viewer_set_resource_palette,_viewer_fit,_viewer_fit_terrain,_viewer_resize,_viewer_set_camera,_viewer_camera_x,_viewer_camera_y,_viewer_camera_scale,_viewer_pick,_viewer_hover_entity,_viewer_hover_x,_viewer_hover_y,_viewer_mouse_inside,_viewer_set_highlight,_viewer_stage_ptr,_viewer_stage_len,_viewer_frame_count,_malloc,_free
VIEWER_EXPORTS="_main,$CORE_EXPORTS"

mkdir -p viewer/dist
rm -f viewer/dist/*
emcc -O2 -DPLATFORM_WEB -DGRAPHICS_API_OPENGL_ES3 \
    -I "$RAYLIB_DIR/include" \
    viewer/viewer_main.c "$RAYLIB_DIR/lib/libraylib.a" \
    -sUSE_GLFW=3 -sUSE_WEBGL2=1 \
    -sALLOW_MEMORY_GROWTH=1 -sABORTING_MALLOC=1 \
    -sINITIAL_MEMORY=64MB -sSTACK_SIZE=1MB \
    -sENVIRONMENT=web \
    -sMODULARIZE=1 -sEXPORT_NAME=createFactorioViewer \
    -sEXPORTED_FUNCTIONS="$VIEWER_EXPORTS" \
    -sEXPORTED_RUNTIME_METHODS=ccall,cwrap,HEAPU8,HEAP32,HEAPF32,UTF8ToString \
    -o viewer/dist/viewer.js
cp viewer/index.html viewer/dist/index.html
cp viewer/replay_pack.js viewer/dist/replay_pack.js

# -- headless core (node smoke build: viewer/smoke.cjs) -----------------------
# Same viewer_main.c minus raylib/main loop (-DVIEWER_HEADLESS): proves the
# data/camera/pick API and memory behaviour under node without pixels.
emcc -O2 -DVIEWER_HEADLESS \
    viewer/viewer_main.c \
    --no-entry \
    -sALLOW_MEMORY_GROWTH=1 -sABORTING_MALLOC=1 \
    -sINITIAL_MEMORY=64MB -sSTACK_SIZE=1MB \
    -sENVIRONMENT=node \
    -sMODULARIZE=1 -sEXPORT_NAME=createFactorioViewerCore \
    -sEXPORTED_FUNCTIONS="$CORE_EXPORTS" \
    -sEXPORTED_RUNTIME_METHODS=ccall,cwrap,HEAPU8,HEAP32,HEAPF32,UTF8ToString \
    -o build/viewer_core.js

ls -la viewer/dist build/viewer_core.*
echo "build_viewer: OK"
