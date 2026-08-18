"""async_main startup behavior: config errors are clean exit-2 paths (no
tracebacks); replay mode serves; unknown task keys exit 2."""

import asyncio
import json
import socket

import aiohttp
import pytest

from cogame_factorio.replay import ReplayWriter
from cogame_factorio.results import EpisodeResult, SeatOutcome, results_doc
from cogame_factorio.server import async_main

from tests.conftest import make_config


def _clear_cogame_env(monkeypatch):
    for name in ("COGAME_CONFIG_URI", "COGAME_LOAD_REPLAY_URI",
                 "COGAME_RESULTS_URI", "COGAME_SAVE_REPLAY_URI",
                 "COGAME_PLAYER_FAILURE_URI", "COGAME_HOST", "COGAME_PORT",
                 "COGAME_FACTORIO_SERVERS"):
        monkeypatch.delenv(name, raising=False)


async def test_missing_config_uri_exits_2(monkeypatch, capsys):
    _clear_cogame_env(monkeypatch)
    assert await async_main() == 2
    assert "COGAME_CONFIG_URI is required" in capsys.readouterr().err


async def test_unreadable_config_uri_exits_2(monkeypatch, capsys):
    _clear_cogame_env(monkeypatch)
    monkeypatch.setenv("COGAME_CONFIG_URI", "file:///no/such/config.json")
    assert await async_main() == 2
    err = capsys.readouterr().err
    assert "invalid config" in err and "Traceback" not in err


async def test_malformed_config_json_exits_2(monkeypatch, capsys, tmp_path):
    _clear_cogame_env(monkeypatch)
    bad = tmp_path / "config.json"
    bad.write_text("{not json")
    monkeypatch.setenv("COGAME_CONFIG_URI", f"file://{bad}")
    assert await async_main() == 2
    err = capsys.readouterr().err
    assert "not valid JSON" in err and "Traceback" not in err


async def test_invalid_config_shape_exits_2(monkeypatch, capsys, tmp_path):
    _clear_cogame_env(monkeypatch)
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"players": []}))
    monkeypatch.setenv("COGAME_CONFIG_URI", f"file://{bad}")
    assert await async_main() == 2
    assert "players" in capsys.readouterr().err


async def test_unknown_task_key_exits_2(monkeypatch, capsys, tmp_path):
    pytest.importorskip("fle")
    _clear_cogame_env(monkeypatch)
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps({"players": [{"name": "a"}], "tokens": ["t"],
                               "task": "no_such_task"}))
    monkeypatch.setenv("COGAME_CONFIG_URI", f"file://{bad}")
    assert await async_main() == 2
    err = capsys.readouterr().err
    assert "no_such_task" in err and "Traceback" not in err


async def test_malformed_http_config_json_exits_2(monkeypatch, capsys):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def handle(request):
        return web.Response(text="{not json", content_type="application/json")

    app = web.Application()
    app.router.add_get("/config", handle)
    server = TestServer(app)
    await server.start_server()
    try:
        _clear_cogame_env(monkeypatch)
        monkeypatch.setenv("COGAME_CONFIG_URI", str(server.make_url("/config")))
        assert await async_main() == 2
        assert "not valid JSON" in capsys.readouterr().err
    finally:
        await server.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_replay_mode_entry_serves(monkeypatch, tmp_path):
    cfg = make_config(num_seats=1)
    writer = ReplayWriter(cfg, {"key": "open_play", "goal_description": ""})
    doc = results_doc(cfg, EpisodeResult((SeatOutcome(),), "steps_cap", 1.0))
    data = writer.finalize(doc)
    replay_path = tmp_path / "replay.json"
    replay_path.write_bytes(data)

    port = _free_port()
    _clear_cogame_env(monkeypatch)
    monkeypatch.setenv("COGAME_LOAD_REPLAY_URI", f"file://{replay_path}")
    monkeypatch.setenv("COGAME_HOST", "127.0.0.1")
    monkeypatch.setenv("COGAME_PORT", str(port))

    task = asyncio.create_task(async_main())
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(100):
                if task.done():
                    raise AssertionError(f"async_main exited: {task.result()}")
                try:
                    async with session.get(
                            f"http://127.0.0.1:{port}/healthz") as resp:
                        assert resp.status == 200
                        break
                except aiohttp.ClientConnectorError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("replay-mode server never came up")
            async with session.get(
                    f"http://127.0.0.1:{port}/replay-data") as resp:
                assert resp.status == 200
                assert await resp.read() == data
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_replay_mode_corrupt_replay_exits_2(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "replay.json"
    bad.write_text("nope")
    _clear_cogame_env(monkeypatch)
    monkeypatch.setenv("COGAME_LOAD_REPLAY_URI", f"file://{bad}")
    assert await async_main() == 2
    assert "invalid replay" in capsys.readouterr().err
