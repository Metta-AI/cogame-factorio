"""Deterministic in-memory stand-ins for FLE (offline tests).

``FakeSession`` implements ``cogame_factorio.session.Session`` without
Factorio: programs "succeed" unless they contain ``raise``/``1/0``, a
``#slow N`` first line sleeps N seconds (program timeout / wall-clock
tests), ``#fault`` raises out of the session (sim_fault path). Each
successful program places one drill (+1 score) and grows a belt group, so
replays have non-empty entities/belts. ``FakeSource`` is a scripted
engine ProgramSource.
"""

from __future__ import annotations

import asyncio
import time

from cogame_factorio.config import GameConfig
from cogame_factorio.replay import flatten_entities
from cogame_factorio.session import Observed, ProgramResult

FAKE_API_DOCS = "# fake FLE api docs\nplace_entity(...)\n" * 20


def drill_dump(i: int, status: str = "working") -> dict:
    return {
        "name": "burner-mining-drill", "direction": 4,
        "position": {"x": 16.0 + 2 * i, "y": 71.0}, "id": 100 + i,
        "energy": 2666.6, "type": "mining-drill",
        "dimensions": {"width": 1.4, "height": 1.4},
        "tile_dimensions": {"tile_width": 2.0, "tile_height": 2.0},
        "health": 150.0, "warnings": [], "status": status,
        "fuel": {"coal": 9}, "drop_position": {"x": 16.5 + 2 * i, "y": 72.5},
    }


def belt_group_dump(n: int) -> dict:
    return {
        "id": 7, "status": "normal", "name": "belt-group",
        "position": {"x": 16.5, "y": 74.5},
        "belts": [{"name": "transport-belt", "direction": 2,
                   "position": {"x": 16.5 + k, "y": 74.5},
                   "status": "normal"} for k in range(n)],
        "inputs": [], "outputs": [], "inventory": {},
    }


def pole_group_dump() -> dict:
    return {
        "id": 3, "status": "normal", "name": "electricity-group",
        "position": {"x": 20.5, "y": 75.5},
        "poles": [{"name": "small-electric-pole", "direction": 0,
                   "position": {"x": 20.5, "y": 75.5}, "status": "normal",
                   "tile_dimensions": {"tile_width": 1.0, "tile_height": 1.0}}],
    }


def pipe_group_dump() -> dict:
    return {
        "id": 9, "status": "normal", "name": "pipe-group",
        "position": {"x": 10.5, "y": 3.5},
        "pipes": [{"name": "pipe", "direction": 0,
                   "position": {"x": 10.5, "y": 3.5 + k}} for k in range(2)],
    }


FAKE_TERRAIN = {
    "bounds": {"x0": -64, "y0": -64, "x1": 64, "y1": 64},
    "resources": [["iron-ore", 15, 70, 1200], ["coal", -20, 30, 800]],
    "water": [[-30, -20, 12]],
    "trees": [[-10, 4]],
    "spawn": {"x": 0, "y": 0},
}


class FakeSession:
    def __init__(self, slot: int, config: GameConfig, *,
                 fail_start: bool = False):
        self.slot = slot
        self.config = config
        self.fail_start = fail_start
        self.started = False
        self.closed = False
        self.programs: list[str] = []
        self._score = 0.0
        self._tick = 0
        self._entities: list[dict] = []
        self._belts = 0
        self._throughput_calls = 0
        self._verified: tuple[bool, float] | None = None
        self.character = {"x": 0.0, "y": 0.0}

    # lifecycle
    def start(self) -> None:
        if self.fail_start:
            raise ConnectionError(f"fake seat {self.slot}: no factorio")
        self.started = True

    def close(self) -> None:
        self.closed = True

    def system_prompt(self) -> str:
        return FAKE_API_DOCS

    def task_info(self) -> dict:
        return {"key": self.config.task,
                "goal_description": "fake goal", "agent_instructions": None}

    def is_throughput_task(self) -> bool:
        return self.config.task.endswith("_throughput")

    def starting_inventory(self) -> dict:
        return {"coal": 500, "burner-mining-drill": 50}

    # play
    def run_program(self, code: str) -> ProgramResult:
        self.programs.append(code)
        t0 = time.monotonic()
        first = code.split("\n", 1)[0]
        if first.startswith("#slow "):
            time.sleep(float(first.split()[1]))
        if "#fault" in code:
            raise RuntimeError("fake factorio died")
        self._tick += 100
        if "raise" in code or "1/0" in code:
            out = ("1: ('Error occurred:\\n  Line 1: x = 1/0\\n\\n"
                   "ZeroDivisionError: division by zero',)")
            return ProgramResult(output=out, error=True,
                                 wall_ms=int((time.monotonic() - t0) * 1000))
        self._score += 1.0
        i = len(self._entities)
        self._entities.append(drill_dump(i))
        self._belts += 1
        self.character = {"x": 15.5 + i, "y": 70.5}
        return ProgramResult(output=f"{len(self.programs)}: ok",
                             error=False,
                             wall_ms=int((time.monotonic() - t0) * 1000))

    def _dumps(self) -> list[dict]:
        dumps = list(self._entities)
        if self._belts:
            dumps.append(belt_group_dump(self._belts))
            dumps.append(pole_group_dump())
            dumps.append(pipe_group_dump())
        return dumps

    def observe(self, last_program):
        dumps = self._dumps()
        inventory = {"coal": 500 - 10 * len(self._entities),
                     "burner-mining-drill": 50 - len(self._entities)}
        flows = {"input": {}, "output": {"iron-ore": 12 * len(self._entities)},
                 "harvested": {}, "crafted": {}}
        if self.is_throughput_task():
            success, thr = self._verified or (False, 0.0)
            verification = {"success": success, "meta": {"throughput": thr}}
        else:
            verification = None
        text = "" if last_program is None else last_program["output"]
        observation = {
            "raw_text": text + "\n" + "".join(
                f"\n\tEntity({e['name']})" for e in self._entities),
            "entities": dumps,
            "inventory": inventory,
            "flows": flows,
            "score": self._score,
            "game_info": {"tick": self._tick, "time": self._tick / 60,
                          "speed": self.config.game_speed},
            "task_verification": verification,
            "last_program": last_program,
            "messages": [],
        }
        snapshot = {"score": self._score, "tick": self._tick,
                    "character": dict(self.character), "inventory": inventory,
                    "flows_output": flows["output"]}
        snapshot.update(flatten_entities(dumps))
        return Observed(observation=observation, snapshot=snapshot)

    def score(self) -> float:
        return self._score

    def ticks(self) -> int:
        return self._tick

    def throughput(self):
        if not self.is_throughput_task():
            return None
        self._throughput_calls += 1
        value = 2.0 * self._score
        self._verified = (value >= 16, value)
        return value

    def capture_terrain(self) -> dict:
        return dict(FAKE_TERRAIN)


def fake_session_factory(**session_kwargs):
    """Returns (factory, created_sessions_list)."""
    created: list[FakeSession] = []

    def factory(slot, endpoint, config):
        s = FakeSession(slot, config, **session_kwargs)
        created.append(s)
        return s

    return factory, created


class FakeSource:
    """Scripted ProgramSource: ``replies`` maps step -> code | None
    (None = no reply, times out) | ("malformed",) etc. ``connect_after``
    seconds delays connection."""

    def __init__(self, replies=None, default_code="place()",
                 connect_after: float = 0.0, connected: bool = True):
        self.replies = replies or {}
        self.default_code = default_code
        self.connect_after = connect_after
        self.connected = connected
        self.wrong_step_count = 0
        self.seen: list[tuple[int, dict]] = []

    async def wait_connected(self, timeout_seconds: float) -> bool:
        if not self.connected:
            await asyncio.sleep(min(timeout_seconds, 0.01))
            return False
        if self.connect_after > timeout_seconds:
            await asyncio.sleep(timeout_seconds)
            return False
        await asyncio.sleep(self.connect_after)
        return True

    async def get_program(self, step, payload, deadline_at):
        self.seen.append((step, payload))
        reply = self.replies.get(step, self.default_code)
        if reply is None:
            await asyncio.sleep(max(0.0, deadline_at - time.monotonic()))
            return None, "timeout"
        if isinstance(reply, tuple):
            return None, reply[0]
        if callable(reply):
            return await reply(step, payload, deadline_at)
        return reply, None


class FakeFactorio:
    """Stand-in FactorioServerManager: no processes, fixed endpoints."""

    def __init__(self, num_seats: int, fail: bool = False):
        self.num_seats = num_seats
        self.fail = fail
        self.started = False
        self.stopped = False

    async def start(self):
        from cogame_factorio.factorio import Endpoint, FactorioError
        if self.fail:
            raise FactorioError("fake: factorio binary not found")
        self.started = True
        return [Endpoint("127.0.0.1", 27100 + i) for i in range(self.num_seats)]

    def stop(self) -> None:
        self.stopped = True
