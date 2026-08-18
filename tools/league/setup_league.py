"""Configure the hosted Factorio league on the Softmax platform ladder.

Idempotent: safe to re-run. Requires a Softmax *team* login (`uv run softmax
login`) — league routes need `X-Use-Elevated-Privileges`.

Steps (mirrors cogame-nmmo/docs/DEPLOYMENT.md, adapted to 2-seat episodes):
  1. league seed for coworld "factorio" (`coworld league create`, once)
  2. PUT  /v2/leagues/{id}/divisions           one "Competition" division
  3. POST /v2/leagues/{id}/settings            ladder: swiss_neighbor over 2 seats,
                                               fillers when short, Elo k=32, 30-min rounds,
                                               variant rotation open_play x2 + iron_plate_throughput
  4. POST /v2/leagues/{id}/filler-policies     the three baseline policy versions
  5. POST /v2/leagues/{id}/rounds-paused false

Usage:
  uv run python tools/league/setup_league.py [--league league_...] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from coworld.api_client import CoworldApiClient

SERVER = "https://softmax.com/api"
COWORLD_NAME = "factorio"
FILLERS = [
    ("factorio-burner-baseline", "Burner Baseline"),
    ("factorio-handcraft-baseline", "Handcraft Baseline"),
    ("factorio-idle-baseline", "Idle Baseline"),
]
LADDER = {
    "enabled": True,
    "scheduler": {
        "strategy": "swiss_neighbor",
        "insufficient_players": "filler_policy",
        "min_episodes_per_entrant": 2,
        "neighbor_window": 2,
        "variant_rotation": ["open_play", "open_play", "iron_plate_throughput"],
    },
    "fulfillment": {"allowed_failures": 0.05, "retry_times": 2},
    "ranking": {"algorithm": "elo", "initial_rating": 1500, "k_factor": 32, "round_scoring_rule": "mean"},
    "divisions": [],  # filled with the Competition division id
}
ROUND_INTERVAL_MINUTES = 30


def _client() -> CoworldApiClient:
    CoworldApiClient.set_elevated(True)
    return CoworldApiClient.from_login(server_url=SERVER)


def _find_league(client: CoworldApiClient) -> str | None:
    for league in client.list_leagues():
        game = league.game
        gname = (getattr(game, "name", None) or getattr(game, "coworld_name", None) or "").lower()
        if gname == COWORLD_NAME and league.disabled_at is None:
            return league.id
    return None


def _create_league() -> None:
    cmd = [
        "coworld", "league", "create", COWORLD_NAME,
        "--set", "commissioner_key=platform",
        "--set", "minimum_champions=1",
        "--set", f"schedule_interval_minutes={ROUND_INTERVAL_MINUTES}",
    ]
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _policy_version_ids(client: CoworldApiClient) -> dict[str, str]:
    """Latest version id per filler policy name (owned by the logged-in user)."""
    out: dict[str, str] = {}
    for name, _ in FILLERS:
        row = client.lookup_policy_version(name=name)
        if row is not None:
            out[name] = str(row.resolved_id)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = _client()
    league_id = args.league or _find_league(client)
    if league_id is None:
        if args.dry_run:
            print("no league yet; would create")
            return 0
        _create_league()
        league_id = _find_league(client)
        if league_id is None:
            print("league not found after create", file=sys.stderr)
            return 1
    print("league:", league_id)

    # 2. divisions
    topo = client._request(  # noqa: SLF001
        "PUT", f"/v2/leagues/{league_id}/divisions", dict,
        json={"divisions": [{"name": "Competition", "level": 1, "type": "competition"}]},
    ) if not args.dry_run else {}
    print("divisions:", json.dumps(topo)[:400])
    divisions = client.list_divisions(league_id=league_id)
    comp = next(d for d in divisions if d.name == "Competition")
    division_id = comp.id
    print("division:", division_id)

    # 3. settings (GET → merge → POST: POST replaces the whole document)
    current = client._get(f"/v2/leagues/{league_id}/settings", dict)  # noqa: SLF001
    settings = dict(current.get("settings") or {})
    ladder = json.loads(json.dumps(LADDER))
    ladder["divisions"] = [
        {"division_id": division_id, "name": "Competition", "disqualify_after_consecutive_failures": 3}
    ]
    settings["ladder"] = ladder
    settings["round_interval_minutes"] = ROUND_INTERVAL_MINUTES
    print("settings ->", json.dumps(settings, indent=1)[:1500])
    if not args.dry_run:
        resp = client._post(f"/v2/leagues/{league_id}/settings", dict, json=settings)  # noqa: SLF001
        print("effective ladder:", json.dumps(resp.get("effective_ladder_config", {}))[:600])

    # 4. fillers
    ids = _policy_version_ids(client)
    missing = [n for n, _ in FILLERS if n not in ids]
    if missing:
        print("missing filler policy versions:", missing, file=sys.stderr)
        return 1
    fillers = [{"policy_version_id": ids[n], "display_name": disp} for n, disp in FILLERS]
    print("fillers ->", json.dumps(fillers))
    if not args.dry_run:
        r = client._post(f"/v2/leagues/{league_id}/filler-policies", dict, json={"filler_policies": fillers})  # noqa: SLF001
        print("fillers:", json.dumps(r)[:600])
        r = client._post(f"/v2/leagues/{league_id}/rounds-paused", dict, json={"paused": False})  # noqa: SLF001
        print("rounds-paused:", r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
