"""Shared fixtures for the offline suite (fake FLE sessions)."""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cogame_factorio.config import GameConfig  # noqa: E402


def make_config(num_seats: int = 2, **overrides) -> GameConfig:
    data = {
        "players": [{"name": f"bot-{i}"} for i in range(num_seats)],
        "tokens": [f"token-{i}" for i in range(num_seats)],
        "num_agents": num_seats,
        "task": "open_play",
        "max_steps": 4,
        "step_deadline_seconds": 2.0,
        "program_timeout_seconds": 5.0,
        "player_connect_timeout_seconds": 5.0,
        "wall_clock_budget_seconds": 60.0,
    }
    data.update(overrides)
    return GameConfig.from_dict(data)


@pytest.fixture
def executor():
    ex = ThreadPoolExecutor(max_workers=4)
    yield ex
    ex.shutdown(wait=False, cancel_futures=True)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("COGAME_FACTORIO_SERVERS"):
        return
    skip = pytest.mark.skip(
        reason="needs COGAME_FACTORIO_SERVERS (fle cluster start -n 2)")
    for item in items:
        if "factorio" in item.keywords:
            item.add_marker(skip)
