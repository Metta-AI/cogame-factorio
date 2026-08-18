"""Replay writer/parser and entity flattening (docs/REPLAY.md)."""

import json

import pytest

from cogame_factorio.replay import (STEP_KEYS, Replay, ReplayError,
                                    ReplayWriter, flatten_entities, rle_rows)
from cogame_factorio.results import EpisodeResult, SeatOutcome, results_doc
from cogame_factorio.version import GAME_VERSION

from tests.conftest import make_config
from tests.fakes import (FAKE_TERRAIN, belt_group_dump, drill_dump,
                         pipe_group_dump, pole_group_dump)


def test_flatten_entities_groups_and_rows():
    dumps = [drill_dump(0), belt_group_dump(3), pole_group_dump(),
             pipe_group_dump(), {"name": "iron-chest", "direction": 0,
                                 "position": {"x": 16.5, "y": 73.5},
                                 "status": "normal",
                                 "tile_dimensions": {"tile_width": 1,
                                                     "tile_height": 1}}]
    out = flatten_entities(dumps)
    assert out["belts"] == [[16.5, 74.5, 2], [17.5, 74.5, 2], [18.5, 74.5, 2]]
    assert out["pipes"] == [[10.5, 3.5], [10.5, 4.5]]
    names = [r[0] for r in out["entities"]]
    assert sorted(names) == ["burner-mining-drill", "iron-chest",
                             "small-electric-pole"]
    drill = next(r for r in out["entities"] if r[0] == "burner-mining-drill")
    assert drill == ["burner-mining-drill", 16.0, 71.0, 4, "working", 2.0, 2.0]
    # no belt/pipe tiles leak into entities
    assert "transport-belt" not in names and "pipe" not in names


def test_flatten_entities_tolerates_garbage():
    out = flatten_entities([None, 5, {"position": {"x": 1}},
                            {"name": "x", "direction": "north",
                             "status": None}])
    assert out["entities"] == [["x", 0.0, 0.0, -1, "", 1.0, 1.0]]


def test_rle_rows():
    assert rle_rows([(1, 0), (2, 0), (3, 0), (5, 0), (1, 1)]) == \
        [[1, 0, 3], [5, 0, 1], [1, 1, 1]]
    assert rle_rows([]) == []


def _record(step, **over):
    rec = {"step": step, "code": "place()", "noop": False, "output": "ok",
           "error": False, "score": float(step + 1), "throughput": None,
           "tick": 100 * (step + 1), "wall_ms": 12,
           "character": {"x": 1.0, "y": 2.0},
           "entities": [["burner-mining-drill", 16.0, 71.0, 4, "working", 2, 2]],
           "belts": [], "pipes": [], "inventory": {"coal": 5},
           "flows_output": {"iron-ore": 3}}
    rec.update(over)
    return rec


def test_writer_document_round_trip():
    cfg = make_config(num_seats=2, task="iron_plate_throughput")
    task = {"key": "iron_plate_throughput", "goal_description": "g"}
    writer = ReplayWriter(cfg, task, FAKE_TERRAIN)
    for step in range(3):
        writer.append_step(0, _record(step))
    writer.append_step(1, _record(0, noop=True, code=""))
    writer.set_seat_throughput(0, 12.5)
    writer.set_seat_throughput(1, None)
    seats = (SeatOutcome(production_score=3.0, throughput=12.5,
                         steps_completed=3, final_tick=300),
             SeatOutcome(dead=True, noop_steps=1, final_tick=0))
    doc_res = results_doc(cfg, EpisodeResult(seats, "steps_cap", 42.0))
    data = writer.finalize(doc_res)
    replay = Replay.parse(data)
    doc = replay.doc
    assert doc["format"] == "cogame-factorio-replay" and doc["version"] == 1
    assert doc["game_version"] == GAME_VERSION
    assert doc["names"] == ["bot-0", "bot-1"]
    assert doc["task"] == task
    assert doc["map"] == FAKE_TERRAIN
    assert "tokens" not in doc["config"]
    assert doc["config"]["task"] == "iron_plate_throughput"
    assert doc["seats"][0]["final_score"] == 12.5
    assert doc["seats"][0]["steps"][-1]["throughput"] == 12.5
    assert doc["seats"][0]["steps"][0]["throughput"] is None
    assert doc["seats"][1]["dead"] is True
    assert doc["seats"][1]["steps"][0]["noop"] is True
    assert doc["result"] == doc_res
    assert set(doc["seats"][0]["steps"][0]) == set(STEP_KEYS)
    assert replay.result["scores"] == [12.5, 0.0]
    # compact encoding
    assert b"\n" not in data


def test_writer_rejects_incomplete_records():
    cfg = make_config(num_seats=1)
    writer = ReplayWriter(cfg, {"key": "open_play", "goal_description": ""})
    with pytest.raises(ValueError, match="missing keys"):
        writer.append_step(0, {"step": 0})


@pytest.mark.parametrize("bad", [
    b"\xff\xfe",
    b"[]",
    json.dumps({"format": "nope"}).encode(),
    json.dumps({"format": "cogame-factorio-replay", "version": 2}).encode(),
    json.dumps({"format": "cogame-factorio-replay", "version": 1}).encode(),
])
def test_parse_rejects_corrupt(bad):
    with pytest.raises(ReplayError):
        Replay.parse(bad)


def test_empty_terrain_default():
    cfg = make_config(num_seats=1)
    writer = ReplayWriter(cfg, {"key": "open_play", "goal_description": ""})
    doc = writer.document(results_doc(cfg, EpisodeResult(
        (SeatOutcome(),), "sim_fault", 0.0)))
    assert doc["map"]["bounds"] == {"x0": -64, "y0": -64, "x1": 64, "y1": 64}
    assert doc["seats"][0]["steps"] == []
