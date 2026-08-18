"""Print the Factorio league's recent rounds, their episodes (participants, scores,
replay), and the division standings. Read-only.

  uv run python tools/league/status.py [--rounds N] [--league league_...]
"""

from __future__ import annotations

import argparse

from coworld.api_client import CoworldApiClient

SERVER = "https://softmax.com/api"
LEAGUE = "league_09df6929-74d3-45ae-8857-4bb69d2880d1"
DIVISION = "div_312c1500-8497-4aab-8d3f-3663513a9d79"


def _rows(obj):
    if isinstance(obj, list):
        return obj
    for key in ("rounds", "entries", "items", "episodes"):
        if isinstance(obj, dict) and isinstance(obj.get(key), list):
            return obj[key]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--league", default=LEAGUE)
    ap.add_argument("--division", default=DIVISION)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    client = CoworldApiClient.from_login(server_url=SERVER)

    rounds = _rows(client._get("/v2/rounds", object, params={"league_id": args.league, "limit": 50}))  # noqa: SLF001
    rounds = sorted(rounds, key=lambda r: r.get("created_at") or "", reverse=True)[: args.rounds]
    for rnd in rounds:
        print(f"round {rnd.get('id')} #{rnd.get('round_number', '?')} {rnd.get('status')} created {rnd.get('created_at')}")
        eps = _rows(client._get("/v2/episode-requests", object, params={"round_id": rnd["id"], "limit": 50}))  # noqa: SLF001
        for e in eps:
            # `scores` is keyed by policy_version_id, not seat order.
            by_pv = {s.get("policy_version_id"): s.get("score") for s in (e.get("scores") or [])}
            parts = []
            for p in e.get("participants") or []:
                sc = by_pv.get(p.get("policy_version_id"))
                parts.append(f"{p.get('policy_name')}@{p.get('player_name')}={'-' if sc is None else round(sc, 1)}")
            print(f"   {e.get('id')} {e.get('status'):10} {parts} replay={'yes' if e.get('replay_url') else '-'}")
    print("standings:")
    for row in client.get_division_leaderboard(args.division):
        print("  ", row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
