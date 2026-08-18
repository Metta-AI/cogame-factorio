"""Live tests against real Factorio servers (``-m factorio``).

Needs ``COGAME_FACTORIO_SERVERS=host:port,host:port`` (e.g. ``fle cluster
start -n 2`` -> ``localhost:27000,localhost:27001``). Runs a full 2-seat,
3-step episode through the real GameServer + FactorioSession with a
scripted stand-in client, validates results against the manifest
results_schema and the replay against docs/REPLAY.md, and refreshes
``tests/fixtures/sample_replay.json`` (the viewer's real fixture).
"""

import asyncio
import json
import os
from pathlib import Path

import aiohttp
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestServer

from cogame_factorio.factorio import FactorioServerManager
from cogame_factorio.replay import STEP_KEYS, Replay
from cogame_factorio.server import GameServer
from cogame_factorio.session import FactorioSession

from tests.conftest import make_config

pytestmark = pytest.mark.factorio

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((REPO_ROOT / "coworld_manifest_template.json").read_text())
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample_replay.json"

SEAT0_PROGRAMS = [
    # step 0: drill on iron ore feeding a chest, fuelled
    "pos = nearest(Resource.IronOre)\n"
    "move_to(pos)\n"
    "drill = place_entity(Prototype.BurnerMiningDrill, position=pos, "
    "direction=Direction.DOWN)\n"
    "chest = place_entity_next_to(Prototype.IronChest, "
    "reference_position=drill.drop_position, direction=Direction.DOWN)\n"
    "insert_item(Prototype.Coal, drill, quantity=20)\n"
    "print(drill)\n",
    # step 1: a stone furnace next to a coal drill + a belt run + a pole
    "cpos = nearest(Resource.Coal)\n"
    "move_to(cpos)\n"
    "cdrill = place_entity(Prototype.BurnerMiningDrill, position=cpos, "
    "direction=Direction.UP)\n"
    "furnace = place_entity_next_to(Prototype.StoneFurnace, "
    "reference_position=cdrill.drop_position, direction=Direction.UP)\n"
    "insert_item(Prototype.Coal, cdrill, quantity=10)\n"
    "belt = connect_entities(Position(x=cpos.x+4, y=cpos.y), "
    "Position(x=cpos.x+9, y=cpos.y), Prototype.TransportBelt)\n"
    "move_to(Position(x=cpos.x-3, y=cpos.y-3))\n"
    "pole = place_entity(Prototype.MediumElectricPole, "
    "position=Position(x=cpos.x-3, y=cpos.y-3))\n"
    "sleep(5)\n"
    "print(inspect_inventory())\n",
    # step 2: an error program (game outcome, not a strike)
    "x = 1 / 0\n",
]
SEAT1_PROGRAMS = [
    "pos = nearest(Resource.Stone)\nmove_to(pos)\n"
    "d = place_entity(Prototype.BurnerMiningDrill, position=pos, "
    "direction=Direction.RIGHT)\ninsert_item(Prototype.Coal, d, quantity=5)\n"
    "print(get_entities())\n",
    "sleep(3)\nprint(inspect_inventory())\n",
    "print(score())\n",
]


async def play(url, programs):
    welcome, observations, result = None, [], None
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(url, max_msg_size=64 * 1024 * 1024) as ws:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data["type"] == "welcome":
                    welcome = data
                elif data["type"] == "observation":
                    observations.append(data)
                    code = programs[data["step"]]
                    await ws.send_str(json.dumps(
                        {"type": "program", "step": data["step"], "code": code}))
                elif data["type"] == "done":
                    result = data["result"]
                    break
    return welcome, observations, result


async def test_live_two_seat_episode(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    servers = os.environ["COGAME_FACTORIO_SERVERS"]
    cfg = make_config(num_seats=2, max_steps=3, step_deadline_seconds=120,
                      program_timeout_seconds=45,
                      player_connect_timeout_seconds=60,
                      wall_clock_budget_seconds=900)
    results_path = tmp_path / "results.json"
    replay_path = tmp_path / "replay.json"
    server = GameServer(
        cfg, results_uri=f"file://{results_path}",
        save_replay_uri=f"file://{replay_path}",
        player_failure_uri=f"file://{tmp_path}/failure.json",
        factorio_manager=FactorioServerManager(2, servers_env=servers))
    ts = TestServer(server.make_app())
    await ts.start_server()
    try:
        episode = asyncio.create_task(server.run_episode())
        (w0, obs0, r0), (w1, obs1, r1) = await asyncio.gather(
            play(str(ts.make_url("/player?slot=0&token=token-0")), SEAT0_PROGRAMS),
            play(str(ts.make_url("/player?slot=1&token=token-1")), SEAT1_PROGRAMS))
        result = await asyncio.wait_for(episode, 600)
    finally:
        await ts.close()

    # welcome carries FLE's api docs and the task
    assert w0["protocol"] == "cogame.factorio.v1"
    assert len(w0["api_docs"]) > 10_000
    assert w0["task"]["key"] == "open_play" and w0["task"]["goal_description"]
    assert w0["episode"]["starting_inventory"]["burner-mining-drill"] == 50
    assert isinstance(w0["episode"]["map_bounds"]["x0"], int)

    # observations
    assert [o["step"] for o in obs0] == [0, 1, 2]
    o1 = obs0[1]["observation"]
    assert o1["last_program"]["error"] is False, o1["last_program"]["output"]
    assert any(e.get("name") == "burner-mining-drill" for e in o1["entities"])
    assert o1["inventory"]["coal"] < 500
    assert o1["game_info"]["tick"] > 0 and o1["game_info"]["speed"] == 10.0
    assert set(o1["flows"]) >= {"input", "output", "harvested", "crafted"}
    assert "burner-mining-drill" in o1["raw_text"]
    o2 = obs0[2]["observation"]
    belt_groups = [e for e in o2["entities"] if isinstance(e.get("belts"), list)]
    assert belt_groups, "expected a BeltGroup in the observation"

    # results: schema-valid, closed key set
    results = json.loads(results_path.read_text())
    assert results == r0 == r1
    jsonschema.validate(results, MANIFEST["game"]["results_schema"])
    assert results["end_reason"] == "steps_cap"
    assert results["steps_completed"] == [3, 3]
    assert results["error_steps"] == [1, 0]
    assert results["noop_steps"] == [0, 0]
    assert results["dead_seats"] == [False, False]
    assert results["throughputs"] == [None, None]
    assert all(t > 0 for t in results["final_ticks"])
    assert results["scores"][0] > results["scores"][1] - 1e-9

    # replay: REPLAY.md conformance with real content
    replay = Replay.parse(replay_path.read_bytes())
    doc = replay.doc
    assert doc["map"]["resources"] and doc["map"]["water"]
    assert {r[0] for r in doc["map"]["resources"]} >= {
        "iron-ore", "copper-ore", "coal", "stone", "crude-oil"}
    assert all(isinstance(r[1], int) and isinstance(r[3], int)
               for r in doc["map"]["resources"])
    assert doc["result"] == results
    s0 = doc["seats"][0]
    assert [s["step"] for s in s0["steps"]] == [0, 1, 2]
    for step in s0["steps"]:
        assert set(step) == set(STEP_KEYS)
    step1 = s0["steps"][1]
    names = {row[0] for row in step1["entities"]}
    assert {"burner-mining-drill", "stone-furnace", "iron-chest",
            "medium-electric-pole"} <= names, names
    assert step1["belts"], "belt tiles should be flattened into belts"
    assert all(len(b) == 3 for b in step1["belts"])
    assert step1["character"]["x"] != 0.0 or step1["character"]["y"] != 0.0
    assert step1["tick"] > s0["steps"][0]["tick"]
    assert step1["wall_ms"] > 0
    assert s0["steps"][2]["error"] is True and "ZeroDivisionError" in \
        s0["steps"][2]["output"]
    assert "transport-belt" not in names

    # refresh the viewer's real fixture (kept small)
    data = replay_path.read_bytes()
    assert len(data) < 1_000_000, len(data)
    FIXTURE.parent.mkdir(exist_ok=True)
    FIXTURE.write_bytes(data)
    assert result.end_reason == "steps_cap"


async def test_live_session_program_semantics():
    """Direct FactorioSession checks: error detection, timeout, terrain."""
    servers = os.environ["COGAME_FACTORIO_SERVERS"].split(",")
    host, port = servers[0].rsplit(":", 1)
    cfg = make_config(num_seats=1, program_timeout_seconds=2)
    session = FactorioSession(0, host, int(port), cfg)
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, session.start)
        ok = await loop.run_in_executor(None, session.run_program, "print(1+1)")
        assert not ok.error and "2" in ok.output
        err = await loop.run_in_executor(None, session.run_program, "1/0")
        assert err.error and "ZeroDivisionError" in err.output
        syn = await loop.run_in_executor(None, session.run_program, "def f(:")
        assert syn.error
        slow = await loop.run_in_executor(None, session.run_program,
                                          "sleep(600)")
        assert slow.error and "timed out" in slow.output
        assert slow.wall_ms < 5000
        terrain = await loop.run_in_executor(None, session.capture_terrain)
        assert terrain["resources"] and terrain["bounds"]["x1"] == 128
        assert session.throughput() is None
        assert session.starting_inventory()["coal"] == 500
    finally:
        session.close()


async def test_live_throughput_task_end_of_seat_verification(tmp_path):
    """iron_plate_throughput: FLE's holdout verification runs once when
    the seat finishes; results carry throughput as the score."""
    servers = os.environ["COGAME_FACTORIO_SERVERS"]
    cfg = make_config(num_seats=1, max_steps=1, task="iron_plate_throughput",
                      step_deadline_seconds=120, player_connect_timeout_seconds=60,
                      wall_clock_budget_seconds=600)
    results_path = tmp_path / "results.json"
    replay_path = tmp_path / "replay.json"
    server = GameServer(
        cfg, results_uri=f"file://{results_path}",
        save_replay_uri=f"file://{replay_path}",
        factorio_manager=FactorioServerManager(1, servers_env=servers))
    ts = TestServer(server.make_app())
    await ts.start_server()
    try:
        episode = asyncio.create_task(server.run_episode())
        w, obs, r = await play(
            str(ts.make_url("/player?slot=0&token=token-0")),
            ["pos = nearest(Resource.IronOre)\nmove_to(pos)\n"
             "d = place_entity(Prototype.BurnerMiningDrill, position=pos, "
             "direction=Direction.DOWN)\n"
             "f = place_entity(Prototype.StoneFurnace, position=d.drop_position)\n"
             "insert_item(Prototype.Coal, d, quantity=20)\n"
             "insert_item(Prototype.Coal, f, quantity=20)\n"])
        await asyncio.wait_for(episode, 600)
    finally:
        await ts.close()
    assert w["task"]["key"] == "iron_plate_throughput"
    assert "16 iron-plate" in w["task"]["goal_description"]
    assert obs[0]["observation"]["task_verification"] == {
        "success": False, "meta": {"throughput": 0.0}}
    results = json.loads(results_path.read_text())
    assert results["task_key"] == "iron_plate_throughput"
    assert results["throughputs"][0] is not None
    assert results["scores"] == results["throughputs"]
    # a fuelled burner drill dropping into a fuelled furnace makes plates
    assert results["throughputs"][0] > 0.0
    assert results["production_scores"][0] != results["throughputs"][0]
    replay = Replay.parse(replay_path.read_bytes())
    assert replay.seats[0]["steps"][-1]["throughput"] == results["throughputs"][0]
