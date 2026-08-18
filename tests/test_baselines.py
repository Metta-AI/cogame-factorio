"""Baseline policies against a real Factorio server (``-m factorio``).

Skipped unless ``COGAME_FACTORIO_SERVERS=host:rcon_port[,host:port...]``
names a running FLE server (``fle cluster start -n 1`` -> ``localhost:27000``).
The first entry is used. Each baseline is driven for a handful of steps
straight through a ``FactorioInstance`` (no websocket) with FLE's
lab-play starting inventory, building the same observation dict the game
server sends, and the resulting production scores must order
``burner > handcraft > idle``.
"""

from __future__ import annotations

import os
import time
import warnings

import pytest

from players.burner_player import BurnerPolicy
from players.handcraft_player import HandcraftPolicy
from players.idle_player import IdlePolicy

pytestmark = pytest.mark.factorio

STEPS = int(os.environ.get("COGAME_BASELINE_STEPS", "10"))
PROGRAM_TIMEOUT = 60


def _server() -> tuple[str, int]:
    raw = os.environ.get("COGAME_FACTORIO_SERVERS", "")
    if not raw.strip():
        pytest.skip("COGAME_FACTORIO_SERVERS not set")
    host, _, port = raw.split(",")[0].strip().rpartition(":")
    return host or "localhost", int(port)


@pytest.fixture(scope="module")
def instance():
    warnings.filterwarnings("ignore")
    host, port = _server()
    from fle.env import FactorioInstance
    from fle.eval.tasks import LAB_PLAY_POPULATED_STARTING_INVENTORY

    inst = FactorioInstance(
        address=host, tcp_port=port, fast=True, num_agents=1,
        inventory=dict(LAB_PLAY_POPULATED_STARTING_INVENTORY))
    inst.rcon_client.send_command("/sc game.tick_paused = false")
    yield inst
    inst.cleanup()


def _observation(inst, step: int, last: dict | None, score: float) -> dict:
    """Mirror of the server's observation document (PROTOCOL.md)."""
    ns = inst.namespace
    ents = []
    for e in ns.get_entities():
        try:
            ents.append(e.model_dump(mode="json", exclude={"prototype"}))
        except Exception:  # noqa: BLE001 - never let a dump kill the test
            pass
    return {
        "raw_text": (last or {}).get("output", ""),
        "entities": ents,
        "inventory": dict(ns.inspect_inventory().model_dump()),
        "flows": {}, "score": score,
        "game_info": {"tick": inst.get_elapsed_ticks()},
        "task_verification": None,
        "last_program": last, "messages": [],
    }


def _run(inst, policy, steps: int) -> tuple[float, list[dict]]:
    inst.reset()
    inst.rcon_client.send_command("/sc game.tick_paused = false")
    score, last, log = 0.0, None, []
    for step in range(steps):
        obs = _observation(inst, step, last, score)
        code = policy.program(step, obs)
        assert isinstance(code, str) and code.strip()
        t0 = time.time()
        score, _goal, out = inst.eval(code, timeout=PROGRAM_TIMEOUT)
        error = "Error" in out or "Traceback" in out
        last = {"code": code, "output": out, "error": error}
        log.append({"step": step, "score": score, "wall": time.time() - t0,
                    "error": error, "output": out[:400]})
    return float(score), log


def _report(name, score, log):
    print(f"\n=== {name}: final score {score:.1f} after {len(log)} steps")
    for row in log:
        print(f"  step {row['step']:2d} score={row['score']:8.1f} "
              f"wall={row['wall']:5.1f}s error={row['error']} | "
              f"{row['output'][:160]!r}")


def test_baselines_score_in_order(instance):
    idle_score, idle_log = _run(instance, IdlePolicy(), STEPS)
    _report("idle", idle_score, idle_log)
    hand_score, hand_log = _run(instance, HandcraftPolicy(), STEPS)
    _report("handcraft", hand_score, hand_log)
    burner_score, burner_log = _run(instance, BurnerPolicy(), STEPS)
    _report("burner", burner_score, burner_log)

    # None of the scripted programs may abort on an exception path that
    # was not caught in-program (the whole program failing = "Error occurred").
    for name, log in (("handcraft", hand_log), ("burner", burner_log)):
        aborted = [r for r in log if "Error occurred" in r["output"]]
        assert not aborted, f"{name} program aborted: {aborted[:1]}"

    # FLE's score is production-stats relative to the reset baseline; a
    # pass-only seat can read a few points off zero, never real production.
    assert abs(idle_score) < 100
    assert hand_score > idle_score + 500
    assert burner_score > hand_score
