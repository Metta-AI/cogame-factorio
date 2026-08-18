"""Reusable async player harness for cogame-factorio websocket seats.

Speaks the ``cogame.factorio.v1`` protocol (see ``docs/PROTOCOL.md``), one
JSON text message per step each way:

    server -> player  {"type": "welcome", "slot": 0, "task": {...},
                       "episode": {...}, "api_docs": "..."}   once per connection
    server -> player  {"type": "observation", "step": k,
                       "deadline_seconds": 60, "observation": {...}}
    player -> server  {"type": "program", "step": k, "code": "<python source>"}
    server -> player  {"type": "done", "result": {...}}       episode end

The websocket URL comes from an explicit argument or, failing that, the
``COWORLD_PLAYER_WS_URL`` / ``COGAMES_ENGINE_WS_URL`` environment variables.

A policy is a :class:`Policy`: ``on_welcome(welcome)`` (called on every
(re)connection), ``program(step, observation) -> str`` (Python source run
against the seat's FLE namespace) and ``on_done(result)``. ``program`` runs
in a worker thread so a slow policy (e.g. an LLM call) never blocks the
websocket heartbeat; if it has not returned by the step deadline the
harness answers ``pass`` for that step (a valid no-op program, *not* a
strike) and logs it. A policy that raises is likewise answered with
``pass`` — a policy bug must never strike the seat out.

Reconnects: the server allows a seat to reconnect any number of times and
re-sends ``welcome`` plus the *current* observation, so transient drops are
retried with a bounded number of consecutive attempts (a connection that
answered at least one step resets the budget). Before the first successful
connection a 403 (bad slot/token) is fatal (exit 1) and connection refusals
burn the bounded budget (then exit 1). Once the seat *has* connected, a
403 or a refused connection means the server has finished and gone away
(it closes every socket after ``done`` and exits): the harness then
returns promptly with an empty result and exit 0 instead of hanging — a
player container must always exit. A 409 (slot occupied) usually means our
own previous connection has not been reaped yet, so it is retried within
the bounded budget.

Telemetry (best effort, never affects play): when
``COWORLD_PLAYER_ARTIFACT_UPLOAD_URL`` is set, one zip per episode
(``meta.json``, ``events.jsonl``, ``summary.json``) is written to a
``file://...zip`` URL or PUT to an ``http(s)`` URL when ``done`` arrives.
Any telemetry error disables telemetry for the rest of the episode.

Only aiohttp is required (stdlib otherwise).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import aiohttp
from aiohttp import WSMsgType

# Wire strings: prefer the server's zero-dependency contract module when
# it is importable (the Docker image has PYTHONPATH=/workspace/server), so
# a rename there is caught by the four-surface rule; fall back to a local
# copy so the harness also runs outside the image.
try:  # pragma: no cover - which branch runs depends on the environment
    from cogame_factorio import contract as _contract  # type: ignore

    PROTOCOL = _contract.PROTOCOL
    MSG_WELCOME = _contract.MSG_WELCOME
    MSG_OBSERVATION = _contract.MSG_OBSERVATION
    MSG_PROGRAM = _contract.MSG_PROGRAM
    MSG_DONE = _contract.MSG_DONE
    WS_URL_ENV_VARS = (_contract.ENV_PLAYER_WS_URL,
                       _contract.ENV_PLAYER_WS_URL_LEGACY)
    MAX_CODE_CHARS = _contract.MAX_CODE_CHARS
except Exception:  # noqa: BLE001 - ImportError or a partial module
    PROTOCOL = "cogame.factorio.v1"
    MSG_WELCOME = "welcome"
    MSG_OBSERVATION = "observation"
    MSG_PROGRAM = "program"
    MSG_DONE = "done"
    WS_URL_ENV_VARS = ("COWORLD_PLAYER_WS_URL", "COGAMES_ENGINE_WS_URL")
    MAX_CODE_CHARS = 64 * 1024

ARTIFACT_URL_ENV_VAR = "COWORLD_PLAYER_ARTIFACT_UPLOAD_URL"

DEFAULT_MAX_CONNECT_ATTEMPTS = 5
DEFAULT_RECONNECT_DELAY_SECONDS = 0.5

# Bound on establishing one websocket connection (TCP + handshake): a
# black-holed connect must fail fast instead of eating minutes of the
# reconnect budget. Applied via the session ClientTimeout (total=None so
# the long-lived episode websocket itself is never killed).
CONNECT_TIMEOUT_SECONDS = 20.0

# Safety margin subtracted from the server's step deadline when bounding
# the policy call: the reply still has to cross the wire.
DEADLINE_MARGIN_SECONDS = 3.0

# The program the harness sends when the policy fails or overruns.
NOOP_PROGRAM = "pass"

# Handshake statuses that can never succeed on retry (before the seat has
# ever connected). 409 (slot already connected) is deliberately NOT here:
# it is usually this seat's own stale previous connection, which the
# server reaps (ws heartbeat), so 409s are retried on the bounded budget.
_FATAL_HTTP_STATUSES = {
    403: "connection rejected (403): bad slot or token",
}


class PlayerError(Exception):
    """Fatal player-side failure (bad auth, server never reachable, bad env)."""


class Policy(ABC):
    """A cogame-factorio policy: one Python program per step.

    Subclass and implement :meth:`program`; override the hooks as needed.
    The three methods are called in order, never concurrently — except
    that a ``program`` call which overruns the step deadline keeps running
    in the background while the harness moves on (its result is discarded).
    """

    def on_welcome(self, welcome: dict) -> None:
        """Called with the ``welcome`` message on every (re)connection."""

    @abstractmethod
    def program(self, step: int, observation: dict) -> str:
        """Return the Python source to run for ``step``.

        ``observation`` is the ``observation`` object of the message
        (``raw_text``, ``entities``, ``inventory``, ``flows``, ``score``,
        ``last_program``, ...). Return a ``str``; anything else is logged
        and replaced by ``pass``.
        """

    def on_done(self, result: dict) -> None:
        """Called once with the episode ``result`` before the harness exits."""


class FunctionPolicy(Policy):
    """Adapts a plain ``fn(step, observation) -> str`` callable to a Policy."""

    def __init__(self, fn: Callable[[int, dict], str]):
        self._fn = fn

    def program(self, step: int, observation: dict) -> str:
        return self._fn(step, observation)


def ws_url_from_env() -> str:
    """The seat websocket URL from the environment (first env var wins)."""
    for name in WS_URL_ENV_VARS:
        url = os.environ.get(name)
        if url:
            return url
    raise PlayerError(
        "no websocket URL: set " + " or ".join(WS_URL_ENV_VARS))


def _log(msg: str) -> None:
    print(f"player: {msg}", file=sys.stderr, flush=True)


# -- telemetry ------------------------------------------------------------------

class Telemetry:
    """Per-episode artifact zip (meta.json, events.jsonl, summary.json).

    Every method swallows its own errors and disables itself: telemetry
    can never fail the episode. ``upload_url`` None disables it outright.
    """

    def __init__(self, upload_url: str | None, policy_module: str):
        self.url = upload_url or None
        self.enabled = bool(self.url)
        self.meta: dict = {"policy_module": policy_module,
                           "started_at": time.time()}
        self.events: list[dict] = []
        self._pending: dict | None = None  # step awaiting its output/score
        self.connections = 0
        self.uploaded = False

    def _disable(self, why: str, exc: Exception) -> None:
        if self.enabled:
            _log(f"telemetry disabled ({why}): {exc!r}")
        self.enabled = False

    def on_welcome(self, welcome: dict) -> None:
        if not self.enabled:
            return
        try:
            self.connections += 1
            episode = welcome.get("episode")
            self.meta.update({
                "slot": welcome.get("slot"),
                "name": welcome.get("name"),
                "game_version": welcome.get("game_version"),
                "protocol": welcome.get("protocol"),
                "task": welcome.get("task"),
                "episode": episode if isinstance(episode, dict) else {
                    k: welcome.get(k) for k in (
                        "max_steps", "step_deadline_seconds",
                        "program_timeout_seconds")},
                "connections": self.connections,
            })
        except Exception as exc:  # noqa: BLE001
            self._disable("on_welcome", exc)

    def on_observation(self, step: int, observation: dict) -> None:
        """Close out the previous step with its output/score, open ``step``."""
        if not self.enabled:
            return
        try:
            self._close_pending(observation)
            self._pending = {"step": step, "score_before": observation.get("score"),
                             "t0": time.monotonic()}
        except Exception as exc:  # noqa: BLE001
            self._disable("on_observation", exc)

    def on_program(self, step: int, code: str, harness_noop: bool) -> None:
        if not self.enabled:
            return
        try:
            p = self._pending
            if p is None or p.get("step") != step:
                p = self._pending = {"step": step, "t0": time.monotonic()}
            p["code"] = code
            p["wall_ms"] = int((time.monotonic() - p["t0"]) * 1000)
            p["harness_noop"] = harness_noop
        except Exception as exc:  # noqa: BLE001
            self._disable("on_program", exc)

    def _close_pending(self, observation: dict | None) -> None:
        p = self._pending
        if p is None:
            return
        self._pending = None
        last = (observation or {}).get("last_program")
        if isinstance(last, dict):
            p["output"] = last.get("output")
            p["error"] = last.get("error")
        else:
            p.setdefault("output", None)
            p.setdefault("error", None)
        p["score"] = (observation or {}).get("score")
        p.pop("t0", None)
        p.pop("score_before", None)
        self.events.append(p)

    def on_done(self, result: dict) -> None:
        if not self.enabled:
            return
        try:
            self._close_pending(None)
            self.result = result
        except Exception as exc:  # noqa: BLE001
            self._disable("on_done", exc)

    def build_zip(self) -> bytes:
        summary = {
            "steps_answered": len(self.events),
            "harness_noops": sum(1 for e in self.events if e.get("harness_noop")),
            "error_steps": sum(1 for e in self.events if e.get("error")),
            "final_score": next((e.get("score") for e in reversed(self.events)
                                 if e.get("score") is not None), None),
            "result": getattr(self, "result", None),
            "connections": self.connections,
            "finished_at": time.time(),
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.json", json.dumps(self.meta, default=str, indent=1))
            zf.writestr("events.jsonl", "".join(
                json.dumps(e, default=str) + "\n" for e in self.events))
            zf.writestr("summary.json", json.dumps(summary, default=str, indent=1))
        return buf.getvalue()

    async def upload(self, session: aiohttp.ClientSession | None = None) -> bool:
        """Write/PUT the zip once. Returns True on success; never raises."""
        if not self.enabled or self.uploaded:
            return False
        try:
            data = self.build_zip()
            parsed = urlparse(self.url)
            if parsed.scheme == "file":
                path = Path(unquote(parsed.path))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            elif parsed.scheme in ("http", "https"):
                own = session is None
                if own:
                    session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=60))
                try:
                    async with session.put(
                            self.url, data=data,
                            headers={"Content-Type": "application/zip"}) as resp:
                        if resp.status >= 300:
                            raise RuntimeError(f"upload HTTP {resp.status}")
                finally:
                    if own:
                        await session.close()
            else:
                raise ValueError(f"unsupported artifact URL scheme {parsed.scheme!r}")
            self.uploaded = True
            _log(f"telemetry uploaded ({len(data)} bytes) to {self.url}")
            return True
        except Exception as exc:  # noqa: BLE001
            self._disable("upload", exc)
            return False


# -- episode loop --------------------------------------------------------------

async def play_episode(
        policy: Policy,
        url: str | None = None,
        *,
        max_connect_attempts: int = DEFAULT_MAX_CONNECT_ATTEMPTS,
        reconnect_delay_seconds: float = DEFAULT_RECONNECT_DELAY_SECONDS,
        deadline_margin_seconds: float = DEADLINE_MARGIN_SECONDS,
        telemetry: Telemetry | None = None,
) -> dict:
    """Play one episode; returns the ``result`` from the done message.

    ``max_connect_attempts`` bounds *consecutive* failed connection
    attempts (or connections dropped before answering any step); a
    connection that answered at least one step resets the budget. Once
    the seat has connected at least once, a refused connection or a 403
    on reconnect means the server has finished: returns ``{}`` (exit 0).
    """
    if url is None:
        url = ws_url_from_env()
    if telemetry is None:
        telemetry = Telemetry(os.environ.get(ARTIFACT_URL_ENV_VAR),
                              type(policy).__module__)

    failures = 0
    total_answered = 0
    ever_connected = False

    def _fail(reason: str, exc: Exception | None = None):
        nonlocal failures
        failures += 1
        _log(f"connection attempt failed "
             f"({failures}/{max_connect_attempts} consecutive): {reason}; "
             f"{total_answered} steps answered so far")
        if failures >= max_connect_attempts:
            raise PlayerError(
                f"giving up after {failures} consecutive failed "
                f"connection attempts: {reason}") from exc

    def _server_gone(reason: str) -> dict:
        _log(f"server gone after the seat had connected ({reason}); "
             f"{total_answered} steps answered; exiting cleanly")
        return {}

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=CONNECT_TIMEOUT_SECONDS,
        sock_connect=CONNECT_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            while True:
                try:
                    ws = await session.ws_connect(url, heartbeat=20.0)
                except aiohttp.WSServerHandshakeError as exc:
                    if exc.status in _FATAL_HTTP_STATUSES:
                        if ever_connected:
                            return _server_gone(f"HTTP {exc.status}")
                        raise PlayerError(
                            _FATAL_HTTP_STATUSES[exc.status]) from exc
                    _fail(f"handshake failed with status {exc.status}", exc)
                    await asyncio.sleep(reconnect_delay_seconds)
                    continue
                except (aiohttp.ClientConnectorError, ConnectionRefusedError) as exc:
                    if ever_connected:
                        return _server_gone(f"connection refused: {exc}")
                    _fail(str(exc), exc)
                    await asyncio.sleep(reconnect_delay_seconds)
                    continue
                except (aiohttp.ClientError, OSError) as exc:
                    _fail(str(exc), exc)
                    await asyncio.sleep(reconnect_delay_seconds)
                    continue

                ever_connected = True
                try:
                    result, answered = await _play_connection(
                        ws, policy, deadline_margin_seconds, telemetry)
                finally:
                    try:
                        await ws.close()
                    except Exception:
                        # A close failure after the done message must never
                        # turn a completed episode into a player failure.
                        pass
                total_answered += answered
                if result is not None:
                    return result
                # connection dropped without a done message
                if answered > 0:
                    failures = 0  # made progress: fresh reconnect budget
                _fail("connection closed before the done message")
                await asyncio.sleep(reconnect_delay_seconds)
        finally:
            # Best effort, whatever way the episode ended.
            await telemetry.upload(session)


async def _call_program(
        policy: Policy, step: int, observation: dict,
        deadline: float | None) -> tuple[str, bool]:
    """Run ``policy.program`` off-loop, bounded by ``deadline`` seconds.

    Returns ``(code, harness_noop)``; ``harness_noop`` is True when the
    policy raised, overran, or returned a non-string and ``NOOP_PROGRAM``
    was substituted (logged).
    """
    coro = asyncio.to_thread(policy.program, step, observation)
    try:
        if deadline is not None:
            code = await asyncio.wait_for(coro, timeout=deadline)
        else:
            code = await coro
    except asyncio.TimeoutError:
        _log(f"policy.program overran the deadline at step {step}; "
             f"answering {NOOP_PROGRAM!r}")
        return NOOP_PROGRAM, True
    except Exception as exc:  # noqa: BLE001 - a policy bug is not fatal
        _log(f"policy.program raised at step {step}: {exc!r}; "
             f"answering {NOOP_PROGRAM!r}")
        return NOOP_PROGRAM, True
    if not isinstance(code, str):
        _log(f"policy.program returned {type(code).__name__} at step "
             f"{step}, expected str; answering {NOOP_PROGRAM!r}")
        return NOOP_PROGRAM, True
    if len(code) > MAX_CODE_CHARS:
        _log(f"policy.program returned {len(code)} chars at step {step} "
             f"(limit {MAX_CODE_CHARS}); the server would noop it, "
             f"answering {NOOP_PROGRAM!r}")
        return NOOP_PROGRAM, True
    return code, False


def _policy_deadline(
        data: dict, welcome: dict | None, margin: float) -> float | None:
    raw = data.get("deadline_seconds")
    if raw is None and welcome is not None:
        raw = welcome.get("step_deadline_seconds")
        if raw is None and isinstance(welcome.get("episode"), dict):
            raw = welcome["episode"].get("step_deadline_seconds")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    return max(float(raw) - margin, 1.0)


async def _play_connection(
        ws: aiohttp.ClientWebSocketResponse, policy: Policy,
        deadline_margin_seconds: float, telemetry: Telemetry,
) -> tuple[dict | None, int]:
    """Answer steps on one connection until done or disconnect.

    Returns ``(result, steps_answered)``; result is None on disconnect.
    Malformed or unknown messages are logged and skipped, never fatal.
    """
    answered = 0
    welcome: dict | None = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                _log("ignoring non-JSON message")
                continue
            if not isinstance(data, dict):
                _log("ignoring non-object message")
                continue
            mtype = data.get("type")

            if mtype == MSG_WELCOME:
                welcome = data
                if data.get("protocol") not in (None, PROTOCOL):
                    _log(f"server protocol {data.get('protocol')!r} != "
                         f"{PROTOCOL!r}; continuing anyway")
                telemetry.on_welcome(data)
                try:
                    policy.on_welcome(data)
                except Exception as exc:  # noqa: BLE001
                    _log(f"policy.on_welcome raised: {exc!r}; ignoring")
                continue

            if mtype == MSG_DONE:
                result = data.get("result")
                if not isinstance(result, dict):
                    result = {}
                telemetry.on_done(result)
                try:
                    policy.on_done(result)
                except Exception as exc:  # noqa: BLE001
                    _log(f"policy.on_done raised: {exc!r}; ignoring")
                return result, answered

            if mtype == MSG_OBSERVATION:
                step = data.get("step")
                observation = data.get("observation")
                if not isinstance(step, int) or isinstance(step, bool):
                    _log("ignoring observation without an integer step")
                    continue
                if not isinstance(observation, dict):
                    _log(f"observation at step {step} has no observation "
                         f"object; answering with an empty one")
                    observation = {}
                telemetry.on_observation(step, observation)
                deadline = _policy_deadline(
                    data, welcome, deadline_margin_seconds)
                code, noop = await _call_program(
                    policy, step, observation, deadline)
                telemetry.on_program(step, code, noop)
                await ws.send_str(json.dumps(
                    {"type": MSG_PROGRAM, "step": step, "code": code}))
                answered += 1
                continue

            _log(f"ignoring message of unknown type {mtype!r}")
    except (aiohttp.ClientError, ConnectionError):
        pass  # dropped mid-episode: caller decides whether to reconnect
    return None, answered


def run_policy_main(policy_factory: Callable[[], Policy]) -> int:
    """Entry-point helper: build the policy and play one episode.

    Takes a zero-arg factory (not a policy) so env-parsing errors during
    policy construction also surface as clean exit codes. Returns a
    process exit code: 0 on a clean done message (or the server going
    away after the seat had played), 1 on fatal player errors (bad env
    config, bad auth, server never reachable), 130 on SIGINT.
    """
    try:
        policy = policy_factory()
        result = asyncio.run(play_episode(policy))
    except PlayerError as exc:
        print(f"player failed: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    print(f"episode done: result={json.dumps(result)}",
          file=sys.stderr, flush=True)
    return 0


def main_for(policy_factory: Callable[[], Policy]) -> None:
    """``if __name__ == "__main__": main_for(MyPolicy)`` — exits the process."""
    sys.exit(run_policy_main(policy_factory))


__all__ = [
    "Policy", "FunctionPolicy", "PlayerError", "Telemetry", "play_episode",
    "run_policy_main", "main_for", "ws_url_from_env", "NOOP_PROGRAM",
    "PROTOCOL", "WS_URL_ENV_VARS", "ARTIFACT_URL_ENV_VAR",
]
