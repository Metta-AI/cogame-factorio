# viewer/assets — Factorio sprite atlas

`atlas.png` + `atlas.json` are the sprites the static replay viewer draws
(`replay-viewer/factorio_replay.nim` preloads this directory into the wasm
filesystem as `assets/`). They are cut from Factorio 1.1's own
`__base__/graphics` sheets — the copies FLE (Factorio Learning Environment)
redistributes through the Hugging Face dataset `Noddybear/fle_images` and
uses for its own renderer — by `viewer/tools/build_atlas.py` (Pillow +
`basisu`; FLE 0.3.0's `EntitySpritesheetExtractor` output feeds it).

**The art is owned by Wube Software** (Factorio). It is included here the way
FLE includes it: for rendering replays of the game, not as a game asset for
any other purpose. If that redistribution ever becomes a problem, delete the
two files and rebuild — the renderer falls back to flat blocks per entity
kind, and the terrain to a flat fill.

Manifest contract: `{"tile_px": 32, "sprites": [{name, dir, x, y, w, h,
cx, cy}], "by_name": {"<name>|<dir>": index}}` — `cx, cy` is the pixel of
the entity centre inside the sprite. Keys: `<entity>|north|east|south|west`
(or `|` for direction-less kinds), `pipe|<shape>`, `transport-belt|<dir>`
and `|<from>_to_<to>` curves, `underground-belt|in-<dir>`,
`<ore>|<volume 1..8>-<variant 1..8>`, `crude-oil|<variant>`,
`water|<variant>`, `tree|<type>-<variant>`, `ground|<n>` (real Factorio
grass decoratives — the mirror carries no grass tile sheet, so the ground
is a flat grass-1-toned fill under them).

Regenerate:

```sh
python viewer/tools/build_atlas.py --hfrepo <fle_images checkout> \
    --extracted <FLE EntitySpritesheetExtractor output dir>
```
