"""Results document: closed key set, scoring rule, fault doc."""

import json

from cogame_factorio import contract
from cogame_factorio.results import (END_REASONS, NOOP_CAUSES, RESULT_KEYS,
                                     EpisodeResult, SeatOutcome,
                                     fault_results_doc, results_doc,
                                     zero_noop_causes)

from tests.conftest import make_config


def test_results_doc_key_set_and_types():
    cfg = make_config(num_seats=2)
    seats = (SeatOutcome(production_score=3.5, steps_completed=4,
                         error_steps=1, final_tick=1234),
             SeatOutcome(dead=True, noop_steps=3,
                         noop_causes={**zero_noop_causes(), "timeout": 3}))
    doc = results_doc(cfg, EpisodeResult(seats, "steps_cap", 12.25))
    assert set(doc) == RESULT_KEYS == set(contract.RESULT_KEYS)
    assert doc["names"] == ["bot-0", "bot-1"]
    assert doc["scores"] == [3.5, 0.0]
    assert doc["production_scores"] == [3.5, 0.0]
    assert doc["throughputs"] == [None, None]
    assert doc["task_key"] == "open_play"
    assert doc["steps_completed"] == [4, 0]
    assert doc["error_steps"] == [1, 0]
    assert doc["noop_steps"] == [0, 3]
    assert doc["dead_seats"] == [False, True]
    assert doc["noop_causes"][1]["timeout"] == 3
    assert set(doc["noop_causes"][0]) == set(NOOP_CAUSES)
    assert doc["final_ticks"] == [1234, 0]
    assert doc["end_reason"] == "steps_cap"
    assert doc["wall_clock_seconds"] == 12.25
    json.dumps(doc)


def test_throughput_task_scores_use_throughput():
    cfg = make_config(num_seats=1, task="iron_plate_throughput")
    seats = (SeatOutcome(production_score=40.0, throughput=17.0),)
    doc = results_doc(cfg, EpisodeResult(seats, "steps_cap", 1.0))
    assert doc["scores"] == [17.0]
    assert doc["production_scores"] == [40.0]
    assert doc["throughputs"] == [17.0]


def test_fault_doc_same_closed_keys_every_seat_present():
    cfg = make_config(num_seats=3)
    doc = fault_results_doc(cfg, 5.0)
    assert set(doc) == RESULT_KEYS
    assert doc["end_reason"] == "sim_fault"
    assert doc["scores"] == [0.0] * 3 and doc["dead_seats"] == [False] * 3
    assert len(doc["noop_causes"]) == 3


def test_end_reasons_closed():
    assert set(END_REASONS) == {"steps_cap", "wall_clock", "sim_fault"}
    assert END_REASONS == contract.END_REASONS
