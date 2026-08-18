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
7. **`GAME_VERSION` is a claim, not a counter.** `server/cogame_factorio/version.py`
   holds `GAME_VERSION` with a prepend-only changelog in the shape
   `GVnn (short rule name): HEADLINE`. Anything that changes what a policy
   observes or how a seat is scored bumps it in the same commit. Replays and
   `welcome` carry it; the viewer shows it. Before claiming a number, check
   every `origin/*` branch for a competing claim on it (compare the RULE
   headline, not the digits).
8. **Wire strings live in one zero-import module.** `server/cogame_factorio/contract.py`
   (stdlib only) hoists every message type / key / enum a policy reads;
   `tests/contract_manifest.txt` is its golden copy. Renaming anything there is
   a four-surface change: contract.py, the manifest txt, docs/PROTOCOL.md, and
   players/. The failure this prevents is SILENT (a bot just stops acting).

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

## Workflows that are easy to get wrong

- **Publishing.** CI publishes on every green push to `main`
  (`.github/workflows/ci.yml`, job `upload-coworld`): version = highest
  existing `factorio` registry row patch-bumped by
  `tools/ci/next_coworld_version.py` (never `coworld next-version`, see the
  script's postmortem docstring). The first upload must be a local
  `coworld upload-coworld` or a `workflow_dispatch` with an explicit
  `version`. The job re-lists and hard-fails unless the new row is
  `canonical` — non-canonical means hosted smoke failed and the league will
  not advance.
- **Fixtures.** The certification fixture pins EVERY field its ending
  depends on (task, max_steps, deadlines, wall clock), not just the ones it
  overrides — a fixture that inherits defaults goes stale without any rule
  change.
- **Replays.** Watch one in the static viewer with your own eyes before
  every upload (`bash viewer/build_viewer.sh` then serve `viewer/dist` with
  `?replay=<url>`); certification does not check that the viewer renders
  the right game.
- **League evidence.** `docs/DEPLOYMENT.md` records ids, settings and
  verification evidence; `docs/ladder/` holds one dated verdict per policy
  change with the decision rule stated *before* the measurement.
