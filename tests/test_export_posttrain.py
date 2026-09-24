"""Exported rows preserve complete games and the program text FLE ran."""

import json
import sys

from tests.fakes import FakeSession
from tools import export_posttrain
from tools.train_bridge import Bridge


def test_full_game_export(monkeypatch, tmp_path):
    monkeypatch.setenv("COGAME_FACTORIO_SERVERS", "localhost:27000")
    monkeypatch.setattr(export_posttrain, "Bridge", lambda manifest, variant: Bridge(
        manifest, variant,
        session_factory=lambda seat, _host, _port, config: FakeSession(seat, config)))
    output = tmp_path / "dataset"
    monkeypatch.setattr(sys, "argv", ["export_posttrain.py", str(output), "10", "solo"])
    export_posttrain.main()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["train_examples"] == 240
    assert manifest["validation_examples"] == 60
    assert len(manifest["runs"]) == 10
    splits = {name: [json.loads(line) for line in (output / f"{name}.jsonl").read_text().splitlines()]
              for name in ("train", "validation")}
    assert {row["seed"] for row in splits["train"]}.isdisjoint(
        {row["seed"] for row in splits["validation"]})
    for row in splits["train"] + splits["validation"]:
        assert [part["role"] for part in row["prompt"]] == ["system", "user"]
        assert row["completion"][0]["content"].startswith("```python\n")
        assert row["completion"][0]["content"].endswith("\n```")
