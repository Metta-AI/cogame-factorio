"""End-to-end tests for the Coworld-contract websocket game server.

In-process aiohttp test server + real websocket clients, fake FLE
sessions (tests/fakes.py) and no Factorio processes.
"""

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestServer

from cogame_factorio import contract, uris
from cogame_factorio.replay import Replay
from cogame_factorio.results import RESULT_KEYS
from cogame_factorio.server import GameServer, make_replay_app
from cogame_factorio.version import GAME_VERSION

from tests.conftest import make_config
from tests.fakes import FakeFactorio, fake_session_factory


class ServerHarness:
    def __init__(self, cfg, tmp_path, *, session_kwargs=None,
                 factorio_fail=False):
        self.cfg = cfg
        self.results_path = tmp_path / "results.json"
        self.replay_path = tmp_path / "replay.json"
        self.failure_path = tmp_path / "player_failure.json"
        self.factory, self.sessions = fake_session_factory(
            **(session_kwargs or {}))
        self.factorio = FakeFactorio(cfg.num_seats, fail=factorio_fail)
        self.server = GameServer(
            cfg,
            results_uri=f"file://{self.results_path}",
            save_replay_uri=f"file://{self.replay_path}",
            player_failure_uri=f"file://{self.failure_path}",
            session_factory=self.factory,
            factorio_manager=self.factorio,
        )
        self.test_server = TestServer(self.server.make_app())
        self.episode_task = None

    async def __aenter__(self):
        await self.test_server.start_server()
        self.episode_task = asyncio.create_task(self.server.run_episode())
        return self

    async def __aexit__(self, *exc):
        if not self.episode_task.done():
            self.episode_task.cancel()
        try:
            await self.episode_task
        except (asyncio.CancelledError, Exception):
            pass
        await self.test_server.close()

    def ws_url(self, slot, token=None):
        token = f"token-{slot}" if token is None else token
        return str(self.test_server.make_url(
            f"/player?slot={slot}&token={token}"))

    def url(self, path):
        return str(self.test_server.make_url(path))

    def results(self):
        return json.loads(self.results_path.read_text())

    def replay(self):
        return Replay.parse(self.replay_path.read_bytes())


async def play_client(h, slot, program=lambda step, obs: f"place()  # {step}",
                      stop_after=None):
    """A well-behaved player: replies to every observation until done.
    Returns (welcome, observations, done_result)."""
    welcome = None
    observations = []
    result = None
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(h.ws_url(slot)) as ws:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                if data["type"] == "welcome":
                    welcome = data
                elif data["type"] == "observation":
                    observations.append(data)
                    if stop_after is not None and len(observations) > stop_after:
                        break
                    code = program(data["step"], data["observation"])
                    await ws.send_str(json.dumps(
                        {"type": "program", "step": data["step"], "code": code}))
                elif data["type"] == "done":
                    result = data["result"]
                    break
    return welcome, observations, result


# -- full episodes -----------------------------------------------------------

async def test_full_episode_two_seats(tmp_path):
    cfg = make_config(max_steps=3)
    async with ServerHarness(cfg, tmp_path) as h:
        (w0, obs0, r0), (w1, obs1, r1) = await asyncio.gather(
            play_client(h, 0), play_client(h, 1, lambda s, o: "x = 1/0"))
        result = await h.episode_task

    # welcome
    assert w0["type"] == "welcome" and w0["protocol"] == contract.PROTOCOL
    assert set(w0) == set(contract.WELCOME_KEYS)
    assert w0["game_version"] == GAME_VERSION and w0["slot"] == 0
    assert w0["name"] == "bot-0" and w0["max_steps"] == 3
    assert w0["task"]["key"] == "open_play"
    assert set(w0["episode"]) == set(contract.EPISODE_KEYS)
    assert w0["episode"]["seats"] == 2 and w0["episode"]["slot"] == 0
    assert w0["episode"]["strike_limit"] == 3
    assert w0["episode"]["starting_inventory"]["coal"] == 500
    assert w0["episode"]["map_bounds"] == {"x0": -64, "y0": -64,
                                          "x1": 64, "y1": 64}
    assert "api docs" in w0["api_docs"]
    # observations
    assert [o["step"] for o in obs0] == [0, 1, 2]
    assert set(obs0[0]) == set(contract.OBSERVATION_MESSAGE_KEYS)
    assert set(obs0[0]["observation"]) == set(contract.OBSERVATION_KEYS)
    assert obs0[1]["observation"]["last_program"]["code"] == "place()  # 0"
    assert obs1[1]["observation"]["last_program"]["error"] is True
    assert obs0[2]["observation"]["score"] == 2.0
    # done
    assert r0 == r1 and r0["scores"] == [3.0, 0.0]
    assert r0["error_steps"] == [0, 3] and r0["steps_completed"] == [3, 3]
    assert r0["end_reason"] == "steps_cap"
    assert set(r0) == RESULT_KEYS

    results = h.results()
    assert results == r0
    assert results["names"] == ["bot-0", "bot-1"]
    assert results["task_key"] == "open_play"
    assert results["throughputs"] == [None, None]
    assert results["dead_seats"] == [False, False]
    assert results["noop_steps"] == [0, 0]

    replay = h.replay()
    doc = replay.doc
    assert doc["game_version"] == GAME_VERSION
    assert doc["names"] == ["bot-0", "bot-1"]
    assert doc["task"]["key"] == "open_play"
    assert doc["map"]["resources"] and doc["map"]["water"]
    assert "tokens" not in doc["config"]
    assert doc["result"] == results
    seat0 = doc["seats"][0]
    assert seat0["final_score"] == 3.0 and not seat0["dead"]
    assert [s["step"] for s in seat0["steps"]] == [0, 1, 2]
    last = seat0["steps"][-1]
    assert last["entities"] and last["belts"] and last["pipes"]
    assert last["score"] == 3.0 and last["code"] == "place()  # 2"
    assert last["character"]["x"] == 17.5
    assert doc["seats"][1]["steps"][0]["error"] is True
    assert not h.failure_path.exists()
    assert all(s.closed for s in h.sessions)
    assert h.factorio.stopped
    assert result.end_reason == "steps_cap"


async def test_missing_player_noops_strikes_out_and_is_reported(tmp_path):
    cfg = make_config(max_steps=6, step_deadline_seconds=0.1,
                      player_connect_timeout_seconds=0.2, strike_limit=2)
    async with ServerHarness(cfg, tmp_path) as h:
        _, _, r1 = await play_client(h, 1)
        await h.episode_task
    results = h.results()
    assert results["dead_seats"] == [True, False]
    assert results["steps_completed"] == [0, 6]
    assert results["noop_causes"][0]["disconnected"] == 2
    assert results["scores"][0] == 0.0
    failure = json.loads(h.failure_path.read_text())
    assert failure["failed_policy_index"] == 0
    assert "did not connect" in failure["message"]
    assert set(failure) == {"message", "failed_policy_index"}


async def test_two_no_shows_report_lowest_slot(tmp_path):
    cfg = make_config(num_seats=3, max_steps=2, step_deadline_seconds=0.05,
                      player_connect_timeout_seconds=0.1, strike_limit=1)
    async with ServerHarness(cfg, tmp_path) as h:
        await play_client(h, 0)
        await h.episode_task
    failure = json.loads(h.failure_path.read_text())
    assert failure["failed_policy_index"] == 1
    assert h.results()["dead_seats"] == [False, True, True]


async def test_reconnect_resends_current_observation(tmp_path):
    cfg = make_config(num_seats=1, max_steps=3, step_deadline_seconds=3.0)
    async with ServerHarness(cfg, tmp_path) as h:
        # first connection: answer step 0, read step 1, then drop
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(h.ws_url(0)) as ws:
                w = json.loads((await ws.receive()).data)
                assert w["type"] == "welcome"
                o0 = json.loads((await ws.receive()).data)
                assert o0["step"] == 0
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 0, "code": "a()"}))
                o1 = json.loads((await ws.receive()).data)
                assert o1["step"] == 1
                d1 = o1["deadline_seconds"]
            await asyncio.sleep(0.3)
            # reconnect: welcome again, then the SAME step with less time
            async with session.ws_connect(h.ws_url(0)) as ws:
                w2 = json.loads((await ws.receive()).data)
                assert w2["type"] == "welcome"
                o1b = json.loads((await ws.receive()).data)
                assert o1b["step"] == 1
                assert o1b["deadline_seconds"] < d1 - 0.2
                assert o1b["observation"]["last_program"]["code"] == "a()"
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 1, "code": "b()"}))
                o2 = json.loads((await ws.receive()).data)
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 2, "code": "c()"}))
                done = json.loads((await ws.receive()).data)
                assert done["type"] == "done"
        await h.episode_task
    results = h.results()
    assert results["steps_completed"] == [3] and results["noop_steps"] == [0]
    assert h.sessions[0].programs == ["a()", "b()", "c()"]


async def test_malformed_and_wrong_step_replies(tmp_path):
    cfg = make_config(num_seats=1, max_steps=4, step_deadline_seconds=0.5,
                      strike_limit=10)
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(h.ws_url(0)) as ws:
                await ws.receive()  # welcome
                o = json.loads((await ws.receive()).data)
                assert o["step"] == 0
                await ws.send_str("{not json")  # -> immediate malformed noop
                o = json.loads((await ws.receive()).data)
                assert o["step"] == 1
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 1, "code": 42}))  # malformed
                o = json.loads((await ws.receive()).data)
                assert o["step"] == 2
                # wrong step: ignored, then a correct reply in time
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 0, "code": "late()"}))
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 2, "code": "ok()"}))
                o = json.loads((await ws.receive()).data)
                assert o["step"] == 3
                assert o["observation"]["last_program"]["code"] == "ok()"
                # wrong step only -> times out
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 9, "code": "x()"}))
                done = json.loads((await ws.receive()).data)
                assert done["type"] == "done"
        await h.episode_task
    r = h.results()
    assert r["steps_completed"] == [1] and r["noop_steps"] == [3]
    causes = r["noop_causes"][0]
    assert causes["malformed"] == 2 and causes["timeout"] == 1
    assert causes["wrong_step"] == 2
    assert h.sessions[0].programs == ["ok()"]


async def test_dead_seat_socket_closed_and_loop_ends(tmp_path):
    cfg = make_config(num_seats=1, max_steps=10, step_deadline_seconds=0.1,
                      strike_limit=2)
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(h.ws_url(0)) as ws:
                msgs = []
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    msgs.append(json.loads(msg.data))
                # welcome + 2 observations, then the server closes us
                assert [m["type"] for m in msgs] == \
                    ["welcome", "observation", "observation"]
        await h.episode_task
    r = h.results()
    assert r["dead_seats"] == [True] and r["noop_steps"] == [2]
    assert len(h.replay().seats[0]["steps"]) == 2
    assert h.replay().seats[0]["steps"][1]["noop"] is True
    assert h.replay().seats[0]["steps"][1]["code"] == ""


async def test_wall_clock_budget_writes_artifacts(tmp_path):
    cfg = make_config(num_seats=1, max_steps=100, step_deadline_seconds=5.0,
                      wall_clock_budget_seconds=0.5)
    async with ServerHarness(cfg, tmp_path) as h:
        _, obs, result = await play_client(
            h, 0, program=lambda s, o: "#slow 0.1\nplace()")
        await h.episode_task
    assert result["end_reason"] == "wall_clock"
    assert 1 <= result["steps_completed"][0] < 100
    assert h.replay().result["end_reason"] == "wall_clock"


# -- auth / routes -----------------------------------------------------------

async def test_bad_token_rejected(tmp_path):
    cfg = make_config()
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await session.ws_connect(h.ws_url(0, "bad"))
            assert exc.value.status == 403


@pytest.mark.parametrize("slot", ["2", "-1", "x", ""])
async def test_bad_slot_rejected(tmp_path, slot):
    cfg = make_config()
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await session.ws_connect(
                    h.url(f"/player?slot={slot}&token=token-0"))
            assert exc.value.status == 403


async def test_duplicate_slot_rejected_while_alive(tmp_path):
    cfg = make_config()
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(h.ws_url(0)):
                with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                    await session.ws_connect(h.ws_url(0))
                assert exc.value.status == 409
            await asyncio.sleep(0.05)
            async with session.ws_connect(h.ws_url(0)) as ws:  # after close: ok
                assert json.loads((await ws.receive()).data)["type"] == "welcome"


async def test_healthz_and_client_pages(tmp_path):
    cfg = make_config()
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            async with session.get(h.url("/healthz")) as resp:
                assert resp.status == 200
                assert await resp.json() == {"status": "ok"}
            async with session.get(h.url("/client/global")) as resp:
                assert resp.status == 200
                assert "text/html" in resp.headers["Content-Type"]
            async with session.get(
                    h.url("/client/player?slot=0&token=token-0")) as resp:
                assert resp.status == 200
            async with session.get(
                    h.url("/client/player?slot=0&token=nope")) as resp:
                assert resp.status == 403


async def test_global_ws_first_message_progress_and_done(tmp_path):
    cfg = make_config(num_seats=1, max_steps=2)
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(h.url("/global")) as gws:
                first = json.loads((await gws.receive()).data)
                assert first["type"] == "status"
                assert set(first) == set(contract.STATUS_KEYS)
                assert first["game_version"] == GAME_VERSION
                assert first["players"] == ["bot-0"] and not first["done"]
                assert first["task"]["key"] == "open_play"
                await play_client(h, 0)
                msgs = []
                async for msg in gws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    msgs.append(json.loads(msg.data))
                    if msgs[-1]["type"] == "done":
                        break
                progress = [m for m in msgs if m["type"] == "progress"]
                assert [p["step"] for p in progress] == [0, 1]
                assert progress[-1]["score"] == 2.0
                assert msgs[-1]["type"] == "done"
                assert msgs[-1]["result"]["scores"] == [2.0]
            # late viewer: self-contained snapshot
            async with session.ws_connect(h.url("/global")) as gws:
                late = json.loads((await gws.receive()).data)
                assert late["done"] and late["result"]["scores"] == [2.0]
        await h.episode_task


# -- faults ------------------------------------------------------------------

async def test_factorio_start_failure_writes_fault_artifacts(tmp_path):
    cfg = make_config(num_seats=2, max_steps=2)
    async with ServerHarness(cfg, tmp_path, factorio_fail=True) as h:
        with pytest.raises(Exception):
            await h.episode_task
    r = h.results()
    assert r["end_reason"] == "sim_fault"
    assert r["scores"] == [0.0, 0.0] and set(r) == RESULT_KEYS
    assert h.replay().result["end_reason"] == "sim_fault"


async def test_session_start_failure_writes_fault_artifacts(tmp_path):
    cfg = make_config(num_seats=1, max_steps=2)
    async with ServerHarness(cfg, tmp_path,
                             session_kwargs={"fail_start": True}) as h:
        with pytest.raises(ConnectionError):
            await h.episode_task
    assert h.results()["end_reason"] == "sim_fault"
    assert h.factorio.stopped


async def test_session_fault_mid_episode_is_contained(tmp_path):
    cfg = make_config(num_seats=2, max_steps=3)
    async with ServerHarness(cfg, tmp_path) as h:
        (_, _, r0), _ = await asyncio.gather(
            play_client(h, 0, lambda s, o: "#fault" if s == 1 else "ok()"),
            play_client(h, 1))
        result = await h.episode_task
    assert r0["end_reason"] == "sim_fault"
    assert r0["steps_completed"] == [1, 3]
    assert result.end_reason == "sim_fault"


async def test_unresponsive_client_never_blocks_exit(tmp_path):
    cfg = make_config(num_seats=1, max_steps=1)
    async with ServerHarness(cfg, tmp_path) as h:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(h.ws_url(0)) as ws:
                await ws.receive()  # welcome
                o = json.loads((await ws.receive()).data)
                await ws.send_str(json.dumps(
                    {"type": "program", "step": 0, "code": "x()"}))
                # never read again; the server must still finish
                await asyncio.wait_for(h.episode_task, 10)
    assert h.results()["steps_completed"] == [1]


async def test_failing_results_uri_does_not_block_replay_write(tmp_path):
    cfg = make_config(num_seats=1, max_steps=1)
    h = ServerHarness(cfg, tmp_path)
    h.server.results_uri = "bogus://nowhere"
    async with h:
        await play_client(h, 0)
        with pytest.raises(IOError, match="artifact writes failed"):
            await h.episode_task
    assert h.replay_path.exists()


# -- uris --------------------------------------------------------------------

async def test_file_uri_round_trip(tmp_path):
    uri = f"file://{tmp_path}/nested/dir/x.bin"
    await uris.write_uri(uri, b"abc")
    assert await uris.read_uri(uri) == b"abc"
    assert uris.local_path("file:///coworld/out/results.json") == \
        Path("/coworld/out/results.json")
    assert uris.local_path("http://x/y") is None


async def test_unsupported_scheme_rejected():
    with pytest.raises(ValueError):
        await uris.read_uri("bogus://x")


# -- replay mode -------------------------------------------------------------

async def _episode_replay_bytes(tmp_path):
    cfg = make_config(num_seats=1, max_steps=2)
    async with ServerHarness(cfg, tmp_path) as h:
        await play_client(h, 0)
        await h.episode_task
    return h.replay_path.read_bytes()


async def test_replay_mode_serves_bytes_and_placeholder(tmp_path):
    data = await _episode_replay_bytes(tmp_path)
    app = make_replay_app(data, viewer_dist=tmp_path / "no-dist")
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(server.make_url("/replay-data")) as resp:
                assert resp.status == 200
                assert await resp.read() == data
                assert "application/json" in resp.headers["Content-Type"]
            async with session.get(server.make_url("/client/replay/")) as resp:
                assert resp.status == 200
                assert "replay" in (await resp.text())
            async with session.get(server.make_url("/healthz")) as resp:
                assert resp.status == 200
            async with session.ws_connect(server.make_url("/replay")) as ws:
                first = json.loads((await ws.receive()).data)
                assert first["type"] == "replay_header"
                assert first["names"] == ["bot-0"]
    finally:
        await server.close()


async def test_replay_mode_serves_viewer_bundle_when_built(tmp_path):
    data = await _episode_replay_bytes(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>viewer</title>")
    (dist / "viewer.js").write_text("// js")
    app = make_replay_app(data, viewer_dist=dist)
    server = TestServer(app)
    await server.start_server()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(server.make_url("/client/replay"),
                                   allow_redirects=False) as resp:
                assert resp.status == 302
                assert resp.headers["Location"] == "/client/replay/"
            async with session.get(server.make_url("/client/replay/")) as resp:
                assert resp.status == 200
                assert "viewer" in await resp.text()
            async with session.get(
                    server.make_url("/client/replay/viewer.js")) as resp:
                assert resp.status == 200
    finally:
        await server.close()


async def test_replay_mode_rejects_corrupt_replay():
    from cogame_factorio.replay import ReplayError
    with pytest.raises(ReplayError):
        make_replay_app(b"{not json")
    with pytest.raises(ReplayError):
        make_replay_app(json.dumps({"format": "other", "version": 1}).encode())
