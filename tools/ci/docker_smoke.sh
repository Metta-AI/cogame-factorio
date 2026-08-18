#!/usr/bin/env bash
# Raw-Docker episode smoke (Coworld Cookbook shape): one game container
# (which spawns its own Factorio servers) + one player container per seat
# on the coworld-local network with file:// artifact URIs. Asserts the
# episode completes (exit 0) and writes results.json with the manifest's
# CLOSED result key set plus a well-formed replay.
#
# usage: tools/ci/docker_smoke.sh [image]   (default cogame-factorio:ci)
# env:   SMOKE_PLAYERS="players.idle_player players.burner_player"
#        (module per seat; default idle + burner)
#        SMOKE_TIMEOUT=900 (seconds to wait for the game container)
set -euo pipefail

image="${1:-cogame-factorio:ci}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/factorio-smoke.XXXXXX")"
run_id="$$"
players=(${SMOKE_PLAYERS:-players.idle_player players.burner_player})
timeout_s="${SMOKE_TIMEOUT:-900}"
nseats="${#players[@]}"

cleanup() {
  docker ps -aq --filter "name=factorio-smoke-${run_id}" | xargs -r docker rm -f >/dev/null 2>&1 || true
  rm -rf "${work_dir}"
}
trap cleanup EXIT

dump_logs() {
  echo "---- game container logs ----" >&2
  docker logs "factorio-smoke-${run_id}-game" 2>&1 | tail -80 >&2
  for ((slot = 0; slot < nseats; slot++)); do
    echo "---- player ${slot} container logs ----" >&2
    docker logs "factorio-smoke-${run_id}-p${slot}" 2>&1 | tail -30 >&2
  done
}

python3 - "${work_dir}/config.json" "${nseats}" <<'EOF'
import json, sys
path, n = sys.argv[1], int(sys.argv[2])
json.dump({
    "players": [{"name": f"smoke-{i}"} for i in range(n)],
    "tokens": [f"token-{i}" for i in range(n)],
    "num_agents": n,
    "task": "open_play",
    "max_steps": 3,
    "step_deadline_seconds": 60,
    "program_timeout_seconds": 45,
    "player_connect_timeout_seconds": 120,
    "wall_clock_budget_seconds": 600,
}, open(path, "w"), indent=2)
EOF
chmod 777 "${work_dir}"

docker network inspect coworld-local >/dev/null 2>&1 || docker network create coworld-local

docker run -d --name "factorio-smoke-${run_id}-game" \
  --network coworld-local --network-alias "factorio-smoke-${run_id}" \
  -e COGAME_HOST=0.0.0.0 -e COGAME_PORT=8080 \
  -e COGAME_CONFIG_URI=file:///coworld/config.json \
  -e COGAME_RESULTS_URI=file:///coworld/results.json \
  -e COGAME_SAVE_REPLAY_URI=file:///coworld/replay.json \
  -e COGAME_PLAYER_FAILURE_URI=file:///coworld/player_failure.json \
  -v "${work_dir}:/coworld:rw" \
  "${image}" >/dev/null

for ((slot = 0; slot < nseats; slot++)); do
  docker run -d --name "factorio-smoke-${run_id}-p${slot}" --network coworld-local \
    -e COWORLD_PLAYER_WS_URL="ws://factorio-smoke-${run_id}:8080/player?slot=${slot}&token=token-${slot}" \
    "${image}" python -m "${players[$slot]}" >/dev/null
done

echo "waiting for the episode (game container exit, up to ${timeout_s}s) ..."
deadline=$((SECONDS + timeout_s))
while docker ps -q --filter "name=factorio-smoke-${run_id}-game" | grep -q .; do
  if (( SECONDS > deadline )); then
    echo "FAIL: game container did not exit within ${timeout_s}s" >&2
    dump_logs
    exit 1
  fi
  sleep 3
done

exit_code="$(docker inspect -f '{{.State.ExitCode}}' "factorio-smoke-${run_id}-game")"
if [[ "${exit_code}" != "0" ]]; then
  echo "FAIL: game container exited ${exit_code}" >&2
  dump_logs
  exit 1
fi

if ! python3 - "${work_dir}" "${nseats}" <<'EOF'
import json, sys
from pathlib import Path

work = Path(sys.argv[1])
n = int(sys.argv[2])
results = json.loads((work / "results.json").read_text())
expected = {
    "names", "scores", "production_scores", "throughputs", "task_key",
    "steps_completed", "error_steps", "noop_steps", "dead_seats",
    "noop_causes", "final_ticks", "end_reason", "wall_clock_seconds",
}
assert set(results) == expected, f"results keys drifted: {sorted(set(results) ^ expected)}"
assert len(results["scores"]) == n, results["scores"]
assert results["end_reason"] == "steps_cap", results["end_reason"]
# Every player must have actually played every step: a broken player
# entrypoint shows up as noops / a dead seat and must fail the smoke.
assert results["noop_steps"] == [0] * n, results["noop_steps"]
assert results["dead_seats"] == [False] * n, results["dead_seats"]
assert results["steps_completed"] == [3] * n, results["steps_completed"]
replay = json.loads((work / "replay.json").read_text())
assert replay["format"] == "cogame-factorio-replay", replay.get("format")
assert replay["version"] == 1
assert replay["result"] == results
assert len(replay["seats"]) == n
assert all(len(s["steps"]) == 3 for s in replay["seats"]), [len(s["steps"]) for s in replay["seats"]]
assert replay["map"]["resources"], "terrain capture empty"
assert not (work / "player_failure.json").exists(), "player failure reported"
print(f"smoke OK: end_reason={results['end_reason']} scores={results['scores']} "
      f"steps={results['steps_completed']} replay={(work / 'replay.json').stat().st_size}B")
EOF
then
  echo "FAIL: results/replay assertions failed" >&2
  dump_logs
  exit 1
fi
