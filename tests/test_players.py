"""Offline tests for the player harness (players/client.py) and baselines.

A minimal in-process aiohttp fake server speaks the ``cogame.factorio.v1``
protocol (welcome -> observation -> program -> done) so the harness can be
exercised end to end without Factorio: full episodes, reconnection with the
server re-sending welcome + current observation, 403 fatal / 409 retry,
malformed messages, policy failures answered with ``pass``, deadline
overruns, telemetry zips, and the exit-code contract. Baseline policies are
checked offline for producing syntactically valid programs on synthetic
observations; their behaviour against a real Factorio server lives in
``tests/test_baselines.py`` (``-m factorio``).
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import zipfile

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from players import client, fle_helpers as H, llm_player
from players.burner_player import BurnerPolicy
from players.client import (FunctionPolicy, PlayerError, Policy, Telemetry,
                            play_episode, run_policy_main)
from players.handcraft_player import HandcraftPolicy
from players.idle_player import IdlePolicy

# -- fake server ---------------------------------------------------------------

def welcome_msg(**over):
    msg = {
        "type": "welcome", "protocol": "cogame.factorio.v1",
        "game_version": "GV1", "slot": 0, "name": "P0",
        "task": {"key": "open_play", "goal_description": "build",
                 "agent_instructions": None},
        "max_steps": 3, "step_deadline_seconds": 5,
        "program_timeout_seconds": 4, "api_docs": "API DOCS",
        "episode": {"max_steps": 3, "step_deadline_seconds": 5,
                    "program_timeout_seconds": 4, "strike_limit": 3,
                    "seats": 1, "slot": 0, "map_bounds": None,
                    "starting_inventory": {"burner-mining-drill": 50,
                                           "stone-furnace": 10,
                                           "wooden-chest": 10, "coal": 500},
                    "fast": True, "game_speed": 10},
    }
    msg.update(over)
    return msg


def obs_msg(step, deadline=5, **obs):
    observation = {
        "raw_text": f"step {step}", "entities": [], "inventory": {"coal": 500},
        "flows": {}, "score": float(step), "game_info": {"tick": 60 * step},
        "task_verification": None,
        "last_program": None if step == 0 else
        {"code": "x", "output": f"out{step - 1}", "error": False},
        "messages": [],
    }
    observation.update(obs)
    return {"type": "observation", "step": step, "deadline_seconds": deadline,
            "observation": observation}


class FakeSeat:
    """Scriptable seat endpoint. ``script`` is a list of per-connection
    lists of actions: ("send", dict|str), ("recv", n), ("close",)."""

    def __init__(self, script):
        self.script = script
        self.connections = 0
        self.received: list[dict] = []
        self.raw_received: list[str] = []

    async def handler(self, request):
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        idx = self.connections
        self.connections += 1
        actions = self.script[min(idx, len(self.script) - 1)]
        for action in actions:
            kind = action[0]
            if kind == "send":
                payload = action[1]
                await ws.send_str(payload if isinstance(payload, str)
                                  else json.dumps(payload))
            elif kind == "recv":
                for _ in range(action[1]):
                    msg = await ws.receive()
                    if msg.type != WSMsgType.TEXT:
                        return ws
                    self.raw_received.append(msg.data)
                    try:
                        self.received.append(json.loads(msg.data))
                    except json.JSONDecodeError:
                        pass
            elif kind == "close":
                await ws.close()
                return ws
        await ws.close()
        return ws


async def serve(handler):
    app = web.Application()
    app.router.add_get("/player", handler)
    server = TestServer(app)
    await server.start_server()
    return server


DONE = {"type": "done", "result": {"scores": [12.5], "end_reason": "steps_cap"}}


def full_episode_script(steps=3):
    actions = [("send", welcome_msg())]
    for k in range(steps):
        actions += [("send", obs_msg(k)), ("recv", 1)]
    actions += [("send", DONE)]
    return [actions]


class RecordingPolicy(Policy):
    def __init__(self, code="print(1)"):
        self.code = code
        self.welcomes = []
        self.steps = []
        self.done = None

    def on_welcome(self, welcome):
        self.welcomes.append(welcome)

    def program(self, step, observation):
        self.steps.append((step, observation))
        return self.code

    def on_done(self, result):
        self.done = result


# -- full episodes -------------------------------------------------------------

async def test_full_episode_replies_program_per_step_and_returns_result():
    fake = FakeSeat(full_episode_script())
    server = await serve(fake.handler)
    try:
        policy = RecordingPolicy()
        result = await play_episode(policy, str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert policy.done == DONE["result"]
    assert len(policy.welcomes) == 1 and policy.welcomes[0]["api_docs"] == "API DOCS"
    assert [s for s, _ in policy.steps] == [0, 1, 2]
    assert policy.steps[1][1]["last_program"]["output"] == "out0"
    assert fake.received == [
        {"type": "program", "step": k, "code": "print(1)"} for k in range(3)]


async def test_baselines_complete_a_fake_episode():
    for factory in (IdlePolicy, HandcraftPolicy, BurnerPolicy):
        fake = FakeSeat(full_episode_script())
        server = await serve(fake.handler)
        try:
            result = await play_episode(factory(), str(server.make_url("/player")),
                                        reconnect_delay_seconds=0.01)
        finally:
            await server.close()
        assert result == DONE["result"]
        assert len(fake.received) == 3
        for m in fake.received:
            ast.parse(m["code"])  # every reply is valid Python


# -- reconnects ----------------------------------------------------------------

async def test_reconnect_after_midgame_drop_resumes_at_current_step():
    """The server re-sends welcome + the current observation on reconnect."""
    fake = FakeSeat([
        [("send", welcome_msg()), ("send", obs_msg(0)), ("recv", 1),
         ("send", obs_msg(1)), ("recv", 1), ("close",)],
        [("send", welcome_msg()), ("send", obs_msg(2, deadline=3)), ("recv", 1),
         ("send", DONE)],
    ])
    server = await serve(fake.handler)
    try:
        policy = RecordingPolicy()
        result = await play_episode(policy, str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert fake.connections == 2
    assert len(policy.welcomes) == 2
    assert [m["step"] for m in fake.received] == [0, 1, 2]


async def test_gives_up_after_bounded_reconnects_without_progress():
    fake = FakeSeat([[("send", welcome_msg()), ("close",)]])
    server = await serve(fake.handler)
    try:
        with pytest.raises(PlayerError, match="giving up"):
            await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                               max_connect_attempts=3, reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert fake.connections == 3


async def test_reconnect_budget_resets_after_progress():
    """Each connection answers one step then drops: never exhausts the budget."""
    script = [[("send", welcome_msg()), ("send", obs_msg(k)), ("recv", 1), ("close",)]
              for k in range(6)]
    script.append([("send", welcome_msg()), ("send", obs_msg(6)), ("recv", 1),
                   ("send", DONE)])
    fake = FakeSeat(script)
    server = await serve(fake.handler)
    try:
        result = await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                                    max_connect_attempts=2,
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert fake.connections == 7


async def test_unreachable_server_gives_up():
    with pytest.raises(PlayerError, match="giving up"):
        await play_episode(RecordingPolicy(), "http://127.0.0.1:9/player",
                           max_connect_attempts=2, reconnect_delay_seconds=0.01)


async def test_server_gone_after_connect_exits_cleanly():
    """After the seat has connected once, connection refused on reconnect
    means the server finished: return {} promptly instead of retrying."""
    holder = {}

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps(welcome_msg()))
        await ws.send_str(json.dumps(obs_msg(0)))
        await ws.receive()
        await ws.close()
        # The server goes away right after the drop: the reconnect is refused.
        asyncio.ensure_future(holder["server"].close())
        return ws
    server = await serve(handler)
    holder["server"] = server
    policy = RecordingPolicy()
    result = await asyncio.wait_for(
        play_episode(policy, str(server.make_url("/player")),
                     max_connect_attempts=50, reconnect_delay_seconds=0.05),
        timeout=15)
    assert result == {}
    assert [s for s, _ in policy.steps] == [0]


# -- handshake errors ----------------------------------------------------------

async def test_403_is_fatal_before_first_connection():
    async def forbid(request):
        raise web.HTTPForbidden(text="bad token")
    server = await serve(forbid)
    try:
        with pytest.raises(PlayerError, match="403"):
            await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                               reconnect_delay_seconds=0.01)
    finally:
        await server.close()


async def test_403_after_connect_means_server_gone_exit_0():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise web.HTTPForbidden(text="episode over")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps(welcome_msg()))
        await ws.send_str(json.dumps(obs_msg(0)))
        await ws.receive()
        await ws.close()
        return ws
    server = await serve(handler)
    try:
        result = await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == {}
    assert calls == 2


async def test_409_is_retried_then_succeeds():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise web.HTTPConflict(text="slot busy")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps(welcome_msg()))
        await ws.send_str(json.dumps(obs_msg(0)))
        await ws.receive()
        await ws.send_str(json.dumps(DONE))
        await ws.close()
        return ws
    server = await serve(handler)
    try:
        result = await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert calls == 3


async def test_409_forever_exhausts_budget():
    calls = 0

    async def conflict(request):
        nonlocal calls
        calls += 1
        raise web.HTTPConflict(text="slot busy")
    server = await serve(conflict)
    try:
        with pytest.raises(PlayerError, match="giving up"):
            await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                               max_connect_attempts=3, reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert calls == 3


# -- malformed input and policy failures ---------------------------------------

async def test_malformed_messages_are_ignored_not_fatal():
    fake = FakeSeat([[
        ("send", "not json"),
        ("send", "[1, 2, 3]"),
        ("send", {"type": "mystery"}),
        ("send", welcome_msg()),
        ("send", {"type": "observation", "observation": {}}),          # no step
        ("send", {"type": "observation", "step": "0", "observation": {}}),  # bad step
        ("send", {"type": "observation", "step": 0, "observation": "nope"}),
        ("recv", 1),
        ("send", {"type": "done", "result": "weird"}),
    ]])
    server = await serve(fake.handler)
    try:
        policy = RecordingPolicy()
        result = await play_episode(policy, str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == {}
    assert policy.steps == [(0, {})]
    assert fake.received == [{"type": "program", "step": 0, "code": "print(1)"}]


async def test_policy_exception_and_bad_return_answer_pass():
    def fn(step, observation):
        if step == 0:
            raise RuntimeError("boom")
        if step == 1:
            return 42
        return "x" * (client.MAX_CODE_CHARS + 1)
    fake = FakeSeat(full_episode_script())
    server = await serve(fake.handler)
    try:
        result = await play_episode(FunctionPolicy(fn), str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert [m["code"] for m in fake.received] == ["pass", "pass", "pass"]


async def test_policy_overrun_answers_pass_before_deadline():
    def slow(step, observation):
        import time
        time.sleep(3)
        return "print('late')"
    fake = FakeSeat([[("send", welcome_msg()),
                      ("send", obs_msg(0, deadline=1.5)), ("recv", 1),
                      ("send", DONE)]])
    server = await serve(fake.handler)
    try:
        result = await asyncio.wait_for(
            play_episode(FunctionPolicy(slow), str(server.make_url("/player")),
                         deadline_margin_seconds=0.5, reconnect_delay_seconds=0.01),
            timeout=10)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert fake.received == [{"type": "program", "step": 0, "code": "pass"}]


async def test_hook_exceptions_are_ignored():
    class Bad(RecordingPolicy):
        def on_welcome(self, welcome):
            raise ValueError("welcome")

        def on_done(self, result):
            raise ValueError("done")
    fake = FakeSeat(full_episode_script(1))
    server = await serve(fake.handler)
    try:
        result = await play_episode(Bad(), str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01)
    finally:
        await server.close()
    assert result == DONE["result"]


# -- telemetry -----------------------------------------------------------------

async def test_telemetry_zip_written_to_file_url(tmp_path):
    fake = FakeSeat(full_episode_script())
    server = await serve(fake.handler)
    out = tmp_path / "nested" / "artifact.zip"
    tel = Telemetry(out.as_uri(), "players.test")
    try:
        await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                           reconnect_delay_seconds=0.01, telemetry=tel)
    finally:
        await server.close()
    assert out.exists()
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as zf:
        names = set(zf.namelist())
        assert names == {"meta.json", "events.jsonl", "summary.json"}
        meta = json.loads(zf.read("meta.json"))
        events = [json.loads(l) for l in zf.read("events.jsonl").decode().splitlines()]
        summary = json.loads(zf.read("summary.json"))
    assert meta["slot"] == 0 and meta["policy_module"] == "players.test"
    assert meta["game_version"] == "GV1"
    assert meta["episode"]["max_steps"] == 3
    assert [e["step"] for e in events] == [0, 1, 2]
    assert events[0]["output"] == "out0" and events[0]["code"] == "print(1)"
    assert events[0]["score"] == 1.0 and "wall_ms" in events[0]
    assert summary["steps_answered"] == 3
    assert summary["result"] == DONE["result"]


async def test_telemetry_failure_never_breaks_play(tmp_path):
    fake = FakeSeat(full_episode_script(1))
    server = await serve(fake.handler)
    tel = Telemetry("ftp://nowhere/x.zip", "players.test")
    try:
        result = await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                                    reconnect_delay_seconds=0.01, telemetry=tel)
    finally:
        await server.close()
    assert result == DONE["result"]
    assert tel.enabled is False and tel.uploaded is False


async def test_telemetry_http_put(tmp_path):
    fake = FakeSeat(full_episode_script(1))
    uploads = []

    async def put(request):
        uploads.append(await request.read())
        return web.Response(status=200)
    app = web.Application()
    app.router.add_get("/player", fake.handler)
    app.router.add_put("/upload", put)
    server = TestServer(app)
    await server.start_server()
    tel = Telemetry(str(server.make_url("/upload")), "players.test")
    try:
        await play_episode(RecordingPolicy(), str(server.make_url("/player")),
                           reconnect_delay_seconds=0.01, telemetry=tel)
    finally:
        await server.close()
    assert len(uploads) == 1 and tel.uploaded
    with zipfile.ZipFile(io.BytesIO(uploads[0])) as zf:
        assert "events.jsonl" in zf.namelist()


def test_telemetry_disabled_without_env(monkeypatch):
    monkeypatch.delenv(client.ARTIFACT_URL_ENV_VAR, raising=False)
    tel = Telemetry(None, "x")
    assert tel.enabled is False
    tel.on_welcome({}); tel.on_observation(0, {}); tel.on_program(0, "pass", False)
    assert asyncio.run(tel.upload()) is False


# -- env plumbing + exit codes -------------------------------------------------

def test_ws_url_env_precedence(monkeypatch):
    monkeypatch.delenv("COWORLD_PLAYER_WS_URL", raising=False)
    monkeypatch.delenv("COGAMES_ENGINE_WS_URL", raising=False)
    with pytest.raises(PlayerError, match="COWORLD_PLAYER_WS_URL"):
        client.ws_url_from_env()
    monkeypatch.setenv("COGAMES_ENGINE_WS_URL", "ws://b/player")
    assert client.ws_url_from_env() == "ws://b/player"
    monkeypatch.setenv("COWORLD_PLAYER_WS_URL", "ws://a/player")
    assert client.ws_url_from_env() == "ws://a/player"


def test_run_policy_main_exit_codes(monkeypatch):
    monkeypatch.delenv("COWORLD_PLAYER_WS_URL", raising=False)
    monkeypatch.delenv("COGAMES_ENGINE_WS_URL", raising=False)
    monkeypatch.delenv(client.ARTIFACT_URL_ENV_VAR, raising=False)
    # no URL -> PlayerError -> 1
    assert run_policy_main(IdlePolicy) == 1

    def boom():
        raise KeyboardInterrupt
    assert run_policy_main(boom) == 130

    async def _ok():
        fake = FakeSeat(full_episode_script(1))
        server = await serve(fake.handler)
        return server, str(server.make_url("/player"))
    # run a real episode through run_policy_main -> 0
    loop = asyncio.new_event_loop()
    server, url = loop.run_until_complete(_ok())
    monkeypatch.setenv("COWORLD_PLAYER_WS_URL", url)
    try:
        # the server lives on `loop`; drive it in a thread while main runs
        import threading
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        assert run_policy_main(IdlePolicy) == 0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.run_until_complete(server.close())
        loop.close()


def test_baseline_modules_import_contract_constants():
    assert client.PROTOCOL == "cogame.factorio.v1"
    assert client.MSG_PROGRAM == "program"
    assert client.WS_URL_ENV_VARS[0] == "COWORLD_PLAYER_WS_URL"


# -- baselines offline: valid programs on synthetic observations ---------------

def _synthetic_obs(step, drills=0, furnaces=0, plates=0, coal=500):
    ents = []
    for i in range(drills):
        ents.append({"name": "burner-mining-drill", "position": {"x": 16 + 2 * i, "y": 71},
                     "status": "working" if i % 2 else "no_fuel",
                     "fuel": {"coal": 3}, "direction": 4})
    for i in range(furnaces):
        ents.append({"name": "stone-furnace", "position": {"x": 17 + 2 * i, "y": 73},
                     "status": "working", "fuel": {"coal": 5}})
    return {"raw_text": "", "entities": ents, "score": 1.0 * step,
            "inventory": {"burner-mining-drill": 50 - drills,
                          "stone-furnace": 10 - furnaces, "wooden-chest": 10,
                          "coal": coal, "iron-plate": plates},
            "last_program": None if step == 0 else
            {"code": "pass", "output": "Error occurred:\n  Line 1: boom", "error": True}}


@pytest.mark.parametrize("factory", [IdlePolicy, HandcraftPolicy, BurnerPolicy])
def test_baselines_emit_valid_python_for_many_observations(factory):
    policy = factory()
    policy.on_welcome(welcome_msg())
    scenarios = [_synthetic_obs(0)] + [
        _synthetic_obs(s, drills=min(s * 2, 12), furnaces=min(s, 9),
                       plates=s * 30, coal=max(0, 500 - 60 * s))
        for s in range(1, 30)]
    # also degenerate observations: empty dict, junk fields
    scenarios += [{}, {"entities": "junk", "inventory": None, "score": "x"},
                  {"entities": [{"name": None}, 5], "inventory": {"coal": "many"}}]
    for step, obs in enumerate(scenarios):
        code = policy.program(step, obs)
        assert isinstance(code, str) and code.strip()
        ast.parse(code)
        assert len(code) < client.MAX_CODE_CHARS


def test_burner_phase_progression_from_observation():
    p = BurnerPolicy()
    p.on_welcome(welcome_msg())
    assert "Resource.IronOre" in p.program(0, _synthetic_obs(0))
    assert "Resource.CopperOre" in p.program(1, _synthetic_obs(1, drills=5, furnaces=5))
    assert "Resource.Coal" in p.program(2, _synthetic_obs(2, drills=8, furnaces=8))
    assert "Resource.Stone" in p.program(3, _synthetic_obs(3, drills=10, furnaces=8))
    maint = p.program(4, _synthetic_obs(4, drills=11, furnaces=8, plates=100, coal=20))
    assert "extract_item" in maint and "IronGearWheel" in maint
    assert "WoodenChest" in maint  # coal restock when inventory coal is low


def test_burner_abandons_phase_that_adds_no_drills():
    p = BurnerPolicy()
    p.on_welcome(welcome_msg())
    assert "IronOre" in p.program(0, _synthetic_obs(0))
    # nothing got placed -> iron is abandoned, copper is next
    assert "CopperOre" in p.program(1, _synthetic_obs(1))
    assert "Coal" in p.program(2, _synthetic_obs(2))
    assert "Stone" in p.program(3, _synthetic_obs(3))
    assert "get_entities" in p.program(4, _synthetic_obs(4))  # maintenance


def test_burner_stops_placing_without_drills_in_inventory():
    p = BurnerPolicy()
    obs = _synthetic_obs(0)
    obs["inventory"]["burner-mining-drill"] = 0
    assert "place_entity" not in p.program(0, obs)


def test_handcraft_rebuilds_when_furnaces_vanish():
    p = HandcraftPolicy()
    p.on_welcome(welcome_msg())
    assert "nearest_buildable" in p.program(0, _synthetic_obs(0))
    assert "harvest_resource" in p.program(1, _synthetic_obs(1, furnaces=2))
    assert "nearest_buildable" in p.program(2, _synthetic_obs(2, furnaces=0))
    obs = _synthetic_obs(3, furnaces=0)
    obs["inventory"]["stone-furnace"] = 0
    assert "harvest_resource" in p.program(3, obs)  # cannot rebuild, keep mining


# -- helpers and llm player (offline) -----------------------------------------

def test_fle_helpers_are_defensive():
    assert H.inventory({}) == {} and H.inventory({"inventory": 3}) == {}
    assert H.inventory_count({"inventory": {"coal": 4.0, "x": "y"}}, "coal") == 4
    assert H.entities({"entities": [1, {"name": "a"}]}) == [{"name": "a"}]
    assert H.entities_named({"entities": [{"name": "a"}, {"name": "b"}]}, "a") == [{"name": "a"}]
    assert H.entities_with_status({"entities": [{"status": "no_fuel"}]}, ["no_fuel"])
    assert H.entity_position({"position": {"x": 1, "y": 2.5}}) == (1.0, 2.5)
    assert H.entity_position({"position": "x"}) is None
    assert H.score({"score": "bad"}) == 0.0 and H.score({"score": 3}) == 3.0
    assert H.last_output({}) == "" and H.last_program_failed({}) is False
    assert H.last_program_failed({"last_program": {"error": True}}) is True
    assert H.raw_text({"raw_text": 5}) == ""
    assert H.get_in({"a": {"b": 1}}, "a", "b") == 1
    assert H.get_in({"a": 1}, "a", "b", default="d") == "d"


def test_llm_player_extracts_fenced_program_and_falls_back(monkeypatch):
    assert llm_player.extract_program("```python\nprint(1)\n```") == "print(1)"
    assert llm_player.extract_program("text ```py\nx = 1\n``` more") == "x = 1"
    assert llm_player.extract_program("bare code") == "bare code"
    assert llm_player.extract_program("``` \n```") is None
    assert llm_player.extract_program("") is None
    # provider none -> pass, no network
    p = llm_player.LLMPolicy(provider="none")
    p.on_welcome(welcome_msg())
    assert p.program(0, obs_msg(0)["observation"]) == "pass"
    # a broken client -> pass, never raises
    p2 = llm_player.LLMPolicy(provider="anthropic", model="m")

    class Boom:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                raise RuntimeError("no network")
    p2._client = Boom()
    assert p2.program(1, obs_msg(1)["observation"]) == "pass"

    class Fake:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                class B:
                    type = "text"
                    text = "Here:\n```python\nmove_to(nearest(Resource.IronOre))\n```"

                class R:
                    stop_reason = "end_turn"
                    content = [B()]
                return R()
    p3 = llm_player.LLMPolicy(provider="anthropic", model="m")
    p3._client = Fake()
    assert p3.program(2, obs_msg(2)["observation"]) == "move_to(nearest(Resource.IronOre))"
