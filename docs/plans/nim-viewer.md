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

- Tile = 32 px (Factorio normal-res). Board = tight bounding box of every
  entity / belt / pipe / character in every seat/step, widened by ambient
  terrain (ore patches, water, trees) only within 16 tiles of that play
  area, plus a 4-tile margin, clamped to `map.bounds`. (Resources used to
  always count: the lab map's patches span most of its 256×192 tiles, so a
  22-entity replay produced a 4800×4416 board — 85 MB of terrain RGBA and a
  35 MB first packet the client had to decompress and blit before the first
  frame.) If a board would exceed 8 M px at 32 px/tile it renders at
  16 px/tile (`MaxBoardPixels`; ~90×90 tiles keep full res) from a 2:1
  box-filtered copy of the sheets; above 24 M px (`MaxViewerPixels`) the
  replay is rejected with a stage note.
- First-frame budget (measured 2026-08-18 on the 30-step hosted replay, see
  `tmp/viewer_perf.md`): the Worker decodes `assets/atlas.png` with
  `createImageBitmap` and hands the RGBA (and its 2:1 downsample) to the
  runtime via `factorio_set_atlas` — an in-wasm PNG inflate cost 0.15 s
  native and 1–4 s in a contended browser. Node smoke / native runs still
  decode with pixie. `factorio_profile_ptr/len` exposes the per-stage load
  profile; the Worker logs it and its own marks (`[replay-worker] …`).
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
- Character: drawn every step from the step's `character` {x,y} (always on
  top, facing its motion); a forward step glides it from the previous
  position over `CharGlideFrames` (12 packets ≈ 0.5 s) with a faint trail
  of the last `CharTrailSteps` positions. The page overlays a selection
  ring + name label on the selected seat's character (same glide), and
  clicking a seat name anywhere (scorebug chip, end card row) selects that
  seat and centres the camera on its character; `c` toggles follow.
- Chrome: scorebug band on top (task + game_version chip, step clock, the
  selected seat's step readout, per-seat chips in rank order = the seat
  selector), the board, the collapsible program/output/inventory plaque on
  the right (`p`, edge tab, state in localStorage, `?panes=0` starts folded),
  ctf's transport bar below. Standings live in the chip order + end card.

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

## Verified / not ported

- Verified (2026-08-18): both fixtures render in Chrome (in-app browser +
  puppeteer, `tmp/nim_viewer.png`, `tmp/nim_viewer_end.png`): ore patches,
  water, trees, drills/chests/furnaces/poles/belts from the Factorio sheets;
  scrubber/transport/standings/end card/spoilers from the ctf chrome; hover
  tooltip; wheel/drag camera; seat switch. `uv run pytest` green (viewer
  tests incl. node smoke of the emitted wasm on both fixtures + canary);
  `bash tools/build_replay_viewer.sh $PWD/build/static-replay-viewer`
  (Docker wasm-builder stage) produces a bundle the same smoke accepts.
- Not ported from ctf: `client/league_replayer.html` (ctf's standalone
  theater shell that iframes the board page with a queue) — the Observatory
  contract is `index.html?replay=`, which the board page satisfies alone;
  the shell's loading plate / end card / Esc relay live in the board page.
  Also not ported: ctf's minimap widget and zoom slider (the core supports
  them; the page has no DOM for them yet).
- Known rough edges: belt items are not drawn (the replay carries none);
  entity shadows are not in the atlas; the ground fill is flat grass-toned
  (no grass sheet in the FLE mirror) with real decoratives on top.
