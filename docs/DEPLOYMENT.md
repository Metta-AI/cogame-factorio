# Hosted deployment (Softmax)

The Factorio Coworld runs as a live platform-ladder league on Softmax. This
file records the production identifiers, the league configuration, and how to
re-verify the deployment. Deployed 2026-08-18.

## Identifiers

| Thing | Id |
| --- | --- |
| Coworld `factorio:0.1.0` (bootstrap upload, canonical) | `cow_4550f2bd-56c1-4b22-88ef-068f9c39bdf6` |
| Manifest hash (0.1.0) | `sha256:7abebb379357efbac3d1ff0aca6f3c1a5fd33238819b9e73e97f4bbb5f4f8e85` |
| League seed | `lseed_d00cf96f-6a26-47ce-9c62-f709730996da` (`league_key` `factorio`) |
| League (display name "Factorio") | `league_09df6929-74d3-45ae-8857-4bb69d2880d1` |
| Division (Competition, level 1) | `div_312c1500-8497-4aab-8d3f-3663513a9d79` |
| Player `daveey` (main) | `ply_44ae9048-3242-4654-881f-6d9d43347fa3` |
| Player `daveey-1` | `ply_bac48eb1-662e-44f8-973d-f3e016dccf5d` |
| Filler `factorio-burner-baseline:v2` | `ce2dd112-af43-49c8-907b-62f5d6f40726` ("Burner Baseline") |
| Filler `factorio-handcraft-baseline:v2` | `ac7a4e55-b989-4d96-9dcb-1702c39d716e` ("Handcraft Baseline") |
| Filler `factorio-idle-baseline:v2` | `b7e47eaf-65d0-46d4-812c-f46fde1b389f` ("Idle Baseline") |
| Champion (daveey) `factorio-burner-baseline:v2` membership | `lpm_da1b3573-17ce-43a9-bbd5-3e82172c7b14` |
| Champion (daveey-1) `handcraft-baseline:v2` | policy version `99c3a780-64a6-46e6-aa62-eaad1b11a0a2` |
| GitHub | https://github.com/Metta-AI/cogame-factorio |

The canonical Coworld version advances on every green push to `main` (CI
`upload-coworld` job, once the `SOFTMAX_TOKEN` repo secret is set); the league
resolves the coworld by name, so `cow_` ids above are historical snapshots.

## League configuration

Platform ladder (Temporal), `commissioner_key: platform`, single Competition
division. Applied by `tools/league/setup_league.py` (idempotent; GET → merge →
POST because `POST /v2/leagues/{id}/settings` replaces the whole document):

- `round_interval_minutes: 30`
- `ladder.enabled: true`
- scheduler: `swiss_neighbor`, `insufficient_players: filler_policy`,
  `min_episodes_per_entrant: 2`, `neighbor_window: 2`,
  `variant_rotation: [open_play, open_play, iron_plate_throughput]`
  (2-seat episodes; each seat plays its own Factorio server)
- fulfillment: `allowed_failures: 0.05`, `retry_times: 2`
- ranking: `elo`, initial 1500, k 32, `round_scoring_rule: mean`
- divisions: Competition, `disqualify_after_consecutive_failures: 3`
- filler policies: the three baselines above (display names "Burner/Handcraft/Idle
  Baseline"); filler seats are excluded from ranking.

**One player identity = one entrant.** The ladder champions one policy per
player, so a real 1v1 needs two identities (accounts are capped at 2 active
players): `daveey` champions the burner baseline, `daveey-1` the handcraft
baseline; idle is filler-only.

League routes need a team credential with `X-Use-Elevated-Privileges: true`
(`CoworldApiClient.set_elevated(True)` in the setup script).

## Verification evidence (2026-08-18)

- Local `coworld certify`: all 10 steps pass (3-seat fixture: Burner 1505,
  Handcraft 1117, Idle 0 production score after 4 steps, 31 s wall).
- Hosted smoke certification for 0.1.0: passed (5 smoke episodes); canonical.
- Round `round_ef52dcc1` (first, 2 episodes, `ereq_c4c603ce`, `ereq_adde610c`):
  30 steps per seat in ~122 s wall, Idle 0 vs Burner Baseline 17,335 production
  score, `end_reason: steps_cap`, replays uploaded to
  `softmax-public.s3.amazonaws.com/replays/…`.
- Hosted static viewer URL for `ereq_c4c603ce`
  (`coworld replay-open <ereq> --hosted --no-open-browser`) served HTTP 200 and
  rendered the map, standings and end card. Known issue at 0.1.0: first frame
  takes >12 s on a 30-step replay so the "didn't load" card fires and is not
  dismissed once frames arrive — fixed in the next viewer bundle.

## How to re-verify

```bash
uv run coworld list                     # factorio versions; exactly one canonical
uv run coworld status cow_4550f2bd-56c1-4b22-88ef-068f9c39bdf6
uv run coworld rounds --league league_09df6929-74d3-45ae-8857-4bb69d2880d1
uv run coworld episodes -r <round_id>   # 2-seat episodes, replay URLs
uv run coworld results div_312c1500-8497-4aab-8d3f-3663513a9d79   # Elo standings
uv run coworld replay-open <ereq_> --hosted   # static wasm viewer
uv run python tools/league/setup_league.py --dry-run
```

Pause / resume: `POST /v2/leagues/{league_id}/rounds-paused {"paused": true|false}`;
`POST /v2/leagues/{league_id}/trigger-round` starts a round now.
