# cogame-factorio

The [Factorio Learning Environment](https://github.com/JackHopkins/factorio-learning-environment)
(FLE 0.3.0, Factorio 1.1.110 headless) as a Coworld game. Every seat gets
its own Factorio server on the FLE lab map and plays a fixed number of
steps; each step is a Python program written against the FLE agent API
(`place_entity`, `connect_entities`, `nearest`, `craft_item`, `sleep`, ...).
Open play scores each seat by FLE production score; throughput variants
score by achieved items/minute of a target item. Seats never share a map,
so scores are absolute and comparable across episodes. Replays record every
program, its output and an entity snapshot per step, rendered by a static
wasm viewer.

Docs: [`docs/RULES.md`](docs/RULES.md) (rules + observation contract + how
to submit a policy), [`docs/PROTOCOL.md`](docs/PROTOCOL.md) (wire protocol,
results schema, runtime contract), [`docs/REPLAY.md`](docs/REPLAY.md)
(replay JSON + viewer), [`AGENTS.md`](AGENTS.md) (working in this repo).

## How an episode works

1. The game container starts one Factorio server per seat (or uses the
   servers named in `COGAME_FACTORIO_SERVERS`), resets each to the lab
   scenario with FLE's populated lab-play starting inventory, and listens
   on `/player?slot=N&token=T`.
2. Each policy container connects, gets `welcome` (task, episode
   parameters, the FLE API reference) and then, per step, an `observation`
   (FLE text view, entity list, inventory, flows, score, the previous
   program's output). It replies with `{"type": "program", "step": k,
   "code": "<python>"}` within `step_deadline_seconds`.
3. The server runs the program with `FactorioInstance.eval` (persistent
   namespace, `program_timeout_seconds` cap). Exceptions are game outcomes,
   returned as the next observation's `last_program`. Silence (late,
   missing, malformed replies) is a noop and a strike; `strike_limit`
   consecutive strikes kill the seat.
4. When every seat has finished (or the wall-clock budget expires) all
   players get `done` with the results document, the server writes results
   and the replay, and exits 0. Player processes must exit after `done`.

## Scoring

- **`open_play`** (variants `open_play`, `solo`, certification): FLE
  production score, the value of everything the factory produced.
- **`*_throughput`** (e.g. `iron_plate_throughput`): achieved items/minute
  of the target item at the end, from FLE's holdout verification.

Results (see `docs/PROTOCOL.md`) always carry `scores`,
`production_scores`, `throughputs`, per-seat step/error/noop counts,
`dead_seats`, `end_reason` and `wall_clock_seconds`.

## Variants

| id | seats | task | steps |
|---|---|---|---|
| `open_play` | 2 | `open_play` | 30 |
| `iron_plate_throughput` | 2 | `iron_plate_throughput` | 30 |
| `solo` | 1 | `open_play` | 30 |

Certification runs the three baselines (burner, handcraft, idle) on
`open_play` for 4 steps. All variants: 60 s step deadline, 45 s program
timeout, game speed 10, FLE fast mode.

## Baseline players

All in `players/`, each runnable as `python -m players.<name>` inside the
player image (`COWORLD_PLAYER_WS_URL` is injected by the runner):

| module | what it does | 10-step production score (measured) |
|---|---|---|
| `idle_player` | replies `pass` every step | ~0 |
| `handcraft_player` | hand-mines iron, hand-placed stone furnaces, hand-crafts gears | ~3700 |
| `burner_player` | burner drills into furnaces on iron and copper, coal/stone into chests, refuel, extract, craft gears | ~6600 |
| `llm_player` | asks Claude for each program (optional `anthropic`/`boto3` deps) | n/a |

`players/client.py` is the shared harness (env-var URL, bounded
reconnects, deadline handling, telemetry zip, exit codes); write your own
policy against it as described in `docs/RULES.md`.

## Local development

```sh
uv sync                                    # runtime + dev deps
uv run pytest                              # offline suite (fake FLE session, fake seat server)
uv sync --extra llm                        # only for players/llm_player.py
```

Real Factorio (needs Docker; FLE's cluster helper starts
`factoriotools/factorio:1.1.110` with RCON on `localhost:27000`,
`27001`, ...):

```sh
FLE_STATE_DIR=$PWD/tmp/fle-state FLE_WORKDIR=$PWD/tmp/fle uv run fle cluster start -n 2
export COGAME_FACTORIO_SERVERS=localhost:27000,localhost:27001
uv run pytest -m factorio                  # baseline scores, server integration
```

Run a full episode locally through the game server with the Coworld CLI.
`coworld run-episode` takes a built manifest, an optional player image
override and `--run` argv; with `COGAME_FACTORIO_SERVERS` in the game
container's environment the server attaches to already-running Factorio
servers instead of spawning its own (Docker Desktop: use
`host.docker.internal`):

```sh
docker build --platform=linux/amd64 -t cogame-factorio:local .
uv run coworld build --version 0.0.1 --project . --compose compose.yaml \
    --template coworld_manifest_template.json --output dist/coworld_manifest.json
uv run coworld run-episode dist/coworld_manifest.json cogame-factorio:local \
    --run python --run -m --run players.burner_player \
    --variant solo --output-dir ./tmp/episode
```

Results and the replay land in `--output-dir`.
Watch the replay: `bash viewer/build_viewer.sh`, serve `viewer/dist` and
open `index.html?replay=<url of replay.json>`.

## Package and publish

See `AGENTS.md`: `coworld build` with `coworld_manifest_template.json`,
`coworld certify`, `coworld upload-coworld`. CI publishes on green pushes to
`main`.
