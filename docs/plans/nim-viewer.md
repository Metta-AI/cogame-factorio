# Nim sprite replay viewer (replaces the C/raylib viewer)

Status: implemented on the `viewer/`, `replay-viewer/`, `client/` tree; this
note records the design decisions so they are not re-litigated.

## Decision: (A) genuine bitworld broadcast packets, ctf's client verbatim

The wasm module (`replay-viewer/factorio_replay.nim`) emits real Bitworld
sprite-protocol v1 packets (`bitworld/spriteprotocol`: `0x06` layer, `0x05`
viewport, `0x01` sprite def with snappy RGBA pixels + label, `0x02` object,
`0x03` delete) and the page renders them with coworld-ctf's
`client/broadcast_core.js` copied unchanged (only the `ctf_*` export names in
the worker/glue are renamed `factorio_*`). Reasons:

- Everything the board needs exists in the protocol: a zoomable map layer
  with i16 coordinates (our largest board is ~4k px), u16 sprite ids
  (~2k needed), u16 object ids (a lab base is a few hundred objects), and
  the reserved chrome sprite id 4090 whose label carries JSON to `onText`.
- ctf's core already has the expensive parts right: static-band caching
  (object ids 40..99 at z=-32768 baked once into a base canvas), per-sprite
  canvas surfaces, wheel zoom 1..12x over fit, drag pan, minimap, DPR, the
  Worker + OffscreenCanvas delivery, sprite decode retry, transform echo.
  Option (B) would re-implement all of that for a smaller wire format with
  no benefit — the packet is only large on the first frame (terrain bands),
  and snappy on repeated grass tiles compresses it well.
- Same failure surface as ctf: `useMalloc` + `ABORTING_MALLOC` + a stage-note
  buffer read after abort; the node smoke loads the exact emitted module.

Deviation from ctf: playback state (step index, playing, speed, seat) lives
in the PAGE, not in Nim. ctf's sim owns the tick clock because the sim must
advance; our replay is a list of snapshots, so the page's chrome
(`chrome_common.js`, ported by the chrome pass) drives it directly and tells
the renderer which frame to draw with the same text-command channel ctf
uses (`0x81` text `s:<step>` seek, `v:<seat>` POV/seat select). Nim is a
pure renderer of (seat, step) plus terrain.

## Board geometry

- Tile = 32 px (Factorio normal-res). Board = tight bounding box of terrain
  content (resources, water, trees) ∪ every entity in every seat/step, plus a
  4-tile margin, clamped to `map.bounds`. The FLE lab map comes out ≈
  120×110 tiles = 3840×3520 px — the same class as ctf's 4992² colossal
  boards. If a board would exceed 24 M px at 32 px/tile it renders at
  16 px/tile (`MaxBoardPixels`), the same fallback rule ctf applies with
  `MaxSupersampledMapPixels`.
- Terrain (grass, water, ore with amount-bucket variants, trees) is baked in
  Nim into ≤ 60 horizontal band sprites (ids 40+n, objects 40+n, z=-32768)
  emitted once; ctf's client caches them as the static base.
- Per frame: entities (atlas sprites, y-sorted via z = bottom px), belts
  per tile per direction, pipes with neighbour-derived shape, character,
  thin red status outline for problem statuses (no_fuel, no_power,
  no_minable_resources, …), and the chrome sprite (4090) with JSON
  `{kind:"factorio", seat, step, board:{tile, x0, y0, w, h}, …}` the page
  uses to map hover coordinates back to tiles.
- Camera: ctf's core (fit whole board at zoom 1; wheel zoom to cursor; drag
  pan; `f` refits). The page also fits to the current step's base bbox via
  `setZoom` + `panTo` on load ("fit base").

## Sprites

`viewer/assets/atlas.png` + `atlas.json` are built by
`viewer/tools/build_atlas.py` from FLE's sprite pipeline (HF dataset
`Noddybear/fle_images` — the Factorio `__base__/graphics` sheets FLE
redistributes — through FLE 0.3.0's `EntitySpritesheetExtractor` +
`basisu`). Entities are stored centred on the entity centre (FLE's renderer
convention), so `atlas.json` carries `cx, cy` (pixel of the entity centre
in the sprite). Kinds with no sheet in the atlas fall back to a flat
coloured block drawn in Nim (the old C viewer's palette), so an unknown
entity is never invisible. Wube owns the art; see `viewer/assets/README.md`.

## Files

- `replay-viewer/factorio_replay.nim`, `replay-viewer/config.nims` — Nim →
  wasm (`nim c -d:emscripten`), exports `factorio_load_replay/frame/input/
  packet_ptr/len/error_ptr/len/stage_ptr/len`; `--preload-file
  viewer/assets@assets`.
- `client/broadcast_core.js` (ctf, verbatim), `client/static_replay.js`,
  `client/static_replay_worker.js` (ctf, renamed exports; init accepts
  `replayUrl` or `replayBytes`), `client/replay_broadcast.html` (board page
  = chrome pass's page with the wasm glue swapped for the worker core).
- `viewer/build_viewer.sh` local build → `viewer/dist` (index.html +
  factorio_replay.{js,wasm,data} + broadcast_core.js + static_replay*.js +
  chrome_common.js) with the same `test -f` / negative-grep guard chain as
  ctf's Dockerfile.replay-viewer; `Dockerfile` `wasm-builder` stage
  (emsdk 4.0.15 + nimby 0.1.27 + Nim 2.2.4 + `nimby --global sync
  nimby.lock`); `tools/build_replay_viewer.sh` builds that stage and copies
  `viewer/dist` out; `tools/wasm_replay_smoke.cjs` node smoke.
