"""Factorio headless server processes: one per seat.

Production: the game container spawns ``N`` children of the Factorio
install at ``COGAME_FACTORIO_ROOT`` (default ``/opt/factorio``), each with
its own RCON port (``COGAME_FACTORIO_RCON_BASE_PORT`` + slot, default
27100+slot), game UDP port (34197+slot) and — because Factorio holds a
lock on, and writes logs/temp/saves into, its write directory — its own
``write-data`` directory selected through a per-seat ``config.ini``
(``-c``). The install tree itself can stay read-only; the FLE
``default_lab_scenario`` is reached through a ``scenarios`` symlink in the
write dir (Factorio looks for scenarios under write-data).

Development / tests: ``COGAME_FACTORIO_SERVERS=host:rcon_port,...`` names
already-running servers (``fle cluster start -n N``) and nothing is
spawned.

Readiness = the RCON TCP port accepts a connection (Factorio opens it
right after the map is loaded and the state is InGame); bounded by
``COGAME_FACTORIO_START_TIMEOUT`` (default 180s). Children are logged to
stderr with a per-seat prefix and terminated on ``stop()``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = "/opt/factorio"
DEFAULT_RCON_BASE_PORT = 27100
DEFAULT_GAME_BASE_PORT = 34197
DEFAULT_START_TIMEOUT_SECONDS = 180.0
RCON_PASSWORD = "factorio"
SCENARIO = "default_lab_scenario"
STOP_GRACE_SECONDS = 10.0


class FactorioError(RuntimeError):
    """A Factorio server could not be located or started."""


@dataclass(frozen=True)
class Endpoint:
    host: str
    rcon_port: int


def parse_servers_env(value: str) -> list[Endpoint]:
    endpoints = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, port = item.rpartition(":")
        if not sep or not host:
            raise FactorioError(
                f"COGAME_FACTORIO_SERVERS entry {item!r} is not host:port")
        try:
            endpoints.append(Endpoint(host, int(port)))
        except ValueError:
            raise FactorioError(
                f"COGAME_FACTORIO_SERVERS entry {item!r} has a bad port")
    return endpoints


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class FactorioServerManager:
    def __init__(self, num_seats: int, *,
                 servers_env: str | None = None,
                 root: str | None = None,
                 rcon_base_port: int | None = None,
                 game_base_port: int = DEFAULT_GAME_BASE_PORT,
                 write_base: str | None = None,
                 start_timeout_seconds: float | None = None):
        self.num_seats = num_seats
        env = os.environ
        if servers_env is None:
            servers_env = env.get("COGAME_FACTORIO_SERVERS", "")
        self._external = parse_servers_env(servers_env) if servers_env else []
        self.root = Path(root or env.get("COGAME_FACTORIO_ROOT", DEFAULT_ROOT))
        self.rcon_base_port = rcon_base_port if rcon_base_port is not None \
            else int(env.get("COGAME_FACTORIO_RCON_BASE_PORT",
                             DEFAULT_RCON_BASE_PORT))
        self.game_base_port = game_base_port
        self.write_base = write_base or env.get("COGAME_FACTORIO_WRITE_DIR")
        self.start_timeout = start_timeout_seconds if start_timeout_seconds \
            is not None else float(env.get("COGAME_FACTORIO_START_TIMEOUT",
                                           DEFAULT_START_TIMEOUT_SECONDS))
        self._procs: list[subprocess.Popen] = []
        self._log_threads: list[threading.Thread] = []
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self.endpoints: list[Endpoint] = []

    @property
    def external(self) -> bool:
        return bool(self._external)

    # -- start ---------------------------------------------------------------

    async def start(self) -> list[Endpoint]:
        if self._external:
            if len(self._external) < self.num_seats:
                raise FactorioError(
                    f"COGAME_FACTORIO_SERVERS names {len(self._external)} "
                    f"servers, need {self.num_seats}")
            self.endpoints = self._external[:self.num_seats]
            print(f"using external Factorio servers: "
                  f"{[f'{e.host}:{e.rcon_port}' for e in self.endpoints]}",
                  file=sys.stderr, flush=True)
        else:
            self.endpoints = self._spawn_all()
        await self._wait_ready()
        return self.endpoints

    def _binary(self) -> Path:
        return self.root / "bin" / "x64" / "factorio"

    def _spawn_all(self) -> list[Endpoint]:
        binary = self._binary()
        if not binary.is_file():
            raise FactorioError(f"Factorio binary not found at {binary}")
        cfg_dir = self.root / "config"
        scen_dir = self.root / "scenarios"
        for needed in (cfg_dir / "server-settings.json",
                       scen_dir / SCENARIO):
            if not needed.exists():
                raise FactorioError(f"missing {needed} (FLE cluster config/"
                                    f"scenarios must be installed under "
                                    f"{self.root})")
        if self.write_base:
            base = Path(self.write_base)
            base.mkdir(parents=True, exist_ok=True)
        else:
            self._tmpdir = tempfile.TemporaryDirectory(prefix="factorio-seats-")
            base = Path(self._tmpdir.name)
        endpoints = []
        for slot in range(self.num_seats):
            wdir = base / f"seat-{slot}"
            wdir.mkdir(parents=True, exist_ok=True)
            (wdir / "config.ini").write_text(
                "[path]\nread-data=__PATH__executable__/../../data\n"
                f"write-data={wdir}\n")
            link = wdir / "scenarios"
            if link.is_symlink() or link.exists():
                if link.is_dir() and not link.is_symlink():
                    shutil.rmtree(link)
                else:
                    link.unlink()
            link.symlink_to(scen_dir, target_is_directory=True)
            rcon_port = self.rcon_base_port + slot
            game_port = self.game_base_port + slot
            cmd = [
                str(binary), "-c", str(wdir / "config.ini"),
                "--start-server-load-scenario", SCENARIO,
                "--port", str(game_port),
                "--rcon-port", str(rcon_port),
                "--rcon-password", RCON_PASSWORD,
                "--server-settings", str(cfg_dir / "server-settings.json"),
                "--map-gen-settings", str(cfg_dir / "map-gen-settings.json"),
                "--map-settings", str(cfg_dir / "map-settings.json"),
            ]
            adminlist = cfg_dir / "server-adminlist.json"
            if adminlist.exists():
                cmd += ["--server-adminlist", str(adminlist)]
            print(f"factorio[{slot}]: starting rcon={rcon_port} "
                  f"game={game_port} write-data={wdir}",
                  file=sys.stderr, flush=True)
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, cwd=str(wdir), text=True,
                errors="replace")
            self._procs.append(proc)
            t = threading.Thread(target=self._pump, args=(slot, proc),
                                 daemon=True, name=f"factorio-log-{slot}")
            t.start()
            self._log_threads.append(t)
            endpoints.append(Endpoint("127.0.0.1", rcon_port))
        return endpoints

    @staticmethod
    def _pump(slot: int, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            # Factorio's per-mod "Loading ..." lines are noise
            if " Loading mod " in line or "Checksum of " in line:
                continue
            print(f"factorio[{slot}]: {line}", file=sys.stderr, flush=True)

    async def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.start_timeout
        pending = list(range(len(self.endpoints)))
        while pending:
            for slot in list(pending):
                if not self._external:
                    proc = self._procs[slot]
                    if proc.poll() is not None:
                        raise FactorioError(
                            f"factorio[{slot}] exited with code "
                            f"{proc.returncode} before RCON came up")
                ep = self.endpoints[slot]
                if await asyncio.get_running_loop().run_in_executor(
                        None, _port_open, ep.host, ep.rcon_port):
                    print(f"factorio[{slot}]: RCON ready at "
                          f"{ep.host}:{ep.rcon_port}", file=sys.stderr,
                          flush=True)
                    pending.remove(slot)
            if not pending:
                return
            if time.monotonic() > deadline:
                raise FactorioError(
                    f"Factorio RCON not ready for seats {pending} within "
                    f"{self.start_timeout:g}s")
            await asyncio.sleep(0.5)

    # -- stop ----------------------------------------------------------------

    def stop(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        for proc in self._procs:
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                proc.wait(timeout=5)
        self._procs = []
        if self._tmpdir is not None:
            try:
                self._tmpdir.cleanup()
            except Exception:
                pass
            self._tmpdir = None
