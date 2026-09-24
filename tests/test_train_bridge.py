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
                assert set(accepted) == {"kind", "action", "observation"}
                observation = accepted["observation"]
                decisions += 1
            assert decisions == 30 * players
            assert set(observation["scores"]) == {str(seat) for seat in range(players)}
            assert all(score >= 0 for score in observation["scores"].values())
            if players == 1:
                score = observation["scores"]["0"]
                assert observation["utilities"]["0"] == score / (abs(score) + 1000)
            else:
                assert "utilities" not in observation
        bridge.close()


def test_parallel_environments_use_distinct_server_groups(monkeypatch):
    monkeypatch.setenv("COGAME_FACTORIO_SERVERS", ",".join(f"localhost:{port}" for port in range(27000, 27004)))
    ports = []

    def session(seat, _host, port, config):
        ports.append(port)
        return FakeSession(seat, config)

    bridge = Bridge(MANIFEST, "open_play", session_factory=session, env_index=1)
    bridge.reset({"players": 2, "seed": "second-environment"})
    bridge.close()
    assert ports == [27002, 27003]


def test_solo_negative_production_score_has_bounded_utility(monkeypatch):
    monkeypatch.setenv("COGAME_FACTORIO_SERVERS", "localhost:27000")

    class NegativeScoreSession(FakeSession):
        def score(self):
            return -50.0

    bridge = Bridge(MANIFEST, "solo", max_steps=1,
                    session_factory=lambda seat, _host, _port, config: NegativeScoreSession(seat, config))
    observation = bridge.reset({"players": 1, "seed": "negative-score"})
    result = bridge.step({"decision_id": observation["decision_id"], "response": '{"action":0}'})
    bridge.close()
    assert result["observation"]["scores"] == {"0": -50.0}
    assert result["observation"]["utilities"] == {"0": -50 / 1050}
