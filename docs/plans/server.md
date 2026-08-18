# Server implementation notes (`server/cogame_factorio/`)

Design record for the game server; the contracts are `docs/PROTOCOL.md`,
`docs/REPLAY.md` and `coworld_manifest_template.json`.

## Shape

- `config.py` — `GameConfig.from_dict` == manifest `config_schema`
  (closed; unknown keys rejected; `num_agents` validated to equal the seat
  count; `wall_clock_budget_seconds` defaults to 0.85 × 60 min).
- `factorio.py` — `FactorioServerManager`: spawns one
  `/opt/factorio/bin/x64/factorio` child per seat with `-c <seat>/config.ini`
  (`write-data=` a per-seat dir; Factorio locks + writes into its write dir,
  so N servers cannot share one; the install tree stays read-only; the FLE
  scenario is reached via a `scenarios` symlink in the write dir), RCON
  27100+slot, UDP 34197+slot. Readiness = RCON TCP accept (Factorio opens
  it right after InGame, ~1 s). Only native Factorio log lines are relayed
  (RCON `/sc` command echoes — thousands, multi-line Lua — are dropped).
  `COGAME_FACTORIO_SERVERS=host:port,...` skips spawning (dev/tests).
- `session.py` — `FactorioSession` (blocking; one executor thread per seat):
  `FactorioInstance` + `TaskFactory` task; open_play (FLE `DefaultTask`)
  is given the populated lab inventory + all technologies so baselines
  can build; `game.tick_paused=false` + `game.speed` forced at start and
  before every program (FLE can leave the game paused). Program execution
  goes through `eval_with_error` so timeouts / parse errors are explicit;
  FLE never raises for runtime errors — errors are detected in the output
  text (`Error occurred:` marker / traceback tail). Score = production
  score delta from a baseline read right after task setup. `observe()`
  does one entity read and produces both the wire observation and the
  replay snapshot. Throughput tasks are verified once when the seat
  finishes (`ThroughputTask.verify` sleeps holdout periods: 6 s+ wall per
  iteration at speed 10). Terrain is captured once over
  x −128..128, y −64..128 via RCON (`find_entities_filtered` /
  `find_tiles_filtered`, JSON, quadrant-chunked).
- `engine.py` — one asyncio task per seat; blocking session calls via
  `run_in_executor`. Wait for connect (bounded), then `max_steps` ×
  (observation → program under deadline → execute → observe/record).
  Strike rule (consecutive noops), wall-clock hard stop (a wait cut short
  by the wall clock is not a strike), per-seat fault containment
  (`sim_fault`). Never-connected seats are reported through a hook.
- `server.py` — aiohttp routes (`/healthz`, `/player`, `/global`,
  `/client/global`, `/client/player`), `WsSeat` = engine `ProgramSource`
  keeping the pending observation so a reconnect gets `welcome` + the
  current step with the remaining deadline; done broadcast before
  artifact writes; fault artifacts on host failure; replay mode serves
  `viewer/dist` at `/client/replay/` and the JSON at `/replay-data`
  (plus the legacy `/replay` websocket header for coworld<=0.1.34's probe).
  `main()` hard-exits after cleanup because FLE's timed-out programs keep
  running on FLE's own executor threads and its atexit cleanup would join
  them.
- `contract.py` (stdlib only) hoists every wire string; `version.py`
  holds `GAME_VERSION` (replay header, welcome, /global status).

## Known limitations

- A program that hits `program_timeout_seconds` is *reported* as timed
  out but FLE cannot kill the thread: it keeps running on FLE's executor
  until it finishes (FLE semantics; the gym env behaves the same).
- Throughput is measured only at the end of a seat (verification costs
  seconds per call); observations carry `task_verification` with the
  last measured value (0.0 until then).
- FLE production score nets consumption; scores can be negative.

## Verification

- Offline: `uv run pytest -m "not factorio"` (fake FLE session, fake
  Factorio binary).
- Live: `fle cluster start -n 2` then
  `COGAME_FACTORIO_SERVERS=localhost:27000,localhost:27001 uv run pytest -m factorio`
  (2-seat/3-step open_play episode → results schema-valid, REPLAY.md
  conformant replay with real entities/terrain, refreshes
  `tests/fixtures/sample_replay.json`; iron_plate_throughput episode).
- Image: `docker build --platform=linux/amd64 -t cogame-factorio:local .`
  then `tools/ci/docker_smoke.sh cogame-factorio:local` (game container
  spawns two Factorio servers; idle + burner players; exit 0; results
  closed key set; replay format).
