# cogame-factorio: rules for policy authors

The authoritative rules of the game and the contract a policy sees. The
wire-level detail is in `docs/PROTOCOL.md`; this page is what you need to
write, test and submit a policy.

## The game in one paragraph

Each seat gets **its own headless Factorio 1.1.110 server** running the
Factorio Learning Environment (FLE 0.3.0) lab scenario: a flat map with
iron, copper, coal and stone patches and water near the origin, and a
populated starting inventory (burner and electric mining drills, stone
furnaces, chests, belts, inserters, poles, pipes, boilers, steam engines,
an offshore pump, coal, ...). A seat plays `max_steps` steps; each step it
receives an **observation** and answers with a **Python program** written
against the FLE agent API. The program runs against the seat's persistent
namespace, the result appears in the next observation, and the score is
read from the world at the end. Seats never share a map, so scores are
absolute.

## Scoring (per variant)

| variant / task | score = |
|---|---|
| `open_play` (variants `open_play`, `solo`, certification) | FLE **production score**: the value of everything your factory produced (mined, smelted, crafted) over the episode, using FLE's item-value table. Higher-tier items are worth more than raw ore; automation that keeps running while you `sleep()` compounds. |
| `*_throughput` tasks (e.g. `iron_plate_throughput`) | **Achieved items/minute** of the task's target item, measured with FLE's holdout verification after the last step (the factory must run unattended). FLE's own quota (e.g. 16/min) is not a pass/fail gate here: the raw rate is the score. |

Results carry both numbers for every seat (`scores`, `production_scores`,
`throughputs`; see `docs/PROTOCOL.md`). Higher is better; there are no
penalties for errors, only for silence (below).

## Steps, deadlines and strikes

- Steps are numbered `0 .. max_steps-1`. Observation `k` shows the world
  before program `k`; `observation.last_program` is program `k-1` with its
  output.
- You have `deadline_seconds` (default 60) after receiving an observation
  to reply `{"type": "program", "step": k, "code": "..."}`. Late, missing,
  malformed, wrong-step or oversize (> 64 KB) replies are a **noop**:
  nothing runs, one strike.
- `strike_limit` (default 3) **consecutive** noops mark the seat **dead**:
  its remaining steps are forfeited and its score is frozen. Any valid
  program resets the count.
- A program that **raises** or hits `program_timeout_seconds` (default 45)
  is **not** a strike: the traceback comes back in the next observation
  (`last_program.error == true`) and play continues. Errors are game
  outcomes; a bad program costs you the step, not the seat.
- Disconnecting is not a strike. Reconnect any time; the server re-sends
  `welcome` and the *current* observation with the remaining deadline.
- The episode also ends on the wall-clock budget; scores are as of then.
- After `done`, the server closes the socket. **Your process must exit.**

## What you receive

`welcome` (once per connection):

```json
{"type": "welcome", "protocol": "cogame.factorio.v1", "game_version": "1",
 "slot": 0, "name": "Player1",
 "task": {"key": "open_play", "goal_description": "...", "agent_instructions": null},
 "max_steps": 30, "step_deadline_seconds": 60, "program_timeout_seconds": 45,
 "episode": {"game_version": "1", "variant_task_key": "open_play", "max_steps": 30,
             "step_deadline_seconds": 60, "program_timeout_seconds": 45,
             "strike_limit": 3, "seats": 2, "slot": 0,
             "map_bounds": {"x0": -64, "y0": -64, "x1": 64, "y1": 64},
             "starting_inventory": {"burner-mining-drill": 50, "stone-furnace": 10, "coal": 500, "...": 0},
             "fast": true, "game_speed": 10},
 "api_docs": "<FLE agent API reference, types, recipes, patterns; 20-60 KB>"}
```

Read episode parameters (`max_steps`, deadlines, `starting_inventory`)
from `welcome.episode`; never hardcode them.

`observation` (one per step):

```json
{"type": "observation", "step": 3, "deadline_seconds": 60,
 "observation": {
   "raw_text": "…FLE text: last program output (numbered) + entity reprs…",
   "entities": [{"name": "burner-mining-drill", "position": {"x": 16.0, "y": 71.0},
                 "direction": 4, "status": "working", "fuel": {"coal": 8},
                 "drop_position": {"x": 16.5, "y": 72.5}, "...": "…"}],
   "inventory": {"coal": 440, "stone-furnace": 4, "iron-plate": 120},
   "flows": {"input": {}, "output": {"iron-ore": 12}, "crafted": [], "harvested": {}},
   "score": 868.0,
   "game_info": {"tick": 12040, "time": 200.6, "speed": 10.0},
   "task_verification": null,
   "last_program": {"code": "...", "output": "9: ('extracted plates:', 168)", "error": false},
   "messages": []}}
```

- `entities` are FLE `Entity` models dumped to JSON (fields vary by class:
  drills carry `fuel`, `drop_position`, `resources`; furnaces
  `furnace_source`/`furnace_result`; belts arrive as groups). `status` is
  an FLE `EntityStatus` value (`working`, `no_fuel`, `no_ingredients`,
  `full_output`, ...). Direction: 0 N, 2 E, 4 S, 6 W.
- `score` is the current production score (open play) or the current task
  metric; `task_verification` carries the throughput task's latest check.

## Writing programs (FLE API)

Programs are plain Python evaluated by FLE with the agent API in scope:
`nearest(Resource.IronOre)`, `move_to(pos)`, `place_entity(Prototype.BurnerMiningDrill,
direction=Direction.DOWN, position=pos)`, `place_entity_next_to(...)`,
`insert_item(Prototype.Coal, drill, quantity=10)`, `extract_item(Prototype.IronPlate,
furnace.position, quantity=50)`, `harvest_resource(pos, quantity=25)`,
`craft_item(Prototype.IronGearWheel, quantity=5)`, `connect_entities(a, b,
Prototype.TransportBelt)`, `get_entities({Prototype.StoneFurnace})`,
`inspect_inventory()`, `sleep(30)`, and so on. The full reference is
`welcome.api_docs`. Things worth knowing:

- **Persistent namespace**: variables and functions defined in step k are
  visible in step k+1 -- *except names starting with `_`*, which are not
  persisted and can be clobbered by stale values. Use plain names, and
  prefer re-querying with `get_entities()` over trusting old handles.
- **Errors abort the program at the failing top-level statement** and roll
  the namespace back to the last successful statement. Wrap risky calls in
  `try/except` and `print()` what happened; you get the output next step.
- **`sleep(n)` advances game time** (game speed 10 by default: 30 game
  seconds cost ~3 real seconds). Production only happens while game time
  passes, so end programs with a sleep once things are running.
- `nearest()` is measured from the character; `move_to()` first.
- The classic bootstrap: a burner drill on ore, `place_entity(Prototype.StoneFurnace,
  position=drill.drop_position)`, coal in both, repeat; then chests on coal
  drills to refuel from. See `players/burner_player.py`.

## Writing a policy with the harness

`players/client.py` speaks the protocol for you (URL from
`COWORLD_PLAYER_WS_URL`, bounded reconnects, deadlines, telemetry, clean
exit). A policy is three methods:

```python
# players/my_player.py
from players.client import Policy, main_for
from players import fle_helpers as H

class MyPolicy(Policy):
    def on_welcome(self, welcome: dict) -> None:
        self.inv = welcome["episode"]["starting_inventory"]

    def program(self, step: int, observation: dict) -> str:
        if step == 0:
            return ("pos = nearest(Resource.IronOre)\nmove_to(pos)\n"
                    "d = place_entity(Prototype.BurnerMiningDrill, direction=Direction.DOWN, position=pos)\n"
                    "f = place_entity(Prototype.StoneFurnace, position=d.drop_position)\n"
                    "insert_item(Prototype.Coal, d, quantity=10)\n"
                    "insert_item(Prototype.Coal, f, quantity=10)\nsleep(30)")
        if H.last_program_failed(observation):
            return "print(inspect_inventory())\nsleep(10)"
        return "sleep(30)"

    def on_done(self, result: dict) -> None:
        pass

if __name__ == "__main__":
    main_for(MyPolicy)
```

`program()` runs in a worker thread; if it raises, returns a non-string, or
overruns the deadline the harness answers `pass` (a valid no-op, not a
strike) and logs why. Exit codes: 0 on `done` (or when the server has gone
away after you played), 1 on fatal setup errors (bad token, no URL), 130 on
SIGINT. Set `COWORLD_PLAYER_ARTIFACT_UPLOAD_URL` (`file://...zip` or an
HTTP PUT URL) to get a per-episode zip of `meta.json`, `events.jsonl`
(step, code, output, error, score, wall ms) and `summary.json`.

Baselines to copy from: `players/idle_player.py` (floor),
`players/handcraft_player.py` (hand mining + furnaces),
`players/burner_player.py` (drills into furnaces, refuel, craft),
`players/llm_player.py` (asks Claude for each program; needs
`uv sync --extra llm` and credentials).

## Testing locally

```sh
uv sync
uv run pytest tests/test_players.py                 # offline harness tests
# a real Factorio: docker + the FLE cluster helper (RCON on localhost:27000)
FLE_STATE_DIR=$PWD/tmp/fle FLE_WORKDIR=$PWD/tmp uv run fle cluster start -n 1
COGAME_FACTORIO_SERVERS=localhost:27000 uv run pytest -m factorio tests/test_baselines.py -s
# a full episode through the game server (see README):
uv run coworld run-episode dist/coworld_manifest.json my-image:latest \
    --run python --run -m --run players.my_player --variant solo -o ./tmp/episode
```

## Submitting

Package the policy as a `linux/amd64` image whose entrypoint runs it, then
upload and submit with the Coworld CLI:

```sh
docker build --platform linux/amd64 -t my-factorio-policy:latest -f Dockerfile .
uv run coworld upload-policy my-factorio-policy:latest \
    --name my-factorio-policy --run python --run -m --run players.my_player
uv run coworld submit my-factorio-policy --league <league-id>       # see `coworld submit --help`
```

The image only needs Python 3.11+ and `aiohttp` (this repo's Dockerfile
already contains the harness and baselines; copy it or start `FROM` it).
The runner injects `COWORLD_PLAYER_WS_URL`; your process must connect,
play, and exit 0 when it receives `done`.
