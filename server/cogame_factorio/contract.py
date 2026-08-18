"""Wire contract constants for ``cogame.factorio.v1`` — stdlib only.

Every string a policy depends on lives here so a policy container can
import this module (or vendor it) without pulling in the server's
third-party dependencies. NO third-party imports, ever.

Four-surface rename rule: renaming or adding any constant here must be
mirrored in (1) this module, (2) ``tests/contract_manifest.txt`` (the
golden list ``tests/test_contract.py`` compares against), (3)
``docs/PROTOCOL.md``, and (4) ``players/`` (the shared client harness
and baselines). A change that alters what a policy sees also bumps
``version.GAME_VERSION``.
"""

from __future__ import annotations

PROTOCOL = "cogame.factorio.v1"

# Message `type` values -------------------------------------------------------
# server -> player
MSG_WELCOME = "welcome"
MSG_OBSERVATION = "observation"
MSG_DONE = "done"
# player -> server
MSG_PROGRAM = "program"
# server -> global viewer
MSG_STATUS = "status"
MSG_PROGRESS = "progress"

# `welcome` keys ---------------------------------------------------------------
WELCOME_KEYS = (
    "type", "protocol", "game_version", "slot", "name", "task", "max_steps",
    "step_deadline_seconds", "program_timeout_seconds", "api_docs", "episode",
)
# `welcome.episode`: episode parameters stated outright at t=0 (policies
# must never infer them from play).
EPISODE_KEYS = (
    "game_version", "variant_task_key", "max_steps", "step_deadline_seconds",
    "program_timeout_seconds", "strike_limit", "seats", "slot", "map_bounds",
    "starting_inventory", "fast", "game_speed",
)
# `welcome.task`
TASK_KEYS = ("key", "goal_description", "agent_instructions")

# `observation` message and its `observation` object -------------------------
OBSERVATION_MESSAGE_KEYS = ("type", "step", "deadline_seconds", "observation")
OBSERVATION_KEYS = (
    "raw_text", "entities", "inventory", "flows", "score", "game_info",
    "task_verification", "last_program", "messages",
)
GAME_INFO_KEYS = ("tick", "time", "speed")
FLOWS_KEYS = ("input", "output", "harvested", "crafted")
LAST_PROGRAM_KEYS = ("code", "output", "error")
TASK_VERIFICATION_KEYS = ("success", "meta")

# `program` reply ---------------------------------------------------------------
PROGRAM_KEYS = ("type", "step", "code")
MAX_CODE_CHARS = 64 * 1024

# `done` message ----------------------------------------------------------------
DONE_KEYS = ("type", "result")

# Results document (closed schema; == manifest results_schema) ---------------
RESULT_KEYS = (
    "names", "scores", "production_scores", "throughputs", "task_key",
    "steps_completed", "error_steps", "noop_steps", "dead_seats",
    "noop_causes", "final_ticks", "end_reason", "wall_clock_seconds",
)
END_REASONS = ("steps_cap", "wall_clock", "sim_fault")
NOOP_CAUSES = ("timeout", "malformed", "wrong_step", "disconnected",
               "host_error")

# Global viewer status snapshot -------------------------------------------------
STATUS_KEYS = ("type", "game_version", "players", "task", "max_steps",
               "steps", "scores", "done")
PROGRESS_KEYS = ("type", "slot", "step", "score")

# Runtime env vars ---------------------------------------------------------------
ENV_PLAYER_WS_URL = "COWORLD_PLAYER_WS_URL"
ENV_PLAYER_WS_URL_LEGACY = "COGAMES_ENGINE_WS_URL"
