"""aiohttp game server implementing the Coworld runtime contract.

Episode mode (default): read the game config from ``COGAME_CONFIG_URI``,
bring up one Factorio server per seat (child processes, or the servers
named by ``COGAME_FACTORIO_SERVERS``), open one FLE session per seat,
serve ``GET /player?slot=N&token=T`` websockets, run the per-seat step
loops (docs/PROTOCOL.md), broadcast ``done``, write results
(``COGAME_RESULTS_URI``) and the replay (``COGAME_SAVE_REPLAY_URI``), and
exit 0. Seats that never connect are declared to
``COGAME_PLAYER_FAILURE_URI`` and play noops until they strike out.

Global viewer: ``GET /global`` is a broadcast-only websocket (status
snapshot on connect, ``progress`` after every executed step, final
``done``); ``GET /client/global`` and ``GET /client/player?slot=N&token=T``
are minimal HTML pages.

Replay mode: with ``COGAME_LOAD_REPLAY_URI`` set no episode runs; the
replay JSON is served at ``GET /replay-data`` and the static viewer bundle
(``viewer/dist``) at ``/client/replay/``.

Entry point: ``python -m cogame_factorio.server``. Binds ``COGAME_HOST``/
``COGAME_PORT`` (default 0.0.0.0:8080). Exit codes: 0 episode complete
(artifacts attempted), 2 missing/invalid config, 1 host failure (Factorio
or FLE never came up — fault artifacts are still attempted).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from aiohttp import WSCloseCode, WSMsgType, web

from . import contract, uris
from .config import ConfigError, GameConfig
from .engine import Engine, validate_program_message
from .factorio import Endpoint, FactorioError, FactorioServerManager
from .replay import Replay, ReplayError, ReplayWriter
from .results import (EpisodeResult, SeatOutcome, fault_results_doc,
                      results_doc)
from .session import FactorioSession, Session
from .version import GAME_VERSION

PROTOCOL = contract.PROTOCOL

# After artifacts are written, keep serving briefly so clients can finish
# reading the done message and close their websockets.
SHUTDOWN_GRACE_SECONDS = 1.0
# Per-seat bound on sending the final done message + close.
DONE_SEND_TIMEOUT_SECONDS = 3.0
# aiohttp ping/pong heartbeat on /player sockets: a half-open connection
# is reaped within ~this interval instead of 409-ing real reconnects.
PLAYER_WS_HEARTBEAT_SECONDS = 20.0
# Bound on FLE session start (RCON connect + task setup) per seat.
SESSION_START_TIMEOUT_SECONDS = 300.0

SessionFactory = Callable[[int, Endpoint, GameConfig], Session]

GLOBAL_CLIENT_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>cogame-factorio</title>
<style>
  body { font-family: ui-monospace, monospace; margin: 2rem; }
  #log { white-space: pre-wrap; }
</style>
</head>
<body>
<h1>cogame-factorio live feed</h1>
<div id="log">connecting to /global ...</div>
<script>
const log = document.getElementById("log");
const proto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(proto + "//" + location.host + "/global");
ws.onmessage = (ev) => { log.textContent += "\\n" + ev.data; };
ws.onopen = () => { log.textContent = "connected"; };
ws.onclose = () => { log.textContent += "\\n[closed]"; };
</script>
</body>
</html>
"""

PLAYER_CLIENT_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>cogame-factorio seat</title></head>
<body style="font-family: ui-monospace, monospace; margin: 2rem;">
<h1>cogame-factorio</h1>
<p>Seat <span id="slot"></span> is played over the websocket protocol
(<code>GET /player?slot=N&amp;token=T</code>, see docs/PROTOCOL.md);
this page only confirms the seat credential is valid.</p>
<script>
document.getElementById("slot").textContent =
  new URLSearchParams(location.search).get("slot");
</script>
</body>
</html>
"""


class WsSeat:
    """One player seat: websocket state + engine ProgramSource.

    ``get_program`` sends the step's observation to the connected player
    and waits for a valid matching-step ``program`` reply until the
    deadline. The pending observation is kept so a (re)connecting player
    gets ``welcome`` and then the current observation with the remaining
    deadline.
    """

    def __init__(self, slot: int, name: str):
        self.slot = slot
        self.name = name
        self.ws: web.WebSocketResponse | None = None
        self.ever_connected = False
        self.welcome: dict | None = None
        self.wrong_step_count = 0
        self._connected = asyncio.Event()
        self._pending: tuple[int, dict, float, asyncio.Future] | None = None
        self._seen_connection = False

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    # -- ProgramSource -------------------------------------------------------

    async def wait_connected(self, timeout_seconds: float) -> bool:
        if self._connected.is_set():
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        return True

    async def get_program(self, step: int, payload: dict, deadline_at: float):
        fut = asyncio.get_running_loop().create_future()
        self._pending = (step, payload, deadline_at, fut)
        self._seen_connection = self.connected
        try:
            if self.connected:
                await self._send_pending()
            remaining = deadline_at - time.monotonic()
            try:
                return await asyncio.wait_for(fut, max(0.0, remaining))
            except (asyncio.TimeoutError, TimeoutError):
                return None, ("timeout" if self._seen_connection
                              else "disconnected")
        finally:
            self._pending = None

    async def _send_pending(self) -> None:
        ws = self.ws
        if ws is None or ws.closed or self._pending is None:
            return
        step, payload, deadline_at, _fut = self._pending
        message = dict(payload)
        message["deadline_seconds"] = max(0.0, deadline_at - time.monotonic())
        try:
            await ws.send_str(json.dumps(message))
        except Exception:
            pass  # the handler's finally clears the socket; deadline rules

    def deliver(self, data) -> None:
        """Route one decoded client message to the pending step."""
        if self._pending is None:
            return
        step, _payload, _deadline, fut = self._pending
        code, cause = validate_program_message(data, step)
        if cause == "wrong_step":
            self.wrong_step_count += 1
            if self.wrong_step_count == 1:
                print(f"seat {self.slot} ({self.name}): first wrong-step "
                      f"reply (got {data.get('step')!r}, pending {step})",
                      file=sys.stderr)
            return
        if not fut.done():
            fut.set_result((code, cause))

    def deliver_malformed(self) -> None:
        if self._pending is None:
            return
        fut = self._pending[3]
        if not fut.done():
            fut.set_result((None, "malformed"))

    # -- connection lifecycle -----------------------------------------------

    async def attach(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        self.ever_connected = True
        self._connected.set()
        if self.welcome is not None:
            await ws.send_str(json.dumps(self.welcome))
        if self._pending is not None:
            self._seen_connection = True
            await self._send_pending()

    def detach(self, ws: web.WebSocketResponse) -> None:
        if self.ws is ws:
            self.ws = None


def default_session_factory(slot: int, endpoint: Endpoint,
                            config: GameConfig) -> Session:
    return FactorioSession(slot, endpoint.host, endpoint.rcon_port, config)


class GameServer:
    def __init__(self, config: GameConfig, *,
                 results_uri: str | None = None,
                 save_replay_uri: str | None = None,
                 player_failure_uri: str | None = None,
                 session_factory: SessionFactory = default_session_factory,
                 factorio_manager: FactorioServerManager | None = None):
        self.config = config
        self.results_uri = results_uri
        self.save_replay_uri = save_replay_uri
        self.player_failure_uri = player_failure_uri
        self.session_factory = session_factory
        self.factorio = factorio_manager
        self.seats = [WsSeat(slot, p.name)
                      for slot, p in enumerate(config.players)]
        self.sessions: list[Session] = []
        self.engine: Engine | None = None
        self.result: EpisodeResult | None = None
        self.results_doc: dict | None = None
        self.task_info: dict = {"key": config.task, "goal_description": "",
                                "agent_instructions": None}
        # welcome/observations can only flow once sessions are up
        self._seats_ready = asyncio.Event()
        self._global_wss: set[web.WebSocketResponse] = set()
        self._global_send_tasks: dict[web.WebSocketResponse, asyncio.Task] = {}
        self._stale_close_tasks: set[asyncio.Task] = set()
        self._reported_failure_slot: int | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._started_at = time.monotonic()

    # -- routes --------------------------------------------------------------

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/player", self._handle_player)
        app.router.add_get("/global", self._handle_global)
        app.router.add_get("/client/global", self._handle_global_client)
        app.router.add_get("/client/player", self._handle_player_client)
        return app

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    def _authorized_slot(self, request: web.Request) -> int:
        try:
            slot = int(request.query.get("slot", ""))
        except ValueError:
            raise web.HTTPForbidden(text="bad slot")
        if not 0 <= slot < len(self.seats):
            raise web.HTTPForbidden(text="bad slot")
        token = request.query.get("token", "")
        if not hmac.compare_digest(
                token.encode("utf-8"),
                self.config.tokens[slot].encode("utf-8")):
            raise web.HTTPForbidden(text="bad token")
        return slot

    async def _handle_global_client(self, request: web.Request) -> web.Response:
        return web.Response(text=GLOBAL_CLIENT_HTML, content_type="text/html")

    async def _handle_player_client(self, request: web.Request) -> web.Response:
        self._authorized_slot(request)
        return web.Response(text=PLAYER_CLIENT_HTML, content_type="text/html")

    def _status_snapshot(self) -> dict:
        engine = self.engine
        n = self.config.num_seats
        snapshot = {
            "type": contract.MSG_STATUS,
            "game_version": GAME_VERSION,
            "players": [s.name for s in self.seats],
            "task": self.task_info,
            "max_steps": self.config.max_steps,
            "steps": list(engine.current_steps) if engine else [0] * n,
            "scores": [float(o.production_score) for o in engine.outcomes]
            if engine else [0.0] * n,
            "done": self.results_doc is not None,
        }
        if self.results_doc is not None:
            snapshot["result"] = self.results_doc
        return snapshot

    async def _handle_global(self, request: web.Request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps(self._status_snapshot()))
        self._global_wss.add(ws)
        try:
            async for _msg in ws:
                pass  # broadcast-only
        finally:
            self._global_wss.discard(ws)
            self._global_send_tasks.pop(ws, None)
        return ws

    def _broadcast_global(self, payload: dict) -> None:
        if not self._global_wss:
            return
        message = json.dumps(payload)
        loop = asyncio.get_running_loop()
        for ws in tuple(self._global_wss):
            if ws.closed:
                continue
            prev = self._global_send_tasks.get(ws)
            if prev is not None and not prev.done():
                continue  # drop rather than interleave sends
            task = loop.create_task(self._global_send(ws, message))
            self._global_send_tasks[ws] = task
            task.add_done_callback(
                lambda t, ws=ws: self._discard_global_send(ws, t))

    def _discard_global_send(self, ws, task) -> None:
        if self._global_send_tasks.get(ws) is task:
            del self._global_send_tasks[ws]

    @staticmethod
    async def _global_send(ws: web.WebSocketResponse, message: str) -> None:
        try:
            await ws.send_str(message)
        except Exception:
            pass

    async def _handle_player(self, request: web.Request):
        slot = self._authorized_slot(request)
        seat = self.seats[slot]
        if seat.connected:
            print(f"seat {slot} ({seat.name}): rejected duplicate "
                  f"connection (409)", file=sys.stderr)
            raise web.HTTPConflict(text="slot already connected")

        ws = web.WebSocketResponse(heartbeat=PLAYER_WS_HEARTBEAT_SECONDS)
        await ws.prepare(request)
        if seat.connected:
            await ws.close(code=WSCloseCode.POLICY_VIOLATION,
                           message=b"slot already connected")
            return ws
        # Claim the seat immediately (409 for duplicates), but welcome
        # only once the FLE sessions are up (api_docs comes from FLE).
        seat.ws = ws
        seat.ever_connected = True
        print(f"seat {slot} ({seat.name}) connected", file=sys.stderr)
        reader = asyncio.create_task(self._read_player(seat, ws))
        try:
            await self._seats_ready.wait()
            if seat.ws is ws and not ws.closed:
                await seat.attach(ws)
            await reader
        finally:
            reader.cancel()
            seat.detach(ws)
            print(f"seat {slot} ({seat.name}) disconnected", file=sys.stderr)
        return ws

    @staticmethod
    async def _read_player(seat: WsSeat, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                seat.deliver_malformed()
                continue
            seat.deliver(data)

    # -- episode orchestration -----------------------------------------------

    async def run_episode(self) -> EpisodeResult:
        cfg = self.config
        writer = ReplayWriter(cfg, self.task_info)
        try:
            await self._start_sessions(writer)
        except Exception as exc:
            import traceback
            print(f"host failure before play: {type(exc).__name__}: {exc}; "
                  f"writing fault artifacts", file=sys.stderr)
            traceback.print_exc()
            self._seats_ready.set()
            await self._write_fault_artifacts(writer, None)
            await self._shutdown_sessions()
            raise
        finally:
            self._seats_ready.set()

        engine = Engine(
            cfg, self.seats, self.sessions, self._executor,
            on_step=writer.append_step,
            on_seat_finished=lambda slot, o: writer.set_seat_throughput(
                slot, o.throughput),
            on_progress=self._on_progress,
            on_seat_dead=self._on_seat_dead,
            on_never_connected=self._report_player_failure)
        self.engine = engine
        try:
            result = await engine.run()
        except Exception as exc:
            print(f"unexpected engine failure: {type(exc).__name__}: {exc}; "
                  f"writing fault artifacts", file=sys.stderr)
            await self._write_fault_artifacts(writer, engine.outcomes)
            await self._shutdown_sessions()
            raise
        self.result = result
        self._log_seat_degrades(result)
        self._log_pacing(result, engine)
        doc = results_doc(cfg, result)
        self.results_doc = doc

        write_errors: list[str] = []

        async def attempt(label, uri, data, content_type):
            if not uri:
                return
            try:
                await uris.write_uri(uri, data, content_type)
            except Exception as exc:
                write_errors.append(f"{label} -> {uri}: {exc}")

        # Done broadcast FIRST (players must not wait out artifact retries).
        await self._broadcast_done(doc)
        await attempt("results", self.results_uri,
                      (json.dumps(doc, indent=2) + "\n").encode("utf-8"),
                      "application/json")
        await attempt("replay", self.save_replay_uri, writer.finalize(doc),
                      "application/json")
        await self._shutdown_sessions()
        if write_errors:
            raise IOError("artifact writes failed: " + "; ".join(write_errors))
        return result

    async def _start_sessions(self, writer: ReplayWriter) -> None:
        cfg = self.config
        if self.factorio is None:
            self.factorio = FactorioServerManager(cfg.num_seats)
        endpoints = await self.factorio.start()
        self._executor = ThreadPoolExecutor(
            max_workers=cfg.num_seats, thread_name_prefix="fle-seat")
        loop = asyncio.get_running_loop()
        self.sessions = [self.session_factory(slot, endpoints[slot], cfg)
                         for slot in range(cfg.num_seats)]
        await asyncio.wait_for(
            asyncio.gather(*(loop.run_in_executor(self._executor, s.start)
                             for s in self.sessions)),
            SESSION_START_TIMEOUT_SECONDS)
        first = self.sessions[0]
        self.task_info = await loop.run_in_executor(
            self._executor, first.task_info)
        writer.task = {"key": self.task_info["key"],
                       "goal_description": self.task_info["goal_description"]}
        writer.set_terrain(await loop.run_in_executor(
            self._executor, first.capture_terrain))
        prompts = await asyncio.gather(*(
            loop.run_in_executor(self._executor, s.system_prompt)
            for s in self.sessions))
        starting_inventory = await loop.run_in_executor(
            self._executor, first.starting_inventory)
        for seat, prompt in zip(self.seats, prompts):
            seat.welcome = {
                "type": contract.MSG_WELCOME,
                "protocol": PROTOCOL,
                "game_version": GAME_VERSION,
                "slot": seat.slot,
                "name": seat.name,
                "task": self.task_info,
                "max_steps": cfg.max_steps,
                "step_deadline_seconds": cfg.step_deadline_seconds,
                "program_timeout_seconds": cfg.program_timeout_seconds,
                "api_docs": prompt,
                # Episode parameters stated outright at t=0 (policies must
                # never infer them from play).
                "episode": {
                    "game_version": GAME_VERSION,
                    "variant_task_key": cfg.task,
                    "max_steps": cfg.max_steps,
                    "step_deadline_seconds": cfg.step_deadline_seconds,
                    "program_timeout_seconds": cfg.program_timeout_seconds,
                    "strike_limit": cfg.strike_limit,
                    "seats": cfg.num_seats,
                    "slot": seat.slot,
                    "map_bounds": dict(writer.terrain["bounds"]),
                    "starting_inventory": dict(starting_inventory),
                    "fast": cfg.fast,
                    "game_speed": cfg.game_speed,
                },
            }
        self._seats_ready.set()
        print(f"sessions ready ({cfg.num_seats} seats, task {cfg.task}); "
              f"terrain: {len(writer.terrain['resources'])} resource tiles",
              file=sys.stderr, flush=True)

    async def _shutdown_sessions(self) -> None:
        loop = asyncio.get_running_loop()
        for session in self.sessions:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, session.close), 10.0)
            except Exception as exc:
                print(f"session close failed: {exc!r}", file=sys.stderr)
        self.sessions = []
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self.factorio is not None:
            try:
                await loop.run_in_executor(None, self.factorio.stop)
            except Exception as exc:
                print(f"factorio stop failed: {exc!r}", file=sys.stderr)

    def _on_progress(self, slot: int, step: int, score: float) -> None:
        self._broadcast_global({"type": contract.MSG_PROGRESS, "slot": slot,
                                "step": step, "score": score})

    def _on_seat_dead(self, slot: int) -> None:
        """Strike-death: the seat's loop is over; close its socket so the
        client sees the end (it will get `done` only if reconnected)."""
        seat = self.seats[slot]
        ws = seat.ws
        if ws is None or ws.closed:
            return

        async def close_stale() -> None:
            try:
                await ws.close(code=WSCloseCode.GOING_AWAY,
                               message=b"seat marked dead (strike rule)")
            except Exception:
                pass

        task = asyncio.get_running_loop().create_task(close_stale())
        self._stale_close_tasks.add(task)
        task.add_done_callback(self._stale_close_tasks.discard)

    def _log_pacing(self, result: EpisodeResult, engine: Engine) -> None:
        """One end-of-episode pacing line: per-seat steps/noops/dead,
        wall clock used vs budget, per-step program eval time p50/max."""
        cfg = self.config
        seats = " ".join(
            f"s{slot}:{o.steps_completed}+{o.noop_steps}noop"
            f"{'/dead' if o.dead else ''}{'/fault' if o.faulted else ''}"
            for slot, o in enumerate(result.seats))
        times = sorted(engine.eval_wall_ms)
        p50 = times[len(times) // 2] if times else 0
        mx = times[-1] if times else 0
        print(f"pacing: end_reason={result.end_reason} seats[{seats}] "
              f"wall={result.wall_clock_seconds:.0f}s/"
              f"{cfg.wall_clock_budget_seconds:.0f}s "
              f"eval_ms p50={p50} max={mx} n={len(times)}",
              file=sys.stderr, flush=True)

    def _log_seat_degrades(self, result: EpisodeResult) -> None:
        for slot, o in enumerate(result.seats):
            if o.noop_steps or o.dead or o.faulted:
                print(f"seat {slot} ({self.seats[slot].name}): "
                      f"{o.noop_steps} noop steps, {o.error_steps} error "
                      f"steps, dead={o.dead}, faulted={o.faulted}",
                      file=sys.stderr)

    async def _write_fault_artifacts(self, writer: ReplayWriter,
                                     outcomes: tuple[SeatOutcome, ...] | None
                                     ) -> None:
        doc = fault_results_doc(
            self.config, time.monotonic() - self._started_at, outcomes)
        self.results_doc = doc
        for label, uri, data, ctype in (
                ("results", self.results_uri,
                 (json.dumps(doc, indent=2) + "\n").encode("utf-8"),
                 "application/json"),
                ("replay", self.save_replay_uri, writer.finalize(doc),
                 "application/json")):
            if not uri:
                continue
            try:
                await uris.write_uri(uri, data, ctype)
            except Exception as exc:
                print(f"fault-artifact write failed: {label} -> {uri}: {exc}",
                      file=sys.stderr)
        try:
            await self._broadcast_done(doc)
        except Exception as exc:
            print(f"fault done-broadcast failed: {exc}", file=sys.stderr)

    async def _report_player_failure(self, slot: int) -> None:
        """Declare a never-connected seat to COGAME_PLAYER_FAILURE_URI.

        The URI holds ONE GamePlayerFailure document; with several
        no-shows the lowest slot wins (a later lower-slot report
        overwrites, a higher one is skipped).
        """
        seat = self.seats[slot]
        if seat.ever_connected:
            return
        if self._reported_failure_slot is not None \
                and self._reported_failure_slot <= slot:
            return
        self._reported_failure_slot = slot
        if not self.player_failure_uri:
            return
        payload = {
            "message": (
                f"player '{seat.name}' in slot {slot} did not connect within "
                f"{self.config.player_connect_timeout_seconds:g}s "
                f"(reason: connect_timeout); seat plays noops unless it "
                f"connects later"),
            "failed_policy_index": slot,
        }
        try:
            await uris.write_uri(self.player_failure_uri,
                                 json.dumps(payload).encode("utf-8"),
                                 "application/json")
        except Exception as exc:
            print(f"player-failure report failed: {exc}", file=sys.stderr)

    async def _broadcast_done(self, doc: dict) -> None:
        message = json.dumps({"type": contract.MSG_DONE, "result": doc})

        async def _send(ws: web.WebSocketResponse) -> None:
            await ws.send_str(message)
            await ws.close()

        async def send_and_close(seat: WsSeat) -> None:
            ws = seat.ws
            if ws is None or ws.closed:
                return
            try:
                await asyncio.wait_for(_send(ws), DONE_SEND_TIMEOUT_SECONDS)
            except Exception:
                pass

        async def send_and_close_global(ws: web.WebSocketResponse) -> None:
            prev = self._global_send_tasks.get(ws)
            if prev is not None and not prev.done():
                try:
                    await asyncio.wait_for(asyncio.shield(prev),
                                           DONE_SEND_TIMEOUT_SECONDS)
                except Exception:
                    pass
            try:
                await asyncio.wait_for(_send(ws), DONE_SEND_TIMEOUT_SECONDS)
            except Exception:
                pass

        await asyncio.gather(
            *(send_and_close(s) for s in self.seats),
            *(send_and_close_global(ws) for ws in tuple(self._global_wss)
              if not ws.closed),
            return_exceptions=True)


# -- replay mode -------------------------------------------------------------

DEFAULT_VIEWER_DIST = Path(__file__).resolve().parents[2] / "viewer" / "dist"

REPLAY_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>cogame-factorio replay</title>
<style>
  body { font-family: ui-monospace, monospace; margin: 2rem; }
  dt { font-weight: bold; margin-top: .6rem; }
  .note { margin-top: 2rem; color: #666; }
</style>
</head>
<body>
<h1>cogame-factorio replay</h1>
<dl id="info">loading /replay-data ...</dl>
<p class="note">Placeholder viewer: this server was built without the
static replay viewer bundle (run viewer/build_viewer.sh).</p>
<script>
async function load() {
  const resp = await fetch("/replay-data");
  const doc = await resp.json();
  const info = document.getElementById("info");
  info.textContent = "";
  const add = (label, value) => {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    info.appendChild(dt);
    info.appendChild(dd);
  };
  add("players", doc.names.join(", "));
  add("task", doc.task.key);
  add("scores", doc.result.scores.join(", "));
  add("end_reason", doc.result.end_reason);
  add("steps", doc.seats.map(s => s.steps.length).join(", "));
}
load().catch(e => {
  document.getElementById("info").textContent = "failed: " + e.message;
});
</script>
</body>
</html>
"""


def make_replay_app(replay_bytes: bytes,
                    viewer_dist: Path | None = None) -> web.Application:
    """Replay-mode app: JSON at /replay-data, viewer at /client/replay/.

    Also serves a legacy ``GET /replay`` websocket emitting one header
    message: the certifier's replay-loadable probe (coworld<=0.1.34 runs
    it even with a static viewer bundle declared) needs one non-empty
    message from that route. Raises ReplayError on a corrupt document.
    """
    replay = Replay.parse(replay_bytes)
    dist = DEFAULT_VIEWER_DIST if viewer_dist is None else Path(viewer_dist)
    index = dist / "index.html"
    have_bundle = index.is_file()
    if not have_bundle:
        print(f"viewer bundle not found at {dist}; serving placeholder page",
              file=sys.stderr)

    async def handle_replay_data(request):
        return web.Response(body=replay_bytes, content_type="application/json")

    async def handle_replay_client(request):
        if have_bundle:
            raise web.HTTPFound("/client/replay/")
        return web.Response(text=REPLAY_PLACEHOLDER_HTML,
                            content_type="text/html")

    async def handle_replay_index(request):
        if have_bundle:
            return web.FileResponse(index)
        return web.Response(text=REPLAY_PLACEHOLDER_HTML,
                            content_type="text/html")

    async def handle_healthz(request):
        return web.json_response({"status": "ok"})

    async def handle_replay_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({
            "type": "replay_header",
            "format": replay.doc["format"],
            "version": replay.doc["version"],
            "names": replay.names,
            "task": replay.doc["task"],
            "result": replay.result,
        }))
        async for _msg in ws:
            pass
        return ws

    app = web.Application()
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_get("/replay", handle_replay_ws)
    app.router.add_get("/replay-data", handle_replay_data)
    app.router.add_get("/client/replay", handle_replay_client)
    app.router.add_get("/client/replay/", handle_replay_index)
    if have_bundle:
        app.router.add_static("/client/replay/", dist)
    return app


# -- process entry point -----------------------------------------------------

def _check_task(task_key: str) -> str | None:
    """Unknown FLE task keys are config errors (exit 2), checked here so
    a typo fails before Factorio is spawned. Skipped when FLE is not
    importable (server-only environments)."""
    try:
        from .session import check_task_key
        return check_task_key(task_key)
    except ImportError:
        return None


async def async_main() -> int:
    host = os.environ.get("COGAME_HOST", "0.0.0.0")
    port = int(os.environ.get("COGAME_PORT", "8080"))

    load_replay_uri = os.environ.get("COGAME_LOAD_REPLAY_URI", "")
    if load_replay_uri:
        replay_bytes = await uris.read_uri(load_replay_uri)
        try:
            app = make_replay_app(replay_bytes)
        except ReplayError as exc:
            print(f"invalid replay at {load_replay_uri}: {exc}", file=sys.stderr)
            return 2
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        print(f"cogame-factorio replay mode on {host}:{port} "
              f"({len(replay_bytes)} replay bytes)", file=sys.stderr)
        await asyncio.Event().wait()
        return 0

    config_uri = os.environ.get("COGAME_CONFIG_URI", "")
    if not config_uri:
        print("COGAME_CONFIG_URI is required", file=sys.stderr)
        return 2
    try:
        if uris.local_path(config_uri) is not None:
            config = GameConfig.from_file_uri(config_uri)
        else:
            raw = await uris.read_uri(config_uri)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(
                    f"config at {config_uri} is not valid JSON: {exc}") from exc
            config = GameConfig.from_dict(data)
    except ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"cannot read config from {config_uri}: {exc}", file=sys.stderr)
        return 2
    task_error = _check_task(config.task)
    if task_error:
        print(f"invalid config: task {config.task!r}: {task_error}",
              file=sys.stderr)
        return 2

    server = GameServer(
        config,
        results_uri=os.environ.get("COGAME_RESULTS_URI"),
        save_replay_uri=os.environ.get("COGAME_SAVE_REPLAY_URI"),
        player_failure_uri=os.environ.get("COGAME_PLAYER_FAILURE_URI"),
    )
    runner = web.AppRunner(server.make_app())
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    print(f"cogame-factorio serving on {host}:{port} "
          f"({config.num_seats} seats, task {config.task}, "
          f"{config.max_steps} steps)", file=sys.stderr, flush=True)
    try:
        result = await server.run_episode()
    except FactorioError as exc:
        print(f"factorio failure: {exc}", file=sys.stderr)
        await runner.cleanup()
        return 1
    except Exception as exc:
        # Host failure (FLE session start, engine crash, artifact writes):
        # fault artifacts were attempted by run_episode where possible.
        print(f"episode failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        await runner.cleanup()
        return 1
    print(f"episode over: end_reason={result.end_reason} "
          f"scores={[o.score for o in result.seats]} "
          f"wall={result.wall_clock_seconds:.0f}s", file=sys.stderr)
    await asyncio.sleep(SHUTDOWN_GRACE_SECONDS)
    await runner.cleanup()
    return 0


def main() -> int:
    code = asyncio.run(async_main())
    sys.stdout.flush()
    sys.stderr.flush()
    # FLE's timed-out programs keep running on its executor threads and
    # its atexit cleanup joins them: a hung `sleep(...)` program would
    # otherwise stall process exit. Sessions and Factorio children are
    # already shut down explicitly, so hard-exit here.
    os._exit(code)


if __name__ == "__main__":
    main()
