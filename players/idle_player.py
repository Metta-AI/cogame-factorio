"""Idle baseline: a valid no-op program every step (the score floor).

``python -m players.idle_player``
"""

from __future__ import annotations

from players.client import Policy, main_for


class IdlePolicy(Policy):
    """Replies ``pass`` on every step; never strikes, never builds."""

    def program(self, step: int, observation: dict) -> str:
        return "pass"


if __name__ == "__main__":
    main_for(IdlePolicy)
