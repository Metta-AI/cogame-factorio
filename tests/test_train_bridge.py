"""Complete certified games through the training boundary."""

import json
import random
from pathlib import Path

from tests.fakes import FakeSession
from tools.train_bridge import Bridge

MANIFEST = Path(__file__).resolve().parents[1] / "coworld_manifest_template.json"


def test_all_variants_with_production_policy_programs(monkeypatch):
    monkeypatch.setenv("COGAME_FACTORIO_SERVERS", "localhost:27000,localhost:27001")
    for variant, players in (("open_play", 2), ("iron_plate_throughput", 2), ("solo", 1)):
        bridge = Bridge(MANIFEST, variant,
                        session_factory=lambda seat, _host, _port, config: FakeSession(seat, config))
        for policy in ("teacher", "random"):
            observation = bridge.reset({"players": players, "seed": f"{variant}-{policy}"})
            rng = random.Random(42)
            decisions = 0
            while observation["kind"] == "decision":
                encoded = bridge.encode()
                assert encoded["decision_id"] == observation["decision_id"]
                assert len(encoded["values"]) == 48
                assert encoded["actions"] == [{"action": i} for i in range(4)]
                assert observation["semantic_view"] == bridge.observed[bridge.seat].observation
                action = (json.loads(bridge.teacher()["response"]) if policy == "teacher"
                          else rng.choice(encoded["actions"]))
                accepted = bridge.step({"decision_id": observation["decision_id"],
                                        "response": json.dumps(action)})
                assert accepted["kind"] == "accepted"
                observation = accepted["observation"]
                decisions += 1
            assert decisions == 30 * players
            assert set(observation["scores"]) == {str(seat) for seat in range(players)}
            assert all(score >= 0 for score in observation["scores"].values())
            if players == 1:
                assert observation["utilities"]["0"] == (
                    2 * observation["scores"]["0"] / (observation["scores"]["0"] + 1000) - 1)
            else:
                assert "utilities" not in observation
        bridge.close()
