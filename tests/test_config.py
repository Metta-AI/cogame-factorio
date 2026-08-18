"""GameConfig.from_dict mirrors the manifest config_schema exactly."""

import json
from pathlib import Path

import pytest

from cogame_factorio.config import GameConfig, ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO_ROOT / "coworld_manifest_template.json").read_text())[
    "game"]["config_schema"]

MINIMAL = {"players": [{"name": "a"}, {"name": "b"}], "tokens": ["t0", "t1"]}


def test_defaults_match_schema():
    cfg = GameConfig.from_dict(dict(MINIMAL))
    props = SCHEMA["properties"]
    assert cfg.task == props["task"]["default"]
    assert cfg.max_steps == props["max_steps"]["default"]
    assert cfg.step_deadline_seconds == props["step_deadline_seconds"]["default"]
    assert cfg.program_timeout_seconds == props["program_timeout_seconds"]["default"]
    assert cfg.strike_limit == props["strike_limit"]["default"]
    assert cfg.player_connect_timeout_seconds == \
        props["player_connect_timeout_seconds"]["default"]
    assert cfg.game_speed == props["game_speed"]["default"]
    assert cfg.fast is props["fast"]["default"]
    # derived: 0.85 x episode_timeout_minutes x 60
    assert cfg.wall_clock_budget_seconds == 0.85 * 60 * 60 == 3060
    assert cfg.num_seats == 2


def test_repo_config_json_parses():
    cfg = GameConfig.from_file_uri(str(REPO_ROOT / "config.json"))
    assert cfg.num_seats == 2 and cfg.max_steps == 6


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="unknown config keys"):
        GameConfig.from_dict({**MINIMAL, "seed": 1})


def test_extra_player_keys_rejected():
    with pytest.raises(ConfigError):
        GameConfig.from_dict({"players": [{"name": "a", "x": 1}], "tokens": ["t"]})


@pytest.mark.parametrize("bad", [
    {"players": []},
    {"players": [{"name": ""}], "tokens": ["t"]},
    {"players": [{"name": "a"}], "tokens": []},
    {"players": [{"name": "a"}], "tokens": ["t", "u"]},
    {"players": [{"name": "a"}] * 5, "tokens": ["t"] * 5},
    {**MINIMAL, "num_agents": 3},
    {**MINIMAL, "task": ""},
    {**MINIMAL, "max_steps": 0},
    {**MINIMAL, "max_steps": 129},
    {**MINIMAL, "max_steps": 2.5},
    {**MINIMAL, "step_deadline_seconds": 0},
    {**MINIMAL, "program_timeout_seconds": -1},
    {**MINIMAL, "strike_limit": 0},
    {**MINIMAL, "player_connect_timeout_seconds": -1},
    {**MINIMAL, "wall_clock_budget_seconds": 0},
    {**MINIMAL, "wall_clock_budget_seconds": float("inf")},
    {**MINIMAL, "game_speed": 0},
    {**MINIMAL, "fast": "yes"},
    {**MINIMAL, "fast": 1},
    [],
])
def test_invalid_configs(bad):
    with pytest.raises(ConfigError):
        GameConfig.from_dict(bad)


def test_num_agents_must_match_players():
    cfg = GameConfig.from_dict({**MINIMAL, "num_agents": 2})
    assert cfg.num_seats == 2


def test_to_dict_excludes_tokens_and_is_json():
    cfg = GameConfig.from_dict({**MINIMAL, "task": "iron_plate_throughput",
                                "fast": False})
    d = cfg.to_dict()
    assert "tokens" not in d
    assert d["task"] == "iron_plate_throughput" and d["fast"] is False
    assert d["num_agents"] == 2
    json.dumps(d)


def test_bad_file_uri():
    with pytest.raises(ConfigError, match="cannot read"):
        GameConfig.from_file_uri("file:///no/such/file.json")
