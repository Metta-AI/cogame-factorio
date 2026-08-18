"""Tripwires: the manifest template stays in sync with the code.

The results schema is CLOSED (AGENTS.md): results.py, the manifest
results_schema and docker_smoke.sh's expected key set must list exactly
the same keys; end_reason and noop cause enums must match. Every
variant/certification game_config must parse (with runner-injected
tokens) and carry num_agents. Every config_schema property must be
provably consumed by GameConfig.from_dict.
"""

import json
import re
from pathlib import Path

from cogame_factorio.config import GameConfig
from cogame_factorio.results import (END_REASONS, NOOP_CAUSES, RESULT_KEYS,
                                     EpisodeResult, SeatOutcome,
                                     fault_results_doc, results_doc)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(
    (REPO_ROOT / "coworld_manifest_template.json").read_text())
DOCKER_SMOKE = (REPO_ROOT / "tools" / "ci" / "docker_smoke.sh").read_text()

# config_schema properties the platform consumes, not the game server
# (each must say so in its schema description).
PLATFORM_ONLY_KEYS = ["num_agents"]

BASE = {"players": [{"name": "a"}, {"name": "b"}], "tokens": ["t0", "t1"]}
# a non-default sample per consumed property (must change from_dict output)
SAMPLES = {
    "tokens": ["x0", "x1"],
    "players": [{"name": "c"}, {"name": "d"}],
    "task": "iron_plate_throughput",
    "max_steps": 7,
    "step_deadline_seconds": 12.5,
    "program_timeout_seconds": 9.0,
    "strike_limit": 5,
    "player_connect_timeout_seconds": 33.0,
    "wall_clock_budget_seconds": 123.0,
    "game_speed": 4.0,
    "fast": False,
}


def _dummy_result(n=2):
    return EpisodeResult(tuple(SeatOutcome() for _ in range(n)),
                         "steps_cap", 1.0)


def _schema_keys():
    schema = MANIFEST["game"]["results_schema"]
    assert schema["additionalProperties"] is False, \
        "results_schema must stay closed"
    return set(schema["required"]), set(schema["properties"])


def test_results_doc_matches_manifest_results_schema():
    cfg = GameConfig.from_dict(dict(BASE))
    doc_keys = set(results_doc(cfg, _dummy_result()))
    required, properties = _schema_keys()
    assert doc_keys == required == RESULT_KEYS, sorted(doc_keys ^ required)
    assert doc_keys == properties, sorted(doc_keys ^ properties)


def test_fault_results_doc_has_same_closed_key_set():
    cfg = GameConfig.from_dict(dict(BASE))
    assert set(fault_results_doc(cfg, 0.0)) == RESULT_KEYS


def test_docker_smoke_expected_keys_match_results_doc():
    match = re.search(r"expected = \{(.*?)\}", DOCKER_SMOKE, re.DOTALL)
    assert match, "docker_smoke.sh expected-keys block not found"
    smoke_keys = set(re.findall(r'"(\w+)"', match.group(1)))
    assert smoke_keys == RESULT_KEYS, sorted(smoke_keys ^ RESULT_KEYS)


def test_end_reason_and_noop_cause_enums_match():
    props = MANIFEST["game"]["results_schema"]["properties"]
    assert set(props["end_reason"]["enum"]) == set(END_REASONS)
    causes = props["noop_causes"]["items"]
    assert causes["additionalProperties"] is False
    assert set(causes["required"]) == set(causes["properties"]) == set(NOOP_CAUSES)


def test_variant_and_certification_configs_parse_with_num_agents():
    configs = [(v["id"], v["game_config"]) for v in MANIFEST["variants"]]
    configs.append(("certification", MANIFEST["certification"]["game_config"]))
    for label, game_config in configs:
        assert "num_agents" in game_config, f"{label}: num_agents required"
        assert game_config["num_agents"] == len(game_config["players"]), label
        data = dict(game_config)
        data["tokens"] = [f"tok-{i}" for i in range(len(data["players"]))]
        cfg = GameConfig.from_dict(data)
        assert cfg.num_seats == len(game_config["players"]), label
        assert cfg.wall_clock_budget_seconds < \
            MANIFEST["episode_timeout_minutes"] * 60, label


def test_config_schema_matches_parser():
    schema = MANIFEST["game"]["config_schema"]
    assert schema["additionalProperties"] is False
    props = schema["properties"]
    assert set(schema["required"]) == {"tokens", "players"}
    # every property is either platform-only (documented) or consumed
    for key, prop in props.items():
        if key in PLATFORM_ONLY_KEYS:
            assert "ignored by the game server" in prop["description"], key
            continue
        assert key in SAMPLES, f"no consumption sample for {key}"
    assert set(SAMPLES) == set(props) - set(PLATFORM_ONLY_KEYS)
    default_cfg = GameConfig.from_dict(dict(BASE)).to_dict()
    default_cfg["tokens"] = list(BASE["tokens"])
    for key, sample in SAMPLES.items():
        cfg = GameConfig.from_dict({**BASE, key: sample})
        d = cfg.to_dict()
        d["tokens"] = list(cfg.tokens)
        assert d != default_cfg, f"{key} sample did not change the config"
        assert d[key] == sample, key
    # schema defaults == parser defaults
    parsed = GameConfig.from_dict(dict(BASE))
    for key in ("task", "max_steps", "step_deadline_seconds",
                "program_timeout_seconds", "strike_limit",
                "player_connect_timeout_seconds", "game_speed", "fast"):
        assert getattr(parsed, key) == props[key]["default"], key
    assert "0.85" in props["wall_clock_budget_seconds"]["description"]
    assert parsed.wall_clock_budget_seconds == \
        0.85 * MANIFEST["episode_timeout_minutes"] * 60
    # unknown keys rejected (closed schema)
    import pytest
    from cogame_factorio.config import ConfigError
    with pytest.raises(ConfigError):
        GameConfig.from_dict({**BASE, "seed": 1})
    # num_agents is validated for consistency even though platform-only
    with pytest.raises(ConfigError):
        GameConfig.from_dict({**BASE, "num_agents": 3})


def test_manifest_run_commands_and_viewer_bundle():
    game = MANIFEST["game"]
    assert game["runnable"]["run"] == ["python", "-m", "cogame_factorio.server"]
    assert game["replay_viewer"]["bundle"] == "static-replay-viewer"
    for player in MANIFEST["player"]:
        assert player["run"][:2] == ["python", "-m"]
        assert player["run"][2].startswith("players.")
