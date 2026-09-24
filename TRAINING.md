# Factorio training

The three certified variants are `open_play`, `iron_plate_throughput`, and
`solo`. Training uses the production `FactorioSession`, its per-seat FLE server,
the production observations, and the maintained idle, handcraft, and burner
programs. The numeric action catalog also contains a 30-second wait program.
Each action is a valid Python program sent to FLE. The bridge keeps a separate
session and policy state for each seat, matching the hosted game's independent
Factorio servers. It reports the hosted production score, or the FLE holdout
throughput for the throughput variant.

Start one FLE server per seat. The certified two-seat variants need two:

```sh
uv sync
FLE_STATE_DIR="$PWD/tmp/fle-state" FLE_WORKDIR="$PWD/tmp/fle" uv run fle cluster start -n 2
export COGAME_FACTORIO_SERVERS=localhost:27000,localhost:27001
uv run pytest tests/test_train_bridge.py -q
```

`tools/train_bridge.py MANIFEST VARIANT [MAX_STEPS]` implements the persistent
Coworld decision protocol. Its 48 numeric values are the visible step, score,
tick, last-program error, selected inventory and flow counts, and entity counts.
`semantic_view` carries the entire hosted seat observation, including FLE's
program output. The four choices are idle, handcraft, burner, and wait. The
numeric learner controls seat 0; the teacher method runs the burner baseline
for other seats. Pass this command to `recipes.external.coworld.train` for
native PufferLib or `recipes.external.coworld_metta_rl.train` for Metta RL,
with `players=2` or `players=1` for `solo` and a timestep limit. A smaller
`MAX_STEPS` is useful for a training curriculum; omit it for complete games.

Factorio's lab map has no Coworld seed field. Resetting a session resets its
FLE task; rollout variation comes from policy choices. Each seat requires its
own FLE server. Program execution and task verification are much slower than
the pure simulators used by other Coworlds.
The solo variant supplies the bounded utility `2 * score / (score + 1000) - 1`
for reinforcement learning; its raw production score remains in `scores`.

For Metta post-training, export complete games with the maintained policies:

```sh
uv run python tools/export_posttrain.py /tmp/factorio-posttrain 10 solo
```

Use `open_play` or `iron_plate_throughput` for the two-seat variants. The
exporter uses handcraft setup in half the games, occasional idle steps, and
burner programs for the remaining steps. It records the exact `LLMPolicy`
system and user prompts, including FLE's
API reference and four recent program/output pairs. The completion is the
program accepted by FLE in the same fenced format the hosted player requests.
Whole games are split by episode into `train.jsonl` and `validation.jsonl`.
These prompts are long: the initial FLE reference was 26,895 tokens with a
local Qwen2.5 tokenizer. Select a model and `--max-length` that can hold the
prompt and completion; the Metta post-training loader filters overlength rows.
The numeric macro policy and text program policy have different action spaces;
both execute on the same FLE session interface.
