"""Replay document (docs/REPLAY.md, ``cogame-factorio-replay`` v1).

The replay is one JSON document, kept in memory and written once at the
end (plus best-effort partial writes on faults). ``flatten_entities``
turns FLE entity dumps (``model_dump(mode="json")`` dicts, including
BeltGroup/PipeGroup/ElectricityGroup) into the compact per-step rows the
viewer draws.
"""

from __future__ import annotations

import json
from typing import Any

from .config import GameConfig
from .version import GAME_VERSION

FORMAT = "cogame-factorio-replay"
VERSION = 1

# Per-step record keys the engine supplies (REPLAY.md `seats[].steps[]`).
STEP_KEYS = (
    "step", "code", "noop", "output", "error", "score", "throughput", "tick",
    "wall_ms", "character", "entities", "belts", "pipes", "inventory",
    "flows_output",
)


class ReplayError(ValueError):
    """Corrupt or unsupported replay document."""


def _num(value, default=0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN -> default


def _direction(value) -> int:
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return -1


def _status(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):  # enum dumped oddly
        return str(value.get("value", ""))
    return "" if value is None else str(getattr(value, "value", value))


def _xy(d: dict) -> tuple[float, float]:
    pos = d.get("position") or {}
    if isinstance(pos, dict):
        return _num(pos.get("x")), _num(pos.get("y"))
    if isinstance(pos, (list, tuple)) and len(pos) == 2:
        return _num(pos[0]), _num(pos[1])
    return 0.0, 0.0


def _size(d: dict) -> tuple[float, float]:
    td = d.get("tile_dimensions") or {}
    if isinstance(td, dict) and "tile_width" in td:
        return (max(1.0, _num(td.get("tile_width"), 1.0)),
                max(1.0, _num(td.get("tile_height"), 1.0)))
    return 1.0, 1.0


def flatten_entities(entities: list[dict]) -> dict:
    """Compact rows for one entity snapshot.

    Returns ``{"entities": [[name, x, y, direction, status, w, h], ...],
    "belts": [[x, y, direction], ...], "pipes": [[x, y], ...]}``. Groups
    are flattened: BeltGroup.belts -> belts, PipeGroup.pipes -> pipes,
    ElectricityGroup.poles / WallGroup.entities -> entities. Rows are
    sorted for a stable document.
    """
    rows: list[list] = []
    belts: list[list] = []
    pipes: list[list] = []

    def visit(d: Any) -> None:
        if not isinstance(d, dict):
            return
        if isinstance(d.get("belts"), list):
            for b in d["belts"]:
                if isinstance(b, dict):
                    x, y = _xy(b)
                    belts.append([x, y, _direction(b.get("direction"))])
            return
        if isinstance(d.get("pipes"), list):
            for p in d["pipes"]:
                if isinstance(p, dict):
                    x, y = _xy(p)
                    pipes.append([x, y])
            return
        for group_key in ("poles", "entities"):
            if isinstance(d.get(group_key), list):
                for e in d[group_key]:
                    visit(e)
                return
        name = d.get("name")
        if not isinstance(name, str):
            return
        x, y = _xy(d)
        w, h = _size(d)
        rows.append([name, x, y, _direction(d.get("direction")),
                     _status(d.get("status")), w, h])

    for e in entities:
        visit(e)
    rows.sort(key=lambda r: (r[1], r[2], r[0]))
    belts.sort()
    pipes.sort()
    return {"entities": rows, "belts": belts, "pipes": pipes}


class ReplayWriter:
    def __init__(self, config: GameConfig, task: dict,
                 terrain: dict | None = None):
        self.config = config
        self.task = dict(task)
        self.terrain = terrain if terrain is not None else empty_terrain()
        self._steps: list[list[dict]] = [[] for _ in range(config.num_seats)]

    @property
    def step_counts(self) -> list[int]:
        return [len(s) for s in self._steps]

    def set_terrain(self, terrain: dict) -> None:
        self.terrain = terrain

    def append_step(self, slot: int, record: dict) -> None:
        missing = [k for k in STEP_KEYS if k not in record]
        if missing:
            raise ValueError(f"step record missing keys {missing}")
        self._steps[slot].append({k: record[k] for k in STEP_KEYS})

    def set_seat_throughput(self, slot: int, throughput: float | None) -> None:
        """Throughput is measured once when the seat finishes (FLE holdout
        verification); it lands on the seat's last recorded step."""
        if throughput is not None and self._steps[slot]:
            self._steps[slot][-1]["throughput"] = float(throughput)

    def document(self, results_doc: dict) -> dict:
        cfg = self.config
        seats = []
        for slot, player in enumerate(cfg.players):
            steps = self._steps[slot]
            seats.append({
                "slot": slot,
                "name": player.name,
                "final_score": float(results_doc["scores"][slot]),
                "dead": bool(results_doc["dead_seats"][slot]),
                "steps": steps,
            })
        return {
            "format": FORMAT,
            "version": VERSION,
            "game_version": GAME_VERSION,
            "config": cfg.to_dict(),
            "names": [p.name for p in cfg.players],
            "task": self.task,
            "map": self.terrain,
            "seats": seats,
            "result": results_doc,
        }

    def finalize(self, results_doc: dict) -> bytes:
        return json.dumps(self.document(results_doc),
                          separators=(",", ":")).encode("utf-8")


def empty_terrain(bounds: tuple[int, int, int, int] = (-64, -64, 64, 64)) \
        -> dict:
    x0, y0, x1, y1 = bounds
    return {"bounds": {"x0": x0, "y0": y0, "x1": x1, "y1": y1},
            "resources": [], "water": [], "trees": [], "spawn": {"x": 0, "y": 0}}


def rle_rows(tiles: list[tuple[int, int]]) -> list[list[int]]:
    """Run-length encode integer tiles into ``[x, y, run_length_east]``."""
    runs: list[list[int]] = []
    for x, y in sorted(set((int(x), int(y)) for x, y in tiles),
                       key=lambda t: (t[1], t[0])):
        if runs and runs[-1][1] == y and runs[-1][0] + runs[-1][2] == x:
            runs[-1][2] += 1
        else:
            runs.append([x, y, 1])
    return runs


class Replay:
    """Parsed replay document with structural validation."""

    def __init__(self, doc: dict):
        self.doc = doc

    @classmethod
    def parse(cls, data: bytes) -> "Replay":
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayError(f"replay is not valid JSON: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise ReplayError("bad replay format magic")
        if doc.get("version") != VERSION:
            raise ReplayError(f"unsupported replay version {doc.get('version')!r}")
        for key in ("game_version", "config", "names", "task", "map",
                    "seats", "result"):
            if key not in doc:
                raise ReplayError(f"replay missing {key!r}")
        if not isinstance(doc["seats"], list):
            raise ReplayError("replay seats must be a list")
        for seat in doc["seats"]:
            for step in seat.get("steps", []):
                missing = [k for k in STEP_KEYS if k not in step]
                if missing:
                    raise ReplayError(f"replay step missing {missing}")
        return cls(doc)

    @property
    def names(self) -> list[str]:
        return list(self.doc["names"])

    @property
    def result(self) -> dict:
        return self.doc["result"]

    @property
    def seats(self) -> list[dict]:
        return self.doc["seats"]
