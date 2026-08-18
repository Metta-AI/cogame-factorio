# Working in this repo

cogame-factorio wraps the Factorio Learning Environment (FLE 0.3.0,
Factorio 1.1.110 headless) as a Coworld game. Design and contract docs:
`docs/PROTOCOL.md` (wire protocol + results), `docs/REPLAY.md` (replay
JSON + viewer contract), `docs/plans/` (design history), and the manifest
template `coworld_manifest_template.json` (the platform contract).

## Inviolable rules

1. **Closed schemas stay in triple sync.** `server/cogame_factorio/results.py`
   (`results_doc` keys + `end_reason` enum) == manifest `results_schema` ==
   `tools/ci/docker_smoke.sh` expected keys. `tests/test_manifest.py` is the
   tripwire; never weaken it.
2. **Degrade, never hang.** Every wait is bounded: connect timeout, step
   deadline, program timeout, strike rule, wall-clock hard stop
   (`wall_clock_budget_seconds` < `episode_timeout_minutes`). Bad player input
   is a noop, never a crash. Program errors are *game outcomes*, not faults.
3. **Broadcast `done` before writing artifacts**; write results and replay
   independently, aggregate errors, exit 0 only after both are attempted.
4. **`num_agents` in every variant and the certification fixture** (the
   ladder schedules zero episodes without it).
5. **The replay is the viewer's only input.** Names, task, terrain, per-step
   snapshots and result all live inside the replay document.
6. Factorio's headless binary is downloaded at image build time from
   factorio.com (free, no auth). Never commit or redistribute it, and never
   ship Wube sprite assets in the viewer — the viewer draws its own shapes.

## Where things live

- `server/cogame_factorio/` — `config.py` (GameConfig ↔ config_schema),
  `factorio.py` (Factorio child-process manager, one server per seat),
  `session.py` (FLE FactorioInstance wrapper: reset, eval, observation,
  score, terrain capture), `engine.py` (per-seat step loop, deadlines,
  strikes, wall clock), `replay.py`, `results.py`, `server.py` (aiohttp:
  `/player`, `/global`, `/client/*`, `/healthz`, replay mode), `uris.py`.
- `players/` — `client.py` (shared websocket harness: env-var URL, reconnect,
  exit-on-done) + `idle_player.py`, `handcraft_player.py`,
  `burner_player.py`, `llm_player.py`. Each is `python -m players.<name>`.
- `viewer/` — static wasm replay viewer: `viewer_main.c` (raylib → emscripten
  draws the map), `index.html` (loads the replay, renders code/output/score
  panels, drives the wasm). Built by `viewer/build_viewer.sh` into
  `viewer/dist/`; `tools/build_replay_viewer.sh` is the `coworld build` hook.
- `tests/` — offline pytest (fake FLE session) plus `-m factorio` tests
  that need `COGAME_FACTORIO_SERVERS` pointing at running servers
  (`fle cluster start -n 2` locally).

## Build / test / package

```sh
uv sync                                   # runtime + dev deps
uv run pytest                             # offline suite
bash viewer/build_viewer.sh               # -> viewer/dist (needs emcc)
docker build --platform=linux/amd64 -t cogame-factorio:local .
uv run coworld build --version X.Y.Z --project . --compose compose.yaml \
   --template coworld_manifest_template.json --output dist/coworld_manifest.json
uv run coworld certify dist/coworld_manifest.json
uv run coworld upload-coworld dist/coworld_manifest.json --timeout-seconds 900 \
   --wait-hosted-smoke --hosted-smoke-timeout-seconds 1800
```

Commit in small units with pathspec `git add`. TDD for behavior changes.
