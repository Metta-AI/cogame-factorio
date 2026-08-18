"""One seat's FLE session: a ``FactorioInstance`` on that seat's server.

Everything here is synchronous and blocking (FLE talks RCON); the engine
runs each seat's session on its own thread of a ThreadPoolExecutor. FLE
itself is imported lazily so importing this module (and the server) stays
cheap and offline tests never need Factorio.

The ``Session`` protocol below is what the engine consumes;
``tests/fakes.py`` provides a deterministic in-memory implementation.
"""

from __future__ import annotations

import math
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import GameConfig
from .replay import empty_terrain, flatten_entities, rle_rows

# FLE's RCON password (fle/cluster/run_envs.py RCON_PASSWORD).
RCON_PASSWORD = "factorio"

# Populated lab-play starting inventory (fle.eval.tasks.throughput_task);
# also used for open_play so baselines can build (DefaultTask ships an
# empty inventory).
LAB_PLAY_POPULATED_STARTING_INVENTORY = {
    "coal": 500, "burner-mining-drill": 50, "wooden-chest": 10,
    "burner-inserter": 50, "inserter": 50, "transport-belt": 500,
    "stone-furnace": 10, "boiler": 2, "offshore-pump": 2, "steam-engine": 2,
    "electric-mining-drill": 50, "medium-electric-pole": 500, "pipe": 500,
    "assembling-machine-2": 10, "electric-furnace": 10,
    "pipe-to-ground": 100, "underground-belt": 100, "pumpjack": 10,
    "oil-refinery": 5, "chemical-plant": 5, "storage-tank": 10,
}

# Program output kept on the wire / in the replay (per step).
MAX_OUTPUT_CHARS = 64 * 1024
# Terrain capture area (tile coords, half-open) and tree cap. The FLE lab
# map's patches span x -71..39, y -4..96 (stone/coal north, crude oil,
# copper/iron south) with a lake to the west (x <= -11).
TERRAIN_BOUNDS = (-128, -64, 128, 128)
MAX_TREES = 4000
WATER_TILES = ("water", "deepwater", "water-green", "deepwater-green",
               "water-shallow", "water-mud")

_ERROR_LINE = re.compile(r"(?m)^\s*(?:[A-Za-z_]\w*Error|Exception|"
                         r"[A-Za-z_]\w*Exception|Error)\b")


@dataclass(frozen=True)
class ProgramResult:
    output: str
    error: bool
    wall_ms: int


@dataclass
class Observed:
    """One world read: the wire observation plus the replay snapshot."""
    observation: dict
    snapshot: dict = field(default_factory=dict)


class Session(Protocol):
    def start(self) -> None: ...
    def system_prompt(self) -> str: ...
    def task_info(self) -> dict: ...
    def is_throughput_task(self) -> bool: ...
    def starting_inventory(self) -> dict: ...
    def run_program(self, code: str) -> ProgramResult: ...
    def observe(self, last_program: dict | None) -> Observed: ...
    def score(self) -> float: ...
    def ticks(self) -> int: ...
    def throughput(self) -> float | None: ...
    def capture_terrain(self) -> dict: ...
    def close(self) -> None: ...


def output_has_error(output: str) -> bool:
    """FLE's eval never raises: errors are text. Detect FLE's own
    'Error occurred:' marker, the timeout sentinel, or a traceback tail
    line ('ZeroDivisionError: ...')."""
    if not output:
        return False
    if "Error occurred:" in output or output.startswith("Error"):
        return True
    return bool(_ERROR_LINE.search(output))


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} chars truncated]"


def _jsonable(value: Any) -> Any:
    """Coerce FLE values (numpy scalars, enums, pydantic) to JSON types."""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, type):  # a class smuggled into an entity field
        return getattr(value, "__name__", str(value))
    if hasattr(value, "item") and callable(value.item):  # numpy scalar
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    if hasattr(value, "value") and not isinstance(value, dict):  # Enum
        return _jsonable(value.value)
    if hasattr(value, "model_dump"):
        return _jsonable(entity_dump(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def entity_dump(entity: Any) -> dict:
    """``model_dump(mode="json", exclude={"prototype"})`` with a fallback:
    FLE entities allow extra fields and some carry non-serializable
    values (classes, ...); fall back to a python-mode dump coerced by
    ``_jsonable`` rather than losing the observation."""
    try:
        return entity.model_dump(mode="json", exclude={"prototype"})
    except Exception:
        try:
            return _jsonable(entity.model_dump(exclude={"prototype"}))
        except Exception:
            return {"name": str(getattr(entity, "name", "")),
                    "repr": str(entity)[:2000]}


def _inventory_dict(inv: Any) -> dict:
    """{item: count} with positive int counts (FLE Inventory model /
    dict; counts may be numpy ints)."""
    if inv is None:
        return {}
    raw = inv.model_dump() if hasattr(inv, "model_dump") else dict(inv)
    out = {}
    for k, v in raw.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(k)] = n
    return out


def check_task_key(task_key: str) -> str | None:
    """Return an error message if FLE does not know ``task_key``."""
    from fle.eval.tasks.task_definitions.task_registry import (
        get_task_config)
    try:
        get_task_config(task_key)
    except KeyError as exc:
        return str(exc)
    return None


class FactorioSession:
    """Real FLE-backed session for one seat (blocking; one thread)."""

    def __init__(self, slot: int, host: str, rcon_port: int,
                 config: GameConfig):
        self.slot = slot
        self.host = host
        self.rcon_port = rcon_port
        self.config = config
        self.instance = None
        self.task = None
        self._score0 = 0.0
        self._last_verify: tuple[bool, float] | None = None
        self._system_prompt: str | None = None

    # -- lifecycle -----------------------------------------------------------

    START_ATTEMPTS = 3

    def start(self) -> None:
        """Connect FLE to this seat's Factorio server and set the task up.

        Retried: FLE's setup reads large RCON replies (research state,
        recipes) that occasionally arrive malformed under load — one hosted
        smoke run died at `task.setup` with `KeyError: 'ingredients'` while
        its siblings passed. A fresh instance + retry is the fix; the failure
        is not deterministic.
        """
        import time
        import traceback

        last: BaseException | None = None
        for attempt in range(1, self.START_ATTEMPTS + 1):
            try:
                self._start_once()
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._log(f"session start attempt {attempt}/{self.START_ATTEMPTS} "
                          f"failed: {type(exc).__name__}: {exc}")
                if attempt == self.START_ATTEMPTS:
                    traceback.print_exc()
                inst = self.instance
                self.instance = None
                if inst is not None:
                    try:
                        inst.cleanup()
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(2.0 * attempt)
        assert last is not None
        raise last

    def _start_once(self) -> None:
        from fle.env import FactorioInstance
        from fle.eval.tasks.task_factory import TaskFactory
        from fle.eval.tasks.throughput_task import ThroughputTask

        cfg = self.config
        task = TaskFactory.create_task(cfg.task)
        if not isinstance(task, ThroughputTask):
            # open_play (DefaultTask): empty inventory + no research by
            # default; give it the populated lab inventory and all
            # technologies so scripted baselines can build.
            task.starting_inventory = dict(LAB_PLAY_POPULATED_STARTING_INVENTORY)
            task.all_technology_reserached = True
        self.task = task

        inst = FactorioInstance(
            address=self.host, tcp_port=self.rcon_port, fast=cfg.fast,
            num_agents=1, inventory=dict(task.starting_inventory),
            cache_scripts=True, all_technologies_researched=True,
            clear_entities=True, peaceful=True, reset_speed=cfg.game_speed)
        self.instance = inst
        task.setup(inst)
        self._unpause()
        self._score0 = self._raw_score()
        self._log(f"session ready on {self.host}:{self.rcon_port} "
                  f"(task {cfg.task}, speed {cfg.game_speed:g}, "
                  f"fast={cfg.fast})")

    def _unpause(self) -> None:
        # GOTCHA: game.tick_paused may be left true by a previous session
        # on this server; force it off and apply the configured speed.
        inst = self.instance
        inst.rcon_client.send_command("/sc game.tick_paused = false")
        inst.game_control._is_paused = False
        inst.set_speed(self.config.game_speed)

    def close(self) -> None:
        inst = self.instance
        if inst is None:
            return
        self.instance = None
        # Not FactorioInstance.cleanup(): it joins every non-daemon thread
        # in the process (5s each) — hostile inside a threaded server.
        try:
            inst.rcon_client.close()
        except Exception:
            pass
        try:
            inst._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        print(f"seat {self.slot}: {msg}", file=sys.stderr, flush=True)

    # -- task ----------------------------------------------------------------

    def system_prompt(self) -> str:
        if self._system_prompt is None:
            self._system_prompt = self.instance.get_system_prompt(0)
        return self._system_prompt

    def task_info(self) -> dict:
        task = self.task
        instructions = None
        if getattr(task, "agent_instructions", None):
            try:
                instructions = task.get_agent_instructions(0)
            except Exception:
                instructions = None
        return {"key": task.task_key,
                "goal_description": task.goal_description,
                "agent_instructions": instructions}

    def is_throughput_task(self) -> bool:
        from fle.eval.tasks.throughput_task import ThroughputTask
        return isinstance(self.task, ThroughputTask)

    def starting_inventory(self) -> dict:
        return _inventory_dict(dict(self.task.starting_inventory or {}))

    # -- play ----------------------------------------------------------------

    def run_program(self, code: str) -> ProgramResult:
        inst = self.instance
        timeout = max(1, int(math.ceil(self.config.program_timeout_seconds)))
        self._unpause()
        t0 = time.monotonic()
        try:
            _score, _goal, output = inst.eval_with_error(
                code, agent_idx=0, timeout=timeout)
        except TimeoutError:
            output, error = "Error: Evaluation timed out", True
        except Exception as exc:  # e.g. SyntaxError from ast.parse
            msg = str(exc.args[0]) if exc.args else str(exc)
            output, error = f"{type(exc).__name__}: {msg}".strip(), True
        else:
            output = "" if output is None else str(output)
            error = output_has_error(output)
        wall_ms = int((time.monotonic() - t0) * 1000)
        return ProgramResult(output=_clip(output), error=error,
                             wall_ms=wall_ms)

    def _raw_score(self) -> float:
        try:
            value, _goal = self.instance.namespaces[0].score()
            return float(value)
        except Exception:
            return 0.0

    def score(self) -> float:
        return self._raw_score() - self._score0

    def ticks(self) -> int:
        try:
            return int(self.instance.get_elapsed_ticks())
        except Exception:
            return 0

    def observe(self, last_program: dict | None) -> Observed:
        inst = self.instance
        ns = inst.namespaces[0]
        entities = ns.get_entities()
        dumps = [_jsonable(entity_dump(e)) for e in entities]
        inventory = _inventory_dict(ns.inspect_inventory())
        try:
            flows = _jsonable(ns._get_production_stats())
        except Exception:
            flows = {}
        if not isinstance(flows, dict):
            flows = {}
        for key in ("input", "output", "harvested", "crafted"):
            flows.setdefault(key, {})
        score = self.score()
        tick = self.ticks()
        try:
            speed = float(inst.get_speed())
        except Exception:
            speed = self.config.game_speed
        try:
            messages = _jsonable(ns.get_messages())
        except Exception:
            messages = []
        if self.is_throughput_task():
            success, thr = self._last_verify or (False, 0.0)
            verification = {"success": bool(success),
                            "meta": {"throughput": float(thr)}}
        else:
            verification = None
        text = "" if last_program is None else str(last_program.get("output", ""))
        reprs = "".join(str(e) for e in entities)
        raw_text = f"{text}\n{reprs}".strip() if reprs else text
        observation = {
            "raw_text": _clip(raw_text, 2 * MAX_OUTPUT_CHARS),
            "entities": dumps,
            "inventory": inventory,
            "flows": flows,
            "score": score,
            "game_info": {"tick": tick, "time": tick / 60.0, "speed": speed},
            "task_verification": verification,
            "last_program": last_program,
            "messages": messages if isinstance(messages, list) else [],
        }
        loc = getattr(ns, "player_location", None)
        character = {"x": float(getattr(loc, "x", 0.0)),
                     "y": float(getattr(loc, "y", 0.0))}
        snapshot = {"score": score, "tick": tick, "character": character,
                    "inventory": inventory,
                    "flows_output": flows.get("output", {}) or {}}
        snapshot.update(flatten_entities(dumps))
        return Observed(observation=observation, snapshot=snapshot)

    def throughput(self) -> float | None:
        """FLE holdout verification (ThroughputTask.verify): sleeps the
        holdout period until throughput stops rising — seconds of wall
        clock per call, so the engine calls it once at the end."""
        if not self.is_throughput_task():
            return None
        from fle.commons.constants import REWARD_OVERRIDE_KEY
        try:
            response = self.task.verify(self._raw_score(), self.instance,
                                        step_statistics={})
        except Exception as exc:
            self._log(f"throughput verification failed: {exc!r}")
            return 0.0
        value = float(response.meta.get(REWARD_OVERRIDE_KEY, 0.0) or 0.0)
        self._last_verify = (bool(response.success), value)
        return value

    # -- terrain -------------------------------------------------------------

    def _rcon_json(self, lua: str):
        import json
        raw = self.instance.rcon_client.send_command("/sc " + lua)
        return json.loads(raw) if raw else []

    def capture_terrain(self) -> dict:
        x0, y0, x1, y1 = TERRAIN_BOUNDS
        terrain = empty_terrain(TERRAIN_BOUNDS)
        try:
            resources: list = []
            # quadrants keep each RCON response modest
            xm, ym = (x0 + x1) // 2, (y0 + y1) // 2
            for (ax, ay, bx, by) in ((x0, y0, xm, ym), (xm, y0, x1, ym),
                                     (x0, ym, xm, y1), (xm, ym, x1, y1)):
                resources.extend(self._rcon_json(f"""
local out = {{}}
for _, e in pairs(game.surfaces[1].find_entities_filtered{{
    area={{{{{ax},{ay}}},{{{bx},{by}}}}}, type="resource"}}) do
  out[#out+1] = {{e.name, math.floor(e.position.x),
                  math.floor(e.position.y), e.amount}}
end
rcon.print(game.table_to_json(out))"""))
            seen = set()
            rows = []
            for r in resources:
                key = (r[0], int(r[1]), int(r[2]))
                if key in seen:
                    continue
                seen.add(key)
                rows.append([str(r[0]), int(r[1]), int(r[2]), int(r[3])])
            rows.sort(key=lambda r: (r[2], r[1], r[0]))
            terrain["resources"] = rows

            names = ",".join(f'"{n}"' for n in WATER_TILES)
            water = self._rcon_json(f"""
local out = {{}}
for _, t in pairs(game.surfaces[1].find_tiles_filtered{{
    area={{{{{x0},{y0}}},{{{x1},{y1}}}}}, name={{{names}}}}}) do
  out[#out+1] = {{t.position.x, t.position.y}}
end
rcon.print(game.table_to_json(out))""")
            terrain["water"] = rle_rows([(t[0], t[1]) for t in water])

            trees = self._rcon_json(f"""
local out = {{}}
local n = 0
for _, e in pairs(game.surfaces[1].find_entities_filtered{{
    area={{{{{x0},{y0}}},{{{x1},{y1}}}}}, type="tree"}}) do
  n = n + 1
  if n > {MAX_TREES} then break end
  out[#out+1] = {{math.floor(e.position.x), math.floor(e.position.y)}}
end
rcon.print(game.table_to_json(out))""")
            terrain["trees"] = sorted(
                {(int(t[0]), int(t[1])) for t in trees})
            terrain["trees"] = [list(t) for t in terrain["trees"]]

            spawn = self._rcon_json("""
local c = global.agent_characters and global.agent_characters[1]
if c and c.valid then
  rcon.print(game.table_to_json({c.position.x, c.position.y}))
else
  rcon.print("[0,0]")
end""")
            if isinstance(spawn, list) and len(spawn) == 2:
                terrain["spawn"] = {"x": float(spawn[0]), "y": float(spawn[1])}
        except Exception as exc:
            self._log(f"terrain capture failed: {exc!r}")
        return terrain
