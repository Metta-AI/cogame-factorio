"""FactorioServerManager: env parsing, per-seat spawn layout, readiness,
shutdown — using a tiny fake 'factorio' script (no real server)."""

import asyncio
import os
import stat
import sys

import pytest

from cogame_factorio.factorio import (Endpoint, FactorioError,
                                      FactorioServerManager,
                                      parse_servers_env)

FAKE_FACTORIO = r'''#!/usr/bin/env python3
"""Fake factorio: prints a Factorio-ish log, opens the --rcon-port,
serves until SIGTERM. Also verifies -c config.ini points at a writable
write-data dir with a scenarios link."""
import os, signal, socket, sys, time
args = sys.argv[1:]
cfg = args[args.index("-c") + 1]
port = int(args[args.index("--rcon-port") + 1])
text = open(cfg).read()
wdir = [l.split("=", 1)[1] for l in text.splitlines() if l.startswith("write-data=")][0]
assert os.path.isdir(os.path.join(wdir, "scenarios", "default_lab_scenario")), wdir
open(os.path.join(wdir, "factorio-current.log"), "w").write("ok\n")
print("   0.001 Factorio 1.1.110 (build 62357, linux64, headless)", flush=True)
print("2026-01-01 00:00:00 [COMMAND] <server> (command): /sc rcon.print(1)", flush=True)
print("global.actions = {}  -- lua echo continuation", flush=True)
if os.environ.get("FAKE_FACTORIO_DIE"):
    print("   0.002 Error: dying on purpose", flush=True)
    sys.exit(3)
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen(5)
print(f"   0.816 Info RemoteCommandProcessor.cpp:133: Starting RCON interface at IP ADDR:({{0.0.0.0:{port}}})", flush=True)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(0.2)
'''


@pytest.fixture
def fake_root(tmp_path):
    root = tmp_path / "factorio"
    (root / "bin" / "x64").mkdir(parents=True)
    binary = root / "bin" / "x64" / "factorio"
    binary.write_text(FAKE_FACTORIO)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    (root / "config").mkdir()
    for name in ("server-settings.json", "map-gen-settings.json",
                 "map-settings.json", "server-adminlist.json"):
        (root / "config" / name).write_text("{}")
    (root / "scenarios" / "default_lab_scenario").mkdir(parents=True)
    return root


def _free_ports(n):
    import socket
    socks = [socket.socket() for _ in range(n)]
    ports = []
    for s in socks:
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def test_parse_servers_env():
    assert parse_servers_env("localhost:27000, 10.0.0.2:27001,") == [
        Endpoint("localhost", 27000), Endpoint("10.0.0.2", 27001)]
    with pytest.raises(FactorioError):
        parse_servers_env("nocolon")
    with pytest.raises(FactorioError):
        parse_servers_env("host:abc")


async def test_external_servers_need_enough_entries():
    mgr = FactorioServerManager(3, servers_env="h:1,h:2")
    with pytest.raises(FactorioError, match="need 3"):
        await mgr.start()


async def test_spawns_one_server_per_seat_and_stops(fake_root, tmp_path, capsys):
    base = _free_ports(2)[0]
    mgr = FactorioServerManager(
        2, servers_env="", root=str(fake_root), rcon_base_port=base,
        write_base=str(tmp_path / "seats"), start_timeout_seconds=20)
    endpoints = await mgr.start()
    try:
        assert endpoints == [Endpoint("127.0.0.1", base),
                             Endpoint("127.0.0.1", base + 1)]
        for slot in range(2):
            wdir = tmp_path / "seats" / f"seat-{slot}"
            assert (wdir / "config.ini").read_text().splitlines()[-1] == \
                f"write-data={wdir}"
            assert (wdir / "scenarios").is_symlink()
            assert (wdir / "factorio-current.log").exists()
        assert all(p.poll() is None for p in mgr._procs)
        procs = list(mgr._procs)
    finally:
        mgr.stop()
    assert all(p.poll() is not None for p in procs)
    await asyncio.sleep(0.1)
    err = capsys.readouterr().err
    assert "factorio[0]:    0.001 Factorio 1.1.110" in err
    assert "RCON ready" in err
    # RCON command echoes and their Lua continuation lines are dropped
    assert "[COMMAND]" not in err and "lua echo" not in err


async def test_child_exit_before_rcon_is_a_factorio_error(fake_root, tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("FAKE_FACTORIO_DIE", "1")
    base = _free_ports(1)[0]
    mgr = FactorioServerManager(
        1, servers_env="", root=str(fake_root), rcon_base_port=base,
        write_base=str(tmp_path / "seats"), start_timeout_seconds=20)
    with pytest.raises(FactorioError, match="exited with code 3"):
        await mgr.start()
    mgr.stop()


async def test_missing_binary_is_a_factorio_error(tmp_path):
    mgr = FactorioServerManager(1, servers_env="", root=str(tmp_path / "nope"))
    with pytest.raises(FactorioError, match="binary not found"):
        await mgr.start()


async def test_readiness_timeout(fake_root, tmp_path, monkeypatch):
    # binary that never opens the port: swap in a sleeper
    binary = fake_root / "bin" / "x64" / "factorio"
    binary.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n")
    mgr = FactorioServerManager(
        1, servers_env="", root=str(fake_root), rcon_base_port=_free_ports(1)[0],
        write_base=str(tmp_path / "seats"), start_timeout_seconds=1.0)
    with pytest.raises(FactorioError, match="not ready"):
        await mgr.start()
    mgr.stop()
