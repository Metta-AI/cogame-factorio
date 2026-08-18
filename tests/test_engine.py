"""Engine behavior against fake sessions/sources (PROTOCOL.md rules)."""

import asyncio
import time

import pytest

from cogame_factorio.engine import Engine, validate_program_message
from cogame_factorio.results import NOOP_CAUSES, results_doc

from tests.conftest import make_config
from tests.fakes import FakeSession, FakeSource


async def run_engine(cfg, sources, sessions, executor, **hooks):
    for s in sessions:
        s.start()
    engine = Engine(cfg, sources, sessions, executor, **hooks)
    result = await engine.run()
    return engine, result


async def test_full_episode_two_seats(executor):
    cfg = make_config(max_steps=3)
    sessions = [FakeSession(i, cfg) for i in range(2)]
    sources = [FakeSource(), FakeSource(replies={1: "x = 1/0"})]
    records = {0: [], 1: []}
    progress = []
    engine, result = await run_engine(
        cfg, sources, sessions, executor,
        on_step=lambda slot, rec: records[slot].append(rec),
        on_progress=lambda slot, step, score: progress.append((slot, step, score)))
    assert result.end_reason == "steps_cap"
    s0, s1 = result.seats
    assert s0.steps_completed == 3 and s0.error_steps == 0 and s0.noop_steps == 0
    assert s1.steps_completed == 3 and s1.error_steps == 1
    assert s0.production_score == 3.0 and s1.production_score == 2.0
    assert not s0.dead and not s1.dead
    assert s0.throughput is None
    # observations: step numbering, last_program chaining, deadline
    steps = [step for step, _ in sources[0].seen]
    assert steps == [0, 1, 2]
    first = sources[0].seen[0][1]
    assert first["type"] == "observation" and first["step"] == 0
    assert first["observation"]["last_program"] is None
    assert 0 < first["deadline_seconds"] <= cfg.step_deadline_seconds
    second = sources[1].seen[2][1]["observation"]
    assert second["last_program"]["code"] == "x = 1/0"
    assert second["last_program"]["error"] is True
    # replay records
    assert [r["step"] for r in records[0]] == [0, 1, 2]
    rec = records[1][1]
    assert rec["error"] is True and rec["code"] == "x = 1/0" and not rec["noop"]
    assert rec["score"] == 1.0  # score AFTER step 1 for seat 1
    assert records[0][2]["entities"] and records[0][2]["belts"]
    assert len(progress) == 6
    doc = results_doc(cfg, result)
    assert doc["scores"] == [3.0, 2.0]
    assert doc["error_steps"] == [0, 1]
    assert doc["throughputs"] == [None, None]
    assert doc["final_ticks"] == [300, 300]


async def test_strike_rule_marks_seat_dead_and_ends_loop(executor):
    cfg = make_config(num_seats=1, max_steps=10, step_deadline_seconds=0.05,
                      strike_limit=3)
    sessions = [FakeSession(0, cfg)]
    # step 0 ok, then 3 timeouts -> dead at step 3, loop ends
    sources = [FakeSource(replies={1: None, 2: None, 3: None, 4: None})]
    dead = []
    engine, result = await run_engine(cfg, sources, sessions, executor,
                                      on_seat_dead=dead.append)
    seat = result.seats[0]
    assert seat.dead and dead == [0]
    assert seat.steps_completed == 1 and seat.noop_steps == 3
    assert seat.noop_causes["timeout"] == 3
    assert [s for s, _ in sources[0].seen] == [0, 1, 2, 3]
    assert result.end_reason == "steps_cap"


async def test_valid_program_resets_consecutive_strikes(executor):
    cfg = make_config(num_seats=1, max_steps=6, step_deadline_seconds=0.05,
                      strike_limit=3)
    sessions = [FakeSession(0, cfg)]
    sources = [FakeSource(replies={0: None, 1: None, 3: None, 4: None})]
    _, result = await run_engine(cfg, sources, sessions, executor)
    seat = result.seats[0]
    assert not seat.dead
    assert seat.noop_steps == 4 and seat.steps_completed == 2


async def test_malformed_and_host_error_causes(executor):
    cfg = make_config(num_seats=1, max_steps=4, strike_limit=10)

    async def boom(step, payload, deadline_at):
        raise RuntimeError("transport exploded")

    sessions = [FakeSession(0, cfg)]
    sources = [FakeSource(replies={0: ("malformed",), 1: boom,
                                   2: ("disconnected",)})]
    _, result = await run_engine(cfg, sources, sessions, executor)
    causes = result.seats[0].noop_causes
    assert causes["malformed"] == 1 and causes["host_error"] == 1
    assert causes["disconnected"] == 1 and causes["timeout"] == 0
    assert set(causes) == set(NOOP_CAUSES)
    assert result.seats[0].steps_completed == 1


async def test_wrong_step_count_from_source_is_merged(executor):
    cfg = make_config(num_seats=1, max_steps=1)
    sessions = [FakeSession(0, cfg)]
    src = FakeSource()
    src.wrong_step_count = 4
    _, result = await run_engine(cfg, [src], sessions, executor)
    assert result.seats[0].noop_causes["wrong_step"] == 4
    assert result.seats[0].noop_steps == 0


async def test_wall_clock_budget_ends_all_seats(executor):
    cfg = make_config(max_steps=50, step_deadline_seconds=5.0,
                      wall_clock_budget_seconds=0.6)
    sessions = [FakeSession(i, cfg) for i in range(2)]
    # seat 0 plays slow programs, seat 1 never replies (waits deadline)
    sources = [FakeSource(default_code="#slow 0.2\nplace()"),
               FakeSource(replies={k: None for k in range(50)})]
    t0 = time.monotonic()
    _, result = await run_engine(cfg, sources, sessions, executor)
    assert time.monotonic() - t0 < 3.0
    assert result.end_reason == "wall_clock"
    assert result.seats[0].steps_completed >= 1
    assert result.seats[0].steps_completed < 50
    # a wait cut short by the wall clock is not a strike
    assert not result.seats[1].dead
    assert result.seats[1].noop_steps == 0


async def test_session_fault_ends_seat_and_marks_sim_fault(executor):
    cfg = make_config(max_steps=4)
    sessions = [FakeSession(i, cfg) for i in range(2)]
    sources = [FakeSource(replies={1: "#fault"}), FakeSource()]
    _, result = await run_engine(cfg, sources, sessions, executor)
    assert result.end_reason == "sim_fault"
    assert result.seats[0].faulted and result.seats[0].steps_completed == 1
    # the other seat still finished its own loop
    assert result.seats[1].steps_completed == 4 and not result.seats[1].faulted


async def test_throughput_task_measures_once_at_end(executor):
    cfg = make_config(num_seats=1, max_steps=3, task="iron_plate_throughput")
    sessions = [FakeSession(0, cfg)]
    sources = [FakeSource()]
    finished = []
    _, result = await run_engine(
        cfg, sources, sessions, executor,
        on_seat_finished=lambda slot, o: finished.append((slot, o.throughput)))
    seat = result.seats[0]
    assert sessions[0]._throughput_calls == 1
    assert seat.throughput == 6.0 and seat.production_score == 3.0
    assert seat.score == 6.0
    assert finished == [(0, 6.0)]
    doc = results_doc(cfg, result)
    assert doc["scores"] == [6.0] and doc["throughputs"] == [6.0]
    assert doc["production_scores"] == [3.0]
    obs = sources[0].seen[0][1]["observation"]
    assert obs["task_verification"] == {"success": False,
                                        "meta": {"throughput": 0.0}}


async def test_never_connected_seat_plays_noops_and_reports(executor):
    cfg = make_config(num_seats=2, max_steps=5, step_deadline_seconds=0.05,
                      player_connect_timeout_seconds=0.1, strike_limit=2)
    sessions = [FakeSession(i, cfg) for i in range(2)]
    reported = []

    async def report(slot):
        reported.append(slot)

    sources = [FakeSource(connected=False,
                          replies={k: ("disconnected",) for k in range(5)}),
               FakeSource()]
    _, result = await run_engine(cfg, sources, sessions, executor,
                                 on_never_connected=report)
    assert reported == [0]
    assert result.seats[0].dead and result.seats[0].noop_causes["disconnected"] == 2
    assert result.seats[1].steps_completed == 5


async def test_hook_exceptions_never_crash_episode(executor):
    cfg = make_config(num_seats=1, max_steps=2)
    sessions = [FakeSession(0, cfg)]

    def bad_hook(*a):
        raise RuntimeError("observer bug")

    _, result = await run_engine(cfg, [FakeSource()], sessions, executor,
                                 on_step=bad_hook, on_progress=bad_hook)
    assert result.seats[0].steps_completed == 2


@pytest.mark.parametrize("data,expected", [
    ({"type": "program", "step": 3, "code": "x"}, ("x", None)),
    ({"type": "program", "step": 2, "code": "x"}, (None, "wrong_step")),
    ({"type": "program", "step": True, "code": "x"}, (None, "wrong_step")),
    ({"type": "program", "step": 3, "code": 5}, (None, "malformed")),
    ({"type": "program", "step": 3}, (None, "malformed")),
    ({"type": "program", "step": 3, "code": "x" * (64 * 1024 + 1)},
     (None, "malformed")),
    ({"type": "action", "step": 3, "code": "x"}, (None, "malformed")),
    ("nope", (None, "malformed")),
    ([1, 2], (None, "malformed")),
])
def test_validate_program_message(data, expected):
    assert validate_program_message(data, 3) == expected


def test_engine_rejects_mismatched_sources(executor):
    cfg = make_config(num_seats=2)
    with pytest.raises(ValueError):
        Engine(cfg, [FakeSource()], [FakeSession(0, cfg)], executor)
