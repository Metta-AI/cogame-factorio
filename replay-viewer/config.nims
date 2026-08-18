import std/[os, strformat, strutils]

let rootDir = currentSourcePath().parentDir().parentDir()
let distDir = rootDir / "replay-viewer" / "dist"

if not dirExists(distDir):
  mkDir(distDir)

switch("nimcache", distDir / "nimcache")
switch("threads", "off")
--define:release
when defined(emscripten):
  --os:linux
  --cpu:wasm32
  --cc:clang
  --clang.exe:emcc
  --clang.linkerexe:emcc
  --clang.cpp.exe:emcc
  --clang.cpp.linkerexe:emcc
  --mm:arc
  --exceptions:goto
  --define:noSignalHandler
  --define:useMalloc
  switch(
    "passL",
    (&"""
    -o {distDir / "factorio_replay.js"}
    --preload-file {rootDir / "viewer" / "assets"}@assets
    -O2
    -s ALLOW_MEMORY_GROWTH
    -s ABORTING_MALLOC=1
    -s FILESYSTEM=1
    -s ENVIRONMENT=web,worker,node
    -s EXPORTED_RUNTIME_METHODS=HEAPU8,FS
    -s EXPORTED_FUNCTIONS=_main,_malloc,_free,_factorio_set_atlas,_factorio_load_replay,_factorio_frame,_factorio_input,_factorio_packet_ptr,_factorio_packet_len,_factorio_error_ptr,_factorio_error_len,_factorio_stage_ptr,_factorio_stage_len,_factorio_profile_ptr,_factorio_profile_len
    """).replace("\n", " ")
  )
