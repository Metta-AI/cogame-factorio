#!/usr/bin/env python3
"""Export complete FLE games in the hosted LLM player's prompt format."""

import json
import subprocess
import sys
from pathlib import Path

from train_bridge import Bridge
from players.llm_player import LLMPolicy


def main() -> None:
    assert len(sys.argv) == 4, "usage: export_posttrain.py OUTPUT EPISODES VARIANT"
    output = Path(sys.argv[1]).resolve()
    episodes = int(sys.argv[2])
    variant = sys.argv[3]
    assert episodes >= 10
    output.mkdir(parents=True, exist_ok=False)
    bridge = Bridge(Path(__file__).resolve().parents[1] / "coworld_manifest_template.json", variant)
    train_rows = []
    validation_rows = []
    runs = []
    try:
        for episode in range(1, episodes + 1):
            observation = bridge.reset({"players": bridge.config.num_seats,
                                        "seed": f"factorio-{variant}-{episode}"})
            prompts = [LLMPolicy(provider="none") for _ in bridge.sessions]
            for seat, session in enumerate(bridge.sessions):
                prompts[seat].on_welcome({
                    "api_docs": session.system_prompt(), "task": session.task_info(),
                    "episode": {"max_steps": bridge.config.max_steps,
                                "step_deadline_seconds": bridge.config.step_deadline_seconds,
                                "program_timeout_seconds": bridge.config.program_timeout_seconds,
                                "starting_inventory": session.starting_inventory()},
                })
            rows = []
            while observation["kind"] == "decision":
                seat = observation["seat"]
                prompt = prompts[seat]
                messages = [
                    {"role": "system", "content": prompt._system_prompt()},
                    {"role": "user", "content": prompt._user_prompt(
                        observation["turn"], observation["semantic_view"])},
                ]
                action = {"action": 2 if (episode + observation["seat"]) % 2 else 1}
                accepted = bridge.step({"decision_id": observation["decision_id"],
                                        "response": json.dumps(action)})
                rows.append(json.dumps({
                    "episode_id": f"factorio-{variant}-{episode}",
                    "seed": f"factorio-{variant}-{episode}",
                    "decision_id": observation["decision_id"],
                    "prompt": messages,
                    "completion": [{"role": "assistant", "content":
                                    f"```python\n{accepted['program']}\n```"}],
                    "game": "factorio", "action_schema_revision": "factorio-program-v1",
                }))
                observation = accepted["observation"]
            (validation_rows if episode % 5 == 0 else train_rows).extend(rows)
            runs.append({"episode": episode, "decisions": len(rows),
                         "scores": observation["scores"]})
            print(f"episode {episode}/{episodes}: {len(rows)} decisions, "
                  f"scores={observation['scores']}", flush=True)
    finally:
        bridge.close()
    (output / "train.jsonl").write_text("\n".join(train_rows) + "\n")
    (output / "validation.jsonl").write_text("\n".join(validation_rows) + "\n")
    (output / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "game": "factorio", "variant": variant,
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "teacher": "alternating-burner-and-handcraft", "train_examples": len(train_rows),
        "validation_examples": len(validation_rows), "runs": runs,
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
