# cogame-factorio replay format (v1)

The replay is a single UTF-8 JSON document written to `COGAME_SAVE_REPLAY_URI`
(content-type `application/json`). It is self-describing: everything the
static viewer needs (names, task, terrain, per-step snapshots, result) is
inside the document — the viewer has no other data source.

The static wasm viewer (`viewer/`, bundle `static-replay-viewer`) is opened
as `index.html?replay=<url-encoded URL>` and fetches the document itself. In
the game container's replay mode the same page is served at
`/client/replay/` and falls back to fetching `/replay-data`.

```jsonc
{
  "format": "cogame-factorio-replay",
  "version": 1,
  "game_version": "1",                        // server/cogame_factorio/version.py GAME_VERSION
  "config": { /* resolved game config, tokens EXCLUDED */ },
  "names": ["Player1", "Player2"],           // seat order
  "task": {"key": "open_play", "goal_description": "…"},

  // Terrain captured once (all seats play the same scenario map).
  // Coordinates are Factorio tile coordinates (integers, +x east, +y south).
  // Read the area from `bounds` (the FLE lab map's patches span roughly
  // x -71..39, y -4..96 with a lake to the west; the server captures
  // x -128..128, y -64..128).
  "map": {
    "bounds": {"x0": -128, "y0": -64, "x1": 128, "y1": 128}, // captured area (half-open)
    "resources": [["iron-ore", 15, 70, 1200], …],            // [name, tile_x, tile_y, amount]
    "water": [[-30, -20, 12], …],                             // [tile_x, tile_y, run_length_east]
    "trees": [[-10, 4], …],                                   // optional; [tile_x, tile_y]
    "spawn": {"x": 0, "y": 0}
  },

  "seats": [
    {
      "slot": 0,
      "name": "Player1",
      "final_score": 123.0,
      "dead": false,
      "steps": [
        {
          "step": 0,
          "code": "pos = nearest(Resource.IronOre)\n…",   // "" for a noop step
          "noop": false,
          "output": "8: (BurnerMiningDrill(...),)",       // program output or error text
          "error": false,
          "score": 3.0,                                     // production score AFTER this step (FLE nets consumption: can be negative)
          "throughput": null,                               // throughput tasks only: measured once when the seat finishes, on its last step
          "tick": 1007,                                     // FLE elapsed ticks after this step
          "wall_ms": 1832,                                  // execution wall time
          "character": {"x": 15.5, "y": 70.5},
          // Entity snapshot AFTER this step: compact rows
          //   [name, x, y, direction, status, width, height]
          //   direction: 0 N, 2 E, 4 S, 6 W (FLE Direction values), -1 unknown
          //   status: FLE EntityStatus value string ("working", "no_fuel", …) or ""
          "entities": [["burner-mining-drill", 16.5, 71.5, 4, "working", 2, 2],
                       ["iron-chest", 16.5, 73.5, 0, "normal", 1, 1]],
          "belts": [[16.5, 74.5, 2], …],                     // individual belt tiles [x, y, direction] (flattened from BeltGroups)
          "pipes": [[10.5, 3.5], …],                          // individual pipe tiles [x, y]
          "inventory": {"coal": 30, "stone-furnace": 2},
          "flows_output": {"iron-ore": 12}                    // cumulative production output counts
        }
      ]
    }
  ],

  "result": { /* the results document, identical to COGAME_RESULTS_URI */ }
}
```

Notes

- Positions are FLE entity center positions (floats; a 2×2 drill at tile
  (16,71) has center (17.0, 72.0); FLE reports e.g. 16.5/71.5 for odd cases —
  the viewer draws a `width×height` rectangle centered on `(x, y)`).
- `entities` never contains belt/pipe tiles; those are flattened into
  `belts` / `pipes` from FLE's `BeltGroup` / `PipeGroup` so the viewer can
  draw them per tile. Electric poles appear in `entities` (from
  `ElectricityGroup.poles`).
- Sizes: ~1 KB per entity row is *not* the case here — a row is ~50 bytes;
  a 30-step, 2-seat episode with ~100 entities is ≈ 0.5–1 MB. The
  server keeps everything in memory and writes once at the end (plus a
  best-effort partial replay on `sim_fault`).
- The results document embedded in `result` uses the same closed schema as
  the manifest `results_schema`.
