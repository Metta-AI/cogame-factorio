# cogame-factorio wire protocol (`cogame.factorio.v1`)

The protocol a policy container speaks to play an episode of the Factorio
Learning Environment (FLE 0.3.0, Factorio 1.1.110) as a Coworld game, plus
the spectator surface and the runtime contract.

## Model of play

- An episode seats `len(config.players)` policies. **Every seat gets its own
  Factorio server** (same scenario, same starting inventory, same task); seats
  never share a map. Scores are therefore per seat and absolute — a seat's
  production score (open play) or achieved throughput (throughput tasks).
- Play is **turn based per seat, not lockstep across seats**: each seat runs
  its own loop of `max_steps` steps and finishes at its own pace. A step is:
  server sends an `observation` → the policy replies with a Python `program`
  → the server executes it against that seat's FLE namespace → the result
  appears in the next `observation`. Programs use the FLE agent API
  (`place_entity`, `move_to`, `connect_entities`, `nearest`, `craft_item`,
  `sleep`, …); the full API reference arrives in `welcome.api_docs`.
- The episode ends when every seat is finished (step cap reached or dead)
  or when the wall-clock budget expires. Then every connected player gets a
  `done` message and the server writes results + replay and exits 0.

## Player websocket (`GET /player?slot=N&token=T`)

A player container receives its fully-formed connection URL in
`COWORLD_PLAYER_WS_URL` (legacy alias `COGAMES_ENGINE_WS_URL`), e.g.
`ws://game-host:8080/player?slot=1&token=abc123`. Connect and speak JSON
text messages:

```
server -> player   {"type": "welcome", ...}                    once per connection
server -> player   {"type": "observation", "step": k, ...}     one per step
player -> server   {"type": "program", "step": k, "code": "…"} the reply for step k
server -> player   {"type": "done", "result": {...}}           episode end, then close
```

### `welcome`

```json
{
  "type": "welcome",
  "protocol": "cogame.factorio.v1",
  "game_version": "1",
  "slot": 0,
  "name": "Player1",
  "task": {"key": "open_play", "goal_description": "…", "agent_instructions": null},
  "max_steps": 30,
  "step_deadline_seconds": 60,
  "program_timeout_seconds": 45,
  "api_docs": "<FLE system prompt: API reference, types, recipes, patterns>",
  "episode": {
    "game_version": "1",
    "variant_task_key": "open_play",
    "max_steps": 30,
    "step_deadline_seconds": 60,
    "program_timeout_seconds": 45,
    "strike_limit": 3,
    "seats": 2,
    "slot": 0,
    "map_bounds": {"x0": -128, "y0": -64, "x1": 128, "y1": 128},
    "starting_inventory": {"coal": 500, "burner-mining-drill": 50, "…": 0},
    "fast": true,
    "game_speed": 10
  }
}
```

`api_docs` is FLE's own agent system prompt for this instance
(`FactorioInstance.get_system_prompt`), ~100 KB. It is sent on every
(re)connection. `game_version` is `server/cogame_factorio/version.py`
`GAME_VERSION` (bumped whenever what a policy sees or how it is scored
changes). `episode` states every episode parameter outright at t=0 —
policies must never infer them from play. Every wire string (message
types, keys, enums) is hoisted in the stdlib-only module
`server/cogame_factorio/contract.py`.

### `observation`

```json
{
  "type": "observation",
  "step": 3,
  "deadline_seconds": 60,
  "observation": {
    "raw_text": "…",                       // FLE-style text: last program output + entity reprs
    "entities": [ {"name": "burner-mining-drill", "position": {"x": 16.5, "y": 71.5},
                   "direction": 4, "status": "working", "type": "…", "id": "…", …} ],
    "inventory": {"coal": 40, "stone-furnace": 2},
    "flows": {"input": {}, "output": {"iron-ore": 12}, "crafted": [], "harvested": {}},
    "score": 3.0,                          // production score so far (open play) / current metric
    "game_info": {"tick": 1007, "time": 16.8, "speed": 10.0},
    "task_verification": {"success": false, "meta": {"throughput": 0.0}} | null,
    "last_program": {"code": "…", "output": "…", "error": false} | null,
    "messages": []
  }
}
```

- `entities` are FLE `Entity` pydantic models dumped with
  `model_dump(mode="json", exclude={"prototype"})` — fields vary by entity
  class (drills carry `fuel`, `drop_position`, `resources`; belts arrive as
  `BeltGroup`s, pipes as `PipeGroup`s, poles as `ElectricityGroup`s).
  Direction is FLE's 8-way enum value (0 N, 2 E, 4 S, 6 W).
- `raw_text` is what FLE's gym env would show a text-only agent: the last
  program's line-numbered output (or error) followed by the entity reprs.
- Step numbering is `0 .. max_steps-1`. Observation `k` shows the world
  before program `k`; `last_program` is program `k-1` and its output.

### `program`

```json
{"type": "program", "step": 3, "code": "pos = nearest(Resource.IronOre)\nmove_to(pos)\n…"}
```

- Must echo the current `step`. Malformed JSON/shape, a non-`program`
  type, non-string `code`, or `code` longer than 64 KB is a **noop** for
  that step: nothing runs, the seat is charged one strike, and the next
  observation is sent (`noop_causes.malformed`). A reply addressed to a
  *different* step is ignored (counted in `noop_causes.wrong_step`); the
  step still waits for a correctly addressed reply until its deadline.
- No reply within `deadline_seconds` is also a noop + strike
  (`noop_causes.timeout`, or `disconnected` if the seat had no connection
  at any point during the step).
- Execution: `FactorioInstance.eval(code, agent_idx=0, timeout=program_timeout_seconds)`.
  A program that raises does **not** strike the seat — errors are part of
  the game; the traceback text comes back in `last_program.output` with
  `error: true`. A program that hits the timeout is interrupted; its partial
  output is returned with `error: true`. Neither counts as a strike.
- **Strike rule:** `strike_limit` (default 3) *consecutive* noops mark the
  seat dead; a dead seat's remaining steps are forfeited and its loop ends
  (its score is whatever it had reached). A valid `program` resets the
  consecutive count.
- Programs run in a persistent per-seat namespace: variables and functions
  defined in step k are visible in step k+1 (FLE semantics).

### Connection semantics

- Bad slot/token → HTTP 403 (fatal). Slot already has a live connection →
  HTTP 409 (retryable; sockets are heartbeated ~20s, so a stale half-open
  socket clears within seconds).
- A seat may disconnect and reconnect any number of times. On reconnect
  the server re-sends `welcome` and then the **current** step's
  `observation` (with the remaining `deadline_seconds`). Disconnection by
  itself is not a strike; missing the step deadline is.
- The episode starts each seat's loop as soon as that seat's Factorio
  server is up **and** the seat has connected, or after
  `player_connect_timeout_seconds` (default 180) — a seat that never
  connects plays noops, strikes out after `strike_limit` steps, and is
  reported to `COGAME_PLAYER_FAILURE_URI`.
- After `done` the server closes the socket; players **must exit** (exit 0)
  — the runner waits for every player container to exit.

## Global viewer (`GET /global`, `GET /client/global`)

`/global` is a broadcast-only websocket: an initial
`{"type": "status", "game_version": "1", "players": [...], "task": {...},
"max_steps": N, "steps": [k0, k1, ...], "scores": [...], "done": false}`
snapshot on connect (plus `"result"` once the episode is over), a `{"type": "progress", "slot": i, "step": k, "score": s}` message
after every executed step, and the final `{"type": "done", "result": {...}}`.
`/client/global` serves a minimal HTML page over that feed;
`GET /client/player?slot=N&token=T` serves a token-checked seat page.

## Results (`COGAME_RESULTS_URI`)

A closed-schema JSON document (see the manifest `results_schema`):

| key | type | meaning |
|---|---|---|
| `names` | string[] | player display names, seat order |
| `scores` | number[] | one scalar per seat: production score (open play) or achieved throughput (throughput tasks) |
| `production_scores` | number[] | FLE production score per seat regardless of task |
| `throughputs` | (number\|null)[] | achieved items/min for throughput tasks, null otherwise |
| `task_key` | string | FLE task key played |
| `steps_completed` | integer[] | programs actually executed per seat |
| `error_steps` | integer[] | executed programs that raised or timed out, per seat |
| `noop_steps` | integer[] | steps forfeited to noop (late/missing/malformed), per seat |
| `dead_seats` | boolean[] | seat struck out |
| `noop_causes` | object[] | per seat `{timeout, malformed, wrong_step, disconnected, host_error}` counters |
| `final_ticks` | integer[] | FLE elapsed game ticks per seat |
| `end_reason` | enum | `steps_cap` (all seats finished or dead), `wall_clock`, `sim_fault` |
| `wall_clock_seconds` | number | episode duration |

## Runtime contract (Coworld)

The game container reads `COGAME_CONFIG_URI` (game config JSON, manifest
`config_schema`), writes `COGAME_RESULTS_URI` (results JSON) and
`COGAME_SAVE_REPLAY_URI` (replay JSON, see `docs/REPLAY.md`), reports
never-connected seats to `COGAME_PLAYER_FAILURE_URI` (one document, lowest
failed slot), binds `COGAME_HOST`:`COGAME_PORT` (default `0.0.0.0:8080`) and
serves `GET /healthz`. `done` is broadcast **before** artifacts are
written; artifact writes are independent and errors aggregated. With
`COGAME_LOAD_REPLAY_URI` set the server runs in replay mode: replay bytes at
`GET /replay-data` and the static viewer bundle at `/client/replay/`.

Factorio servers are child processes of the game container
(`/opt/factorio/bin/x64/factorio -c <seat write-dir>/config.ini
--start-server-load-scenario default_lab_scenario --rcon-port 27100+slot
--port 34197+slot …`), one per seat, each with its own `write-data`
directory (Factorio locks and writes into it) under a read-only install,
unless `COGAME_FACTORIO_SERVERS=host:rcon_port,host:rcon_port,…` names
already-running servers (local development against `fle cluster start`).
Startup knobs: `COGAME_FACTORIO_ROOT` (default `/opt/factorio`),
`COGAME_FACTORIO_RCON_BASE_PORT` (27100), `COGAME_FACTORIO_START_TIMEOUT`
(180 s), `COGAME_FACTORIO_WRITE_DIR` (default a temp dir).

Exit codes: 0 episode complete (artifacts attempted); 2 missing/invalid
config (including an unknown FLE task key); 1 host failure (Factorio or
FLE never came up — fault artifacts with `end_reason: sim_fault` are still
attempted). A Factorio/FLE fault *during* play ends that seat's loop and
the episode reports `sim_fault` with scores as of the fault (exit 0).
