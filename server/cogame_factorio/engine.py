"""Transport-free per-seat episode engine (docs/PROTOCOL.md).

Every seat runs its own asyncio task: wait for the seat to connect (or
the connect timeout), then ``max_steps`` rounds of observation -> program
-> execute -> record. Seats are independent (own Factorio server, own
pace); the episode ends when every seat loop has ended or the wall-clock
budget expires. Session calls (FLE, blocking) run on a thread pool, one
worker per seat.

Degrade, never hang: every wait is bounded (connect timeout, step
deadline, program timeout via FLE, wall clock); a bad or missing reply is
a noop + strike, ``strike_limit`` consecutive strikes end the seat's loop
(dead). Program errors are game outcomes, not faults. A session call that
raises ends *that seat's* loop as faulted and the episode reports
``end_reason: sim_fault`` (scores as of the fault, partial replay).
"""

from __future__ import annotations

import asyncio
import sys
import time
from concurrent.futures import Executor
from typing import Awaitable, Callable, Protocol, Sequence

from . import contract
from .config import GameConfig
from .results import (END_SIM_FAULT, END_STEPS_CAP, END_WALL_CLOCK,
                      NOOP_CAUSES, EpisodeResult, SeatOutcome)
from .session import Session

# Mid-episode stderr heartbeat: at most one line per interval.
PROGRESS_INTERVAL_SECONDS = 30.0

MAX_CODE_CHARS = contract.MAX_CODE_CHARS


class ProgramSource(Protocol):
    """Per-seat program provider (websocket seat, scripted fake, ...)."""

    wrong_step_count: int

    async def wait_connected(self, timeout_seconds: float) -> bool:
        """Block until the seat has connected (True) or timeout (False)."""
        ...

    async def get_program(self, step: int, payload: dict,
                          deadline_at: float) -> tuple[str | None, str | None]:
        """Send ``payload`` (the observation message) and wait for the
        step's program until ``deadline_at`` (time.monotonic()).

        Returns ``(code, None)`` for a valid reply, else ``(None, cause)``
        with ``cause`` in NOOP_CAUSES. Must never raise except on
        cancellation; a raise is recorded as ``host_error``.
        """
        ...


def validate_program_message(data, step: int) -> tuple[str | None, str | None]:
    """Classify a decoded client message for the pending ``step``.

    Returns ``(code, None)`` when valid, ``(None, "wrong_step")`` when it
    addresses another step (dropped; the step keeps waiting), or
    ``(None, "malformed")`` for anything else (immediate noop).
    """
    if not isinstance(data, dict) or data.get("type") != contract.MSG_PROGRAM:
        return None, "malformed"
    if data.get("step") != step or isinstance(data.get("step"), bool):
        return None, "wrong_step"
    code = data.get("code")
    if not isinstance(code, str) or len(code) > MAX_CODE_CHARS:
        return None, "malformed"
    return code, None


class Engine:
    def __init__(self, config: GameConfig, sources: Sequence[ProgramSource],
                 sessions: Sequence[Session], executor: Executor, *,
                 on_step: Callable[[int, dict], None] | None = None,
                 on_seat_finished: Callable[[int, SeatOutcome], None] | None = None,
                 on_progress: Callable[[int, int, float], None] | None = None,
                 on_seat_dead: Callable[[int], None] | None = None,
                 on_never_connected: Callable[[int], Awaitable[None]] | None = None,
                 progress_interval_seconds: float = PROGRESS_INTERVAL_SECONDS):
        n = config.num_seats
        if len(sources) != n or len(sessions) != n:
            raise ValueError(
                f"need {n} sources and sessions, got {len(sources)} / "
                f"{len(sessions)}")
        self._config = config
        self._sources = list(sources)
        self._sessions = list(sessions)
        self._executor = executor
        self._on_step = on_step
        self._on_seat_finished = on_seat_finished
        self._on_progress = on_progress
        self._on_seat_dead = on_seat_dead
        self._on_never_connected = on_never_connected
        self._progress_interval = progress_interval_seconds
        self.outcomes = tuple(SeatOutcome() for _ in range(n))
        self.current_steps = [0] * n
        self._start = 0.0
        self._wall_deadline = 0.0
        self._wall_expired = False
        self._finished = [False] * n
        # per-program execution wall times (pacing diagnostics)
        self.eval_wall_ms: list[int] = []

    # -- helpers -------------------------------------------------------------

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _log(self, slot: int, msg: str) -> None:
        print(f"seat {slot} ({self._config.players[slot].name}): {msg}",
              file=sys.stderr, flush=True)

    def _wall_remaining(self) -> float:
        return self._wall_deadline - time.monotonic()

    def _hook(self, hook, *args) -> None:
        if hook is None:
            return
        try:
            hook(*args)
        except Exception as exc:  # observers never crash the episode
            print(f"engine hook {getattr(hook, '__name__', hook)} raised "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)

    # -- run -----------------------------------------------------------------

    async def run(self) -> EpisodeResult:
        cfg = self._config
        self._start = time.monotonic()
        self._wall_deadline = self._start + cfg.wall_clock_budget_seconds
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            await asyncio.gather(*(self._run_seat(s) for s in range(cfg.num_seats)))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):
                pass
        wall = time.monotonic() - self._start
        if any(o.faulted for o in self.outcomes):
            end_reason = END_SIM_FAULT
        elif self._wall_expired:
            end_reason = END_WALL_CLOCK
        else:
            end_reason = END_STEPS_CAP
        return EpisodeResult(seats=tuple(self.outcomes), end_reason=end_reason,
                             wall_clock_seconds=wall)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._progress_interval)
            elapsed = time.monotonic() - self._start
            print(f"progress: steps {self.current_steps}/{self._config.max_steps}, "
                  f"elapsed {elapsed:.0f}s, "
                  f"noops={[o.noop_steps for o in self.outcomes]}, "
                  f"dead={[o.dead for o in self.outcomes]}",
                  file=sys.stderr, flush=True)

    async def _run_seat(self, slot: int) -> None:
        cfg = self._config
        seat = self.outcomes[slot]
        source = self._sources[slot]
        session = self._sessions[slot]
        try:
            await self._play_seat(slot, seat, source, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            seat.faulted = True
            self._log(slot, f"session fault: {type(exc).__name__}: {exc}")
        # Final tallies (best effort — the session may be the thing that
        # faulted).
        try:
            seat.production_score = float(await self._call(session.score))
            seat.final_tick = int(await self._call(session.ticks))
        except Exception as exc:
            self._log(slot, f"final score/tick read failed: {exc!r}")
        seat.noop_causes["wrong_step"] += int(
            getattr(source, "wrong_step_count", 0))
        self._finished[slot] = True
        self._hook(self._on_seat_finished, slot, seat)

    async def _play_seat(self, slot: int, seat: SeatOutcome,
                         source: ProgramSource, session: Session) -> None:
        cfg = self._config
        connect_wait = min(cfg.player_connect_timeout_seconds,
                           max(0.0, self._wall_remaining()))
        connected = await source.wait_connected(connect_wait)
        if not connected:
            self._log(slot, f"not connected after {connect_wait:g}s; "
                            f"loop starts anyway (noops until it connects)")
            if self._on_never_connected is not None:
                try:
                    await self._on_never_connected(slot)
                except Exception as exc:
                    self._log(slot, f"never-connected hook failed: {exc!r}")

        observed = await self._call(session.observe, None)
        strikes = 0
        for step in range(cfg.max_steps):
            self.current_steps[slot] = step
            remaining_wall = self._wall_remaining()
            if remaining_wall <= 0:
                self._wall_expired = True
                self._log(slot, f"wall-clock budget expired before step {step}")
                break
            deadline = min(cfg.step_deadline_seconds, remaining_wall)
            payload = {
                "type": contract.MSG_OBSERVATION,
                "step": step,
                "deadline_seconds": deadline,
                "observation": observed.observation,
            }
            deadline_at = time.monotonic() + deadline
            try:
                code, cause = await source.get_program(step, payload, deadline_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(slot, f"program source raised {type(exc).__name__}: "
                                f"{exc} (noop, host_error)")
                code, cause = None, "host_error"

            if code is None and self._wall_remaining() <= 0 \
                    and cause in ("timeout", "disconnected", None):
                # the wall clock, not the player, ended this wait
                self._wall_expired = True
                self._log(slot, f"wall-clock budget expired during step {step}")
                break

            last_program = None
            noop = code is None
            output, error, wall_ms = "", False, 0
            if not noop:
                strikes = 0
                result = await self._call(session.run_program, code)
                seat.steps_completed += 1
                if result.error:
                    seat.error_steps += 1
                output, error, wall_ms = result.output, result.error, result.wall_ms
                self.eval_wall_ms.append(int(wall_ms))
                last_program = {"code": code, "output": output, "error": error}
            else:
                if cause not in NOOP_CAUSES:
                    cause = "timeout"
                strikes += 1
                seat.noop_steps += 1
                seat.noop_causes[cause] += 1
                if seat.noop_causes[cause] == 1:
                    self._log(slot, f"first '{cause}' noop at step {step}")

            observed = await self._call(session.observe, last_program)
            record = {
                "step": step,
                "code": "" if noop else code,
                "noop": noop,
                "output": output,
                "error": error,
                "throughput": None,
                "wall_ms": wall_ms,
            }
            record.update(observed.snapshot)
            self._hook(self._on_step, slot, record)
            self._hook(self._on_progress, slot, step,
                       float(observed.snapshot.get("score", 0.0)))

            if strikes >= cfg.strike_limit:
                seat.dead = True
                self._log(slot, f"marked dead (strike rule) at step {step}")
                self._hook(self._on_seat_dead, slot)
                break
        else:
            self.current_steps[slot] = cfg.max_steps

        if session.is_throughput_task():
            self._log(slot, "measuring throughput (FLE holdout verification)")
            seat.throughput = await self._call(session.throughput)
            if seat.throughput is None:
                seat.throughput = 0.0
