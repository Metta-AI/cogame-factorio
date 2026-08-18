"""The results document (``COGAME_RESULTS_URI``) — a CLOSED schema.

Triple-sync rule (AGENTS.md): the key set produced here == the manifest
``results_schema`` == ``tools/ci/docker_smoke.sh`` expected keys, and the
``end_reason`` values == the schema enum. ``tests/test_manifest.py`` is
the tripwire.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

from . import contract
from .config import GameConfig

# Per-seat noop cause taxonomy (results `noop_causes`):
#   timeout       deadline elapsed with no valid reply while the seat was
#                 connected at some point during the step
#   malformed     a reply arrived but failed shape/size validation
#   wrong_step    replies addressed to a different step (message count;
#                 such a step itself ends as timeout unless a correct
#                 reply follows in time)
#   disconnected  the seat had no connection for the whole step
#   host_error    the transport raised
NOOP_CAUSES = contract.NOOP_CAUSES

EndReason = Literal["steps_cap", "wall_clock", "sim_fault"]
END_REASONS: tuple[str, ...] = get_args(EndReason)
assert END_REASONS == contract.END_REASONS
END_STEPS_CAP: EndReason = "steps_cap"
END_WALL_CLOCK: EndReason = "wall_clock"
END_SIM_FAULT: EndReason = "sim_fault"

RESULT_KEYS = frozenset(contract.RESULT_KEYS)


def zero_noop_causes() -> dict:
    return dict.fromkeys(NOOP_CAUSES, 0)


@dataclass
class SeatOutcome:
    """Per-seat tallies the engine accumulates while a seat plays."""
    production_score: float = 0.0
    throughput: float | None = None
    steps_completed: int = 0
    error_steps: int = 0
    noop_steps: int = 0
    dead: bool = False
    faulted: bool = False
    noop_causes: dict = field(default_factory=zero_noop_causes)
    final_tick: int = 0

    @property
    def score(self) -> float:
        # scores: production score (open play) or achieved throughput
        return self.throughput if self.throughput is not None \
            else self.production_score


@dataclass(frozen=True)
class EpisodeResult:
    seats: tuple[SeatOutcome, ...]
    end_reason: EndReason
    wall_clock_seconds: float


def results_doc(config: GameConfig, result: EpisodeResult) -> dict:
    seats = result.seats
    assert len(seats) == config.num_seats
    return {
        "names": [p.name for p in config.players],
        "scores": [float(s.score) for s in seats],
        "production_scores": [float(s.production_score) for s in seats],
        "throughputs": [None if s.throughput is None else float(s.throughput)
                        for s in seats],
        "task_key": config.task,
        "steps_completed": [int(s.steps_completed) for s in seats],
        "error_steps": [int(s.error_steps) for s in seats],
        "noop_steps": [int(s.noop_steps) for s in seats],
        "dead_seats": [bool(s.dead) for s in seats],
        "noop_causes": [dict(s.noop_causes) for s in seats],
        "final_ticks": [int(s.final_tick) for s in seats],
        "end_reason": result.end_reason,
        "wall_clock_seconds": float(result.wall_clock_seconds),
    }


def fault_results_doc(config: GameConfig, wall_clock_seconds: float,
                      seats: tuple[SeatOutcome, ...] | None = None) -> dict:
    """A schema-complete results doc for an episode the host lost
    (Factorio never came up, engine crashed): end_reason sim_fault with
    whatever per-seat tallies exist (zeros when nothing was played)."""
    if seats is None:
        seats = tuple(SeatOutcome() for _ in range(config.num_seats))
    return results_doc(config, EpisodeResult(
        seats=seats, end_reason=END_SIM_FAULT,
        wall_clock_seconds=wall_clock_seconds))
