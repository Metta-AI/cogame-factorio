"""Game config model: the manifest ``config_schema`` as a dataclass.

The config JSON arrives via ``COGAME_CONFIG_URI``. ``players`` and
``tokens`` are parallel arrays in seat-slot order; every other key has
the default the manifest schema declares. The schema is closed
(``additionalProperties: false``): unknown keys are rejected here too, so
a typo in a variant fails at startup (exit 2) instead of silently
playing defaults.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TASK = "open_play"
DEFAULT_MAX_STEPS = 30
MAX_MAX_STEPS = 128
MAX_SEATS = 4
DEFAULT_STEP_DEADLINE_SECONDS = 60.0
DEFAULT_PROGRAM_TIMEOUT_SECONDS = 45.0
DEFAULT_STRIKE_LIMIT = 3
DEFAULT_PLAYER_CONNECT_TIMEOUT_SECONDS = 180.0
# 0.85 x episode_timeout_minutes (60) x 60: always under the platform's
# container kill, which would lose results and replay.
DEFAULT_WALL_CLOCK_BUDGET_SECONDS = 0.85 * 60 * 60
DEFAULT_GAME_SPEED = 10.0
DEFAULT_FAST = True

KNOWN_KEYS = frozenset({
    "tokens", "players", "num_agents", "task", "max_steps",
    "step_deadline_seconds", "program_timeout_seconds", "strike_limit",
    "player_connect_timeout_seconds", "wall_clock_budget_seconds",
    "game_speed", "fast",
})


class ConfigError(ValueError):
    """Invalid or inconsistent game config."""


@dataclass(frozen=True)
class PlayerConfig:
    name: str


@dataclass(frozen=True)
class GameConfig:
    players: tuple[PlayerConfig, ...]
    tokens: tuple[str, ...]
    task: str
    max_steps: int
    step_deadline_seconds: float
    program_timeout_seconds: float
    strike_limit: int
    player_connect_timeout_seconds: float
    wall_clock_budget_seconds: float
    game_speed: float
    fast: bool

    @property
    def num_seats(self) -> int:
        return len(self.players)

    @classmethod
    def from_dict(cls, data) -> "GameConfig":
        if not isinstance(data, dict):
            raise ConfigError(
                f"config must be a JSON object, got {type(data).__name__}")
        unknown = sorted(set(data) - KNOWN_KEYS)
        if unknown:
            raise ConfigError(f"unknown config keys: {unknown}")

        players_raw = data.get("players")
        if not isinstance(players_raw, list) or not players_raw:
            raise ConfigError("config requires a non-empty 'players' array")
        if len(players_raw) > MAX_SEATS:
            raise ConfigError(
                f"players supports at most {MAX_SEATS} seats, "
                f"got {len(players_raw)}")
        players = []
        for i, entry in enumerate(players_raw):
            if not isinstance(entry, dict) or set(entry) != {"name"} \
                    or not isinstance(entry["name"], str) or not entry["name"]:
                raise ConfigError(
                    f"players[{i}] must be an object with exactly a "
                    f"non-empty 'name'")
            players.append(PlayerConfig(name=entry["name"]))

        tokens_raw = data.get("tokens")
        if not isinstance(tokens_raw, list) or \
                not all(isinstance(t, str) and t for t in tokens_raw):
            raise ConfigError(
                "config requires a 'tokens' array of non-empty strings")
        if len(tokens_raw) != len(players):
            raise ConfigError(
                f"tokens length {len(tokens_raw)} != players length "
                f"{len(players)}")

        if "num_agents" in data:
            num_agents = _int_field(data, "num_agents", 0)
            if num_agents != len(players):
                raise ConfigError(
                    f"num_agents ({num_agents}) must equal the number of "
                    f"players ({len(players)})")

        task = data.get("task", DEFAULT_TASK)
        if not isinstance(task, str) or not task:
            raise ConfigError(f"task must be a non-empty string, got {task!r}")

        max_steps = _int_field(data, "max_steps", DEFAULT_MAX_STEPS)
        if not 1 <= max_steps <= MAX_MAX_STEPS:
            raise ConfigError(
                f"max_steps must be in [1, {MAX_MAX_STEPS}], got {max_steps}")

        step_deadline = _number_field(
            data, "step_deadline_seconds", DEFAULT_STEP_DEADLINE_SECONDS,
            positive=True)
        program_timeout = _number_field(
            data, "program_timeout_seconds",
            DEFAULT_PROGRAM_TIMEOUT_SECONDS, positive=True)

        strike_limit = _int_field(data, "strike_limit", DEFAULT_STRIKE_LIMIT)
        if strike_limit < 1:
            raise ConfigError(f"strike_limit must be >= 1, got {strike_limit}")

        connect_timeout = _number_field(
            data, "player_connect_timeout_seconds",
            DEFAULT_PLAYER_CONNECT_TIMEOUT_SECONDS, positive=False)
        budget = _number_field(
            data, "wall_clock_budget_seconds",
            DEFAULT_WALL_CLOCK_BUDGET_SECONDS, positive=True)
        game_speed = _number_field(
            data, "game_speed", DEFAULT_GAME_SPEED, positive=True)

        fast = data.get("fast", DEFAULT_FAST)
        if not isinstance(fast, bool):
            raise ConfigError(f"fast must be a boolean, got {fast!r}")

        return cls(
            players=tuple(players),
            tokens=tuple(tokens_raw),
            task=task,
            max_steps=max_steps,
            step_deadline_seconds=step_deadline,
            program_timeout_seconds=program_timeout,
            strike_limit=strike_limit,
            player_connect_timeout_seconds=connect_timeout,
            wall_clock_budget_seconds=budget,
            game_speed=game_speed,
            fast=fast,
        )

    @classmethod
    def from_file_uri(cls, uri: str) -> "GameConfig":
        """Parse a config from a local ``file://`` URI or plain path."""
        path = uri.removeprefix("file://")
        try:
            raw = Path(path).read_text()
        except OSError as exc:
            raise ConfigError(f"cannot read config from {uri}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"config at {uri} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        """Fully-resolved config for the replay document.

        Tokens are deliberately excluded: replays are public artifacts,
        tokens are per-episode player credentials.
        """
        return {
            "players": [{"name": p.name} for p in self.players],
            "num_agents": self.num_seats,
            "task": self.task,
            "max_steps": self.max_steps,
            "step_deadline_seconds": self.step_deadline_seconds,
            "program_timeout_seconds": self.program_timeout_seconds,
            "strike_limit": self.strike_limit,
            "player_connect_timeout_seconds":
                self.player_connect_timeout_seconds,
            "wall_clock_budget_seconds": self.wall_clock_budget_seconds,
            "game_speed": self.game_speed,
            "fast": self.fast,
        }


def _int_field(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer, got {value!r}")
    return value


def _number_field(data: dict, key: str, default: float, *,
                  positive: bool) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(value):
        raise ConfigError(f"{key} must be a finite number, got {value!r}")
    if positive and value <= 0:
        raise ConfigError(f"{key} must be positive, got {value!r}")
    if not positive and value < 0:
        raise ConfigError(f"{key} must be non-negative, got {value!r}")
    return float(value)
