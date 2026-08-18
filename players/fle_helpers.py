"""Small, defensive helpers for reading a cogame-factorio ``observation``.

Every accessor tolerates missing/malformed fields (returns an empty value)
so a policy conditioned on the observation never crashes on a surprising
message. Field layout: ``docs/PROTOCOL.md`` (``observation``).
"""

from __future__ import annotations

from typing import Any, Iterable


def inventory(observation: dict) -> dict[str, int]:
    """``{item_name: count}`` of the player's inventory (empty if absent)."""
    inv = observation.get("inventory") if isinstance(observation, dict) else None
    if not isinstance(inv, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in inv.items():
        if isinstance(k, str) and isinstance(v, (int, float)) \
                and not isinstance(v, bool):
            out[k] = int(v)
    return out


def inventory_count(observation: dict, item: str) -> int:
    """Count of ``item`` (Factorio item name, e.g. ``"burner-mining-drill"``)."""
    return inventory(observation).get(item, 0)


def entities(observation: dict) -> list[dict]:
    """The observation's entity list (dicts), skipping non-dict rows."""
    ents = observation.get("entities") if isinstance(observation, dict) else None
    if not isinstance(ents, list):
        return []
    return [e for e in ents if isinstance(e, dict)]


def entities_named(observation: dict, name: str) -> list[dict]:
    """Entities whose ``name`` equals ``name`` (e.g. ``"stone-furnace"``)."""
    return [e for e in entities(observation) if e.get("name") == name]


def entities_with_status(observation: dict, statuses: Iterable[str]) -> list[dict]:
    """Entities whose ``status`` (FLE EntityStatus value) is in ``statuses``."""
    wanted = set(statuses)
    return [e for e in entities(observation) if e.get("status") in wanted]


def entity_position(entity: dict) -> tuple[float, float] | None:
    """``(x, y)`` of an entity dict, or None if it has no usable position."""
    pos = entity.get("position") if isinstance(entity, dict) else None
    if not isinstance(pos, dict):
        return None
    x, y = pos.get("x"), pos.get("y")
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        return float(x), float(y)
    return None


def score(observation: dict) -> float:
    """Current production score / task metric (0.0 if absent)."""
    s = observation.get("score") if isinstance(observation, dict) else None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return float(s)
    return 0.0


def last_program(observation: dict) -> dict:
    """The ``last_program`` object (``code``/``output``/``error``) or ``{}``."""
    lp = observation.get("last_program") if isinstance(observation, dict) else None
    return lp if isinstance(lp, dict) else {}


def last_output(observation: dict) -> str:
    """Text output of the previous program (``""`` for step 0)."""
    out = last_program(observation).get("output")
    return out if isinstance(out, str) else ""


def last_program_failed(observation: dict) -> bool:
    """True when the previous program raised or timed out."""
    return bool(last_program(observation).get("error"))


def raw_text(observation: dict) -> str:
    """FLE-style text view of the observation (``""`` if absent)."""
    t = observation.get("raw_text") if isinstance(observation, dict) else None
    return t if isinstance(t, str) else ""


def get_in(obj: Any, *keys: str, default: Any = None) -> Any:
    """Nested dict lookup that never raises: ``get_in(obs, "flows", "output")``."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur
