#!/usr/bin/env python3
"""Numeric decisions over the same FLE sessions used by the hosted game."""

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT)]

from cogame_factorio.config import GameConfig  # noqa: E402
from cogame_factorio.factorio import parse_servers_env  # noqa: E402
from cogame_factorio.session import FactorioSession  # noqa: E402
from players.burner_player import BurnerPolicy  # noqa: E402
from players.handcraft_player import HandcraftPolicy  # noqa: E402
from players.idle_player import IdlePolicy  # noqa: E402

ITEMS = ("coal", "iron-ore", "copper-ore", "stone", "iron-plate",
         "copper-plate", "iron-gear-wheel", "burner-mining-drill",
         "stone-furnace", "wooden-chest")
ENTITIES = ("burner-mining-drill", "stone-furnace", "wooden-chest",
            "transport-belt", "inserter", "electric-mining-drill")
ACTION_NAMES = ("idle", "handcraft", "burner", "wait")


class Bridge:
    def __init__(self, manifest: Path, variant: str, max_steps: int | None = None,
                 session_factory=FactorioSession) -> None:
        document = json.loads(manifest.read_text())
        config = next(row["game_config"] for row in document["variants"] if row["id"] == variant)
        self.config = GameConfig.from_dict({**config, "tokens": [
            f"training-{seat}" for seat in range(config["num_agents"])]})
        self.max_steps = self.config.max_steps if max_steps is None else max_steps
        assert 1 <= self.max_steps <= self.config.max_steps
        self.session_factory = session_factory
        self.sessions = []
        self.policies = []
        self.observed = []
        self.step_count = 0
        self.seat = 0
        self.decision_id = 0

    def reset(self, request: dict) -> dict:
        assert request["players"] == self.config.num_seats
        self.close()
        endpoints = parse_servers_env(os.environ["COGAME_FACTORIO_SERVERS"])
        assert len(endpoints) >= self.config.num_seats
        self.sessions = [self.session_factory(seat, endpoints[seat].host,
                         endpoints[seat].rcon_port, self.config)
                         for seat in range(self.config.num_seats)]
        for session in self.sessions:
            session.start()
        self.policies = [
            (IdlePolicy(), HandcraftPolicy(sleep_seconds=2),
             BurnerPolicy(sleep_seconds=2))
            for _ in self.sessions
        ]
        self.observed = [session.observe(None) for session in self.sessions]
        self.step_count = 0
        self.seat = 0
        self.decision_id = 0
        return self.current()

    def current(self) -> dict:
        observation = self.observed[self.seat].observation
        return {
            "kind": "decision", "game": "factorio", "decision_id": self.decision_id,
            "seat": self.seat, "engine_seat": self.seat, "turn": self.step_count,
            "semantic_view": observation, "inbox": [],
            "messages": [{"role": "user", "content": json.dumps({
                "step": self.step_count, "score": observation["score"],
                "inventory": observation["inventory"]})}],
            "speech_messages": [],
            "action_schema": {"type": "object", "properties": {
                "action": {"enum": list(range(len(ACTION_NAMES)))}}, "required": ["action"]},
            "typed_question": None,
        }

    def encode(self) -> dict:
        observation = self.observed[self.seat].observation
        inventory = observation["inventory"]
        flows = observation["flows"]
        counts = Counter(entity["name"] for entity in observation["entities"])
        values = [float(self.step_count), float(observation["score"]),
                  float(observation["game_info"]["tick"]),
                  float(bool(observation["last_program"] and
                             observation["last_program"]["error"]))]
        values.extend(float(inventory.get(item, 0)) for item in ITEMS)
        for kind in ("input", "output", "harvested", "crafted"):
            values.extend(float(flows[kind].get(item, 0)) for item in ITEMS[:7])
        values.extend(float(counts[name]) for name in ENTITIES)
        return {"decision_id": self.decision_id, "values": values,
                "actions": [{"action": i} for i in range(len(ACTION_NAMES))]}

    def teacher(self) -> dict:
        return {"response": json.dumps({"action": 2})}

    def step(self, request: dict) -> dict:
        assert request["decision_id"] == self.decision_id
        action = json.loads(request["response"])
        assert set(action) == {"action"} and action["action"] in range(len(ACTION_NAMES))
        observed = self.observed[self.seat].observation
        program = ("sleep(30)" if action["action"] == 3 else
                   self.policies[self.seat][action["action"]].program(self.step_count, observed))
        result = self.sessions[self.seat].run_program(program)
        self.observed[self.seat] = self.sessions[self.seat].observe({
            "code": program, "output": result.output, "error": result.error})
        self.decision_id += 1
        self.seat += 1
        if self.seat == self.config.num_seats:
            self.seat = 0
            self.step_count += 1
        if self.step_count == self.max_steps:
            scores = {}
            for seat, session in enumerate(self.sessions):
                throughput = session.throughput() if session.is_throughput_task() else None
                scores[str(seat)] = session.score() if throughput is None else throughput
            next_observation = {"kind": "terminal", "scores": scores}
            if self.config.num_seats == 1:
                assert scores["0"] >= 0
                next_observation["utilities"] = {
                    "0": 2 * scores["0"] / (scores["0"] + 1000) - 1}
        else:
            next_observation = self.current()
        return {"kind": "accepted", "action": action, "program": program,
                "observation": next_observation}

    def close(self) -> None:
        for session in self.sessions:
            session.close()
        self.sessions = []


if __name__ == "__main__":
    assert len(sys.argv) in (3, 4), "usage: train_bridge.py MANIFEST VARIANT [MAX_STEPS]"
    bridge = Bridge(Path(sys.argv[1]).resolve(), sys.argv[2],
                    int(sys.argv[3]) if len(sys.argv) == 4 else None)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            response = {
                "reset": bridge.reset,
                "encode": lambda _request: bridge.encode(),
                "teacher": lambda _request: bridge.teacher(),
                "step": bridge.step,
            }[request["kind"]](request)
            print(json.dumps(response, separators=(",", ":")), flush=True)
    finally:
        bridge.close()
