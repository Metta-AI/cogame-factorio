# Hosted deployment (Softmax)

The Factorio Coworld runs as a live platform-ladder league on Softmax. This
file records the production identifiers, the league configuration, and how to
re-verify the deployment. Deployed 2026-08-18.

## Identifiers

| Thing | Id |
| --- | --- |
| Coworld `factorio:0.1.0` (bootstrap upload) | `cow_4550f2bd-56c1-4b22-88ef-068f9c39bdf6` |
| Coworld `factorio:0.1.1` (hosted smoke failed 2/5 — see below; non-canonical) | `cow_19ad742f-decf-4131-8d26-c168cd881f60` |
| Coworld `factorio:0.1.2` (canonical: fixed viewer, game resource requests, session-start retries) | `cow_88bf8a92-2dee-4dce-9f96-d8e31af0df8a` |
| Manifest hash (0.1.2) | `sha256:404403428378a84d8ff78e4714678eb819280d9c65a60b069f1fd54de4ccc424` |
| League seed | `lseed_d00cf96f-6a26-47ce-9c62-f709730996da` (`league_key` `factorio`) |
| League (display name "Factorio") | `league_09df6929-74d3-45ae-8857-4bb69d2880d1` |
| Division (Competition, level 1) | `div_312c1500-8497-4aab-8d3f-3663513a9d79` |
| Player `daveey` (main) | `ply_44ae9048-3242-4654-881f-6d9d43347fa3` |
| Player `daveey-1` | `ply_bac48eb1-662e-44f8-973d-f3e016dccf5d` |
| Filler `factorio-burner-baseline:v2` | `ce2dd112-af43-49c8-907b-62f5d6f40726` ("Burner Baseline") |
| Filler `factorio-handcraft-baseline:v2` | `ac7a4e55-b989-4d96-9dcb-1702c39d716e` ("Handcraft Baseline") |
| Filler `factorio-idle-baseline:v2` | `b7e47eaf-65d0-46d4-812c-f46fde1b389f` ("Idle Baseline") |
| Champion (daveey) `factorio-claude:v2` (Claude via Bedrock sidecar) | policy version `2bd3de0f-0bed-4b17-9202-168d956564f9` |
| Champion (daveey-1) `burner-baseline:v1` | policy version `72e5dede-c559-4180-9ea3-fc5907a62bd5` |
| Earlier entrants (benched/retired) | `factorio-burner-baseline:v2` `lpm_da1b3573…`, `handcraft-baseline:v2` `99c3a780…` |
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
players; `coworld player use <ply_id>` — by id, the name form did not resolve):
`daveey` champions `factorio-claude` (players/llm_player.py, Claude on Bedrock),
`daveey-1` the burner baseline; handcraft/idle are filler-only.

**Hosted Bedrock.** Pods get a Bedrock *sidecar*: `AWS_ENDPOINT_URL_BEDROCK_RUNTIME`
+ `AWS_BEARER_TOKEN_BEDROCK` (set by `coworld upload-policy --use-bedrock
--bedrock-model us.anthropic.claude-sonnet-4-6`). The anthropic SDK's IAM path
403s there ("Invalid API Key format"); `llm_player` uses a raw InvokeModel client
against the sidecar (`_BedrockHttpClient`), pinned model first then the
candidate list. Verified round 9–10: Claude 29.9k–53.7k vs burner 14.7k–19.7k.

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
  rendered the map, standings and end card. 0.1.0's bundle took >12 s to first
  frame on a 30-step replay (the "didn't load" card fired and stuck); 0.1.1
  ships the fixed viewer (first frame ~1 s, card dismissed on first frame,
  top-bar chrome, character focus/follow, collapsible right pane).
- Rounds self-start every 30 minutes (round 10 at 20:06Z, 30 min after the
  manually triggered round 9). Rounds 9–10 (Claude vs burner, 2 episodes each):
  Claude 40,904 / 53,696 / 43,490 / 29,908 vs burner 17,427 / 17,427 / 19,739 /
  14,675 production score; Elo after round 10: daveey 1529, daveey-1 1471.
- 0.1.1's hosted smoke failed 2/5 episodes at session start with FLE
  `KeyError: 'ingredients'` (`task.setup` → `GameState.from_instance` →
  `_save_research_state`: a large RCON reply arriving malformed under load; the
  platform truncates the game log so only local certify showed the traceback).
  0.1.2 retries session start with a fresh instance (3 attempts) and declares
  game resource requests (3 cpu / 4Gi / 4Gi ephemeral; the platform default was
  1 cpu / 512Mi and a cpu *limit* on the game runnable is rejected). 0.1.2 hosted
  smoke: 5/5, canonical.
- One platform oddity seen once (`ereq_c854cf17`, round 6): the episode request
  was marked completed 5 s after creation with no artifacts while its pod played
  7 steps before teardown — a platform-side dedupe/teardown, not a game fault
  (its sibling episode completed normally).

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
