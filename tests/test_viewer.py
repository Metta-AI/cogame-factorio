"""Static wasm replay viewer checks (viewer/, tools/build_replay_viewer.sh).

Layers, cheapest first:

- fixture contract: tests/fixtures/synthetic_replay.json (and
  tests/fixtures/sample_replay.json when the server agent has recorded a
  real one) conform to docs/REPLAY.md v1 - a small structural validator
  lives here so the viewer's only input has a tripwire independent of
  the JS;
- page contract: viewer/index.html reads the `replay` query parameter,
  falls back to /replay-data, references bundle assets relatively only,
  wires Module.onAbort to a visible error, honours ?spoilers=0 and relays
  Escape to the embedding shell;
- build hook: tools/build_replay_viewer.sh asserts every bundle file;
- JS packing (node): tests/viewer_pack_harness.js exercises
  viewer/replay_pack.js against the fixture (skipped without node);
- build outputs + headless smoke: viewer/dist/{index.html,replay_pack.js,
  viewer.js,viewer.wasm} and build/viewer_core.{js,wasm} exist after
  viewer/build_viewer.sh, and viewer/smoke.cjs drives the headless wasm
  under node (address-space canary included). Both skip when the wasm
  build is absent unless COGAME_REQUIRE_WASM_BUILD=1, which turns the skip
  into a failure (CI rule).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from numbers import Real
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VIEWER_DIR = REPO_ROOT / "viewer"
VIEWER_DIST = VIEWER_DIR / "dist"
VIEWER_CORE_JS = REPO_ROOT / "build" / "viewer_core.js"
FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_replay.json"
SAMPLE = FIXTURES / "sample_replay.json"
PACK_HARNESS = Path(__file__).parent / "viewer_pack_harness.js"
SMOKE = VIEWER_DIR / "smoke.cjs"

BUNDLE_FILES = ("index.html", "replay_pack.js", "viewer.js", "viewer.wasm")
NOT_BUILT = "viewer not built - run viewer/build_viewer.sh first"

FLE_DIRECTIONS = {-1, 0, 1, 2, 3, 4, 5, 6, 7}


# --------------------------------------------------------------------------
# REPLAY.md v1 structural validator
# --------------------------------------------------------------------------

def _num(v) -> bool:
    return isinstance(v, Real) and not isinstance(v, bool)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fail(path: str, msg: str):
    raise AssertionError(f"replay {path}: {msg}")


def validate_replay(doc: dict) -> None:
    """Assert `doc` conforms to docs/REPLAY.md (format v1).

    Raises AssertionError naming the offending path. Mirrors the parts
    the viewer depends on plus the documented per-step fields.
    """
    if not isinstance(doc, dict):
        _fail("$", "not an object")
    if doc.get("format") != "cogame-factorio-replay":
        _fail("format", f"{doc.get('format')!r} != 'cogame-factorio-replay'")
    if doc.get("version") != 1:
        _fail("version", f"{doc.get('version')!r} != 1")
    if not isinstance(doc.get("config"), dict):
        _fail("config", "missing/not an object")
    if "tokens" in doc["config"]:
        _fail("config.tokens", "tokens must be excluded from the replay")
    names = doc.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        _fail("names", "must be a non-empty list of strings")
    task = doc.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("key"), str):
        _fail("task", "must be an object with a string key")
    if "game_version" in doc and not isinstance(doc["game_version"], str):
        _fail("game_version", "must be a string when present")

    m = doc.get("map")
    if not isinstance(m, dict):
        _fail("map", "missing/not an object")
    b = m.get("bounds")
    if not isinstance(b, dict) or not all(_num(b.get(k)) for k in ("x0", "y0", "x1", "y1")):
        _fail("map.bounds", "needs numeric x0,y0,x1,y1")
    if not (b["x1"] > b["x0"] and b["y1"] > b["y0"]):
        _fail("map.bounds", "must be a non-empty half-open box")
    res = m.get("resources")
    if not isinstance(res, list):
        _fail("map.resources", "missing/not a list")
    for i, r in enumerate(res):
        if not (isinstance(r, list) and len(r) == 4 and isinstance(r[0], str)
                and _num(r[1]) and _num(r[2]) and _num(r[3])):
            _fail(f"map.resources[{i}]", f"expected [name, tile_x, tile_y, amount], got {r!r}")
    water = m.get("water")
    if not isinstance(water, list):
        _fail("map.water", "missing/not a list")
    for i, w in enumerate(water):
        if not (isinstance(w, list) and len(w) == 3 and all(_num(v) for v in w) and w[2] >= 1):
            _fail(f"map.water[{i}]", f"expected [tile_x, tile_y, run_length_east>=1], got {w!r}")
    trees = m.get("trees", [])
    if not isinstance(trees, list):
        _fail("map.trees", "not a list")
    for i, t in enumerate(trees):
        if not (isinstance(t, list) and len(t) == 2 and _num(t[0]) and _num(t[1])):
            _fail(f"map.trees[{i}]", f"expected [tile_x, tile_y], got {t!r}")
    spawn = m.get("spawn")
    if spawn is not None and not (isinstance(spawn, dict) and _num(spawn.get("x")) and _num(spawn.get("y"))):
        _fail("map.spawn", "must be {x, y}")

    seats = doc.get("seats")
    if not isinstance(seats, list) or not seats:
        _fail("seats", "must be a non-empty list")
    if len(seats) != len(names):
        _fail("seats", f"{len(seats)} seats but {len(names)} names")
    for si, seat in enumerate(seats):
        p = f"seats[{si}]"
        if not isinstance(seat, dict):
            _fail(p, "not an object")
        if seat.get("slot") != si:
            _fail(f"{p}.slot", f"{seat.get('slot')!r} != {si} (seat order)")
        if not isinstance(seat.get("name"), str):
            _fail(f"{p}.name", "missing string")
        if not _num(seat.get("final_score")):
            _fail(f"{p}.final_score", "missing number")
        if not isinstance(seat.get("dead"), bool):
            _fail(f"{p}.dead", "missing bool")
        steps = seat.get("steps")
        if not isinstance(steps, list):
            _fail(f"{p}.steps", "missing list")
        for ki, st in enumerate(steps):
            q = f"{p}.steps[{ki}]"
            _validate_step(q, st)
            if st["step"] != ki:
                _fail(f"{q}.step", f"{st['step']} != index {ki} (steps are dense, in order)")

    result = doc.get("result")
    if not isinstance(result, dict):
        _fail("result", "missing/not an object")
    for key in ("names", "scores", "task_key", "end_reason"):
        if key not in result:
            _fail(f"result.{key}", "missing (results_schema key)")
    if result["end_reason"] not in ("steps_cap", "wall_clock", "sim_fault"):
        _fail("result.end_reason", f"{result['end_reason']!r} not in enum")
    if list(result["names"]) != list(names):
        _fail("result.names", "must equal top-level names")


def _validate_step(q: str, st) -> None:
    if not isinstance(st, dict):
        _fail(q, "not an object")
    if not _int(st.get("step")) or st["step"] < 0:
        _fail(f"{q}.step", "missing non-negative int")
    if not isinstance(st.get("code"), str):
        _fail(f"{q}.code", "missing string")
    if not isinstance(st.get("noop"), bool):
        _fail(f"{q}.noop", "missing bool")
    if st["noop"] and st["code"] != "":
        _fail(f"{q}.code", "noop step must have empty code")
    if not isinstance(st.get("output"), str):
        _fail(f"{q}.output", "missing string")
    if not isinstance(st.get("error"), bool):
        _fail(f"{q}.error", "missing bool")
    if not _num(st.get("score")):
        _fail(f"{q}.score", "missing number")
    if not (st.get("throughput") is None or _num(st["throughput"])):
        _fail(f"{q}.throughput", "must be number or null")
    if not _int(st.get("tick")) or st["tick"] < 0:
        _fail(f"{q}.tick", "missing non-negative int")
    if not _num(st.get("wall_ms")) or st["wall_ms"] < 0:
        _fail(f"{q}.wall_ms", "missing non-negative number")
    ch = st.get("character")
    if ch is not None and not (isinstance(ch, dict) and _num(ch.get("x")) and _num(ch.get("y"))):
        _fail(f"{q}.character", "must be {x, y} or null")
    ents = st.get("entities")
    if not isinstance(ents, list):
        _fail(f"{q}.entities", "missing list")
    for ei, e in enumerate(ents):
        if not (isinstance(e, list) and len(e) == 7):
            _fail(f"{q}.entities[{ei}]", f"expected 7-element row, got {e!r}")
        name, x, y, d, status, w, h = e
        if not isinstance(name, str) or not name:
            _fail(f"{q}.entities[{ei}][0]", "name must be a non-empty string")
        if not (_num(x) and _num(y)):
            _fail(f"{q}.entities[{ei}]", "x, y must be numbers")
        if not _int(d) or d not in FLE_DIRECTIONS:
            _fail(f"{q}.entities[{ei}][3]", f"direction {d!r} not in {sorted(FLE_DIRECTIONS)}")
        if not isinstance(status, str):
            _fail(f"{q}.entities[{ei}][4]", "status must be a string ('' allowed)")
        if not (_num(w) and _num(h) and w > 0 and h > 0):
            _fail(f"{q}.entities[{ei}]", "width, height must be positive numbers")
        if name in ("transport-belt", "fast-transport-belt", "express-transport-belt", "pipe"):
            _fail(f"{q}.entities[{ei}]", f"{name} tiles belong in belts/pipes, not entities")
    belts = st.get("belts")
    if not isinstance(belts, list):
        _fail(f"{q}.belts", "missing list")
    for bi, bt in enumerate(belts):
        if not (isinstance(bt, list) and len(bt) == 3 and _num(bt[0]) and _num(bt[1])
                and _int(bt[2]) and bt[2] in FLE_DIRECTIONS):
            _fail(f"{q}.belts[{bi}]", f"expected [x, y, direction], got {bt!r}")
    pipes = st.get("pipes")
    if not isinstance(pipes, list):
        _fail(f"{q}.pipes", "missing list")
    for pi, pp in enumerate(pipes):
        if not (isinstance(pp, list) and len(pp) == 2 and _num(pp[0]) and _num(pp[1])):
            _fail(f"{q}.pipes[{pi}]", f"expected [x, y], got {pp!r}")
    for key in ("inventory", "flows_output"):
        d = st.get(key)
        if not isinstance(d, dict):
            _fail(f"{q}.{key}", "missing object")
        for k, v in d.items():
            if not isinstance(k, str) or not _num(v):
                _fail(f"{q}.{key}[{k!r}]", "must map item name -> number")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _fixture_paths():
    paths = [SYNTHETIC]
    if SAMPLE.exists():
        paths.append(SAMPLE)
    return paths


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.name)
def test_fixture_conforms_to_replay_md(path: Path):
    doc = json.loads(path.read_text())
    validate_replay(doc)


def test_synthetic_fixture_is_rich_enough_for_the_viewer():
    """The synthetic replay must exercise every renderer feature: several
    seats, noop + error steps, a dead seat, all resource kinds, water,
    trees, belts, pipes, a character, and every status class."""
    doc = json.loads(SYNTHETIC.read_text())
    assert len(doc["seats"]) >= 2
    kinds = {r[0] for r in doc["map"]["resources"]}
    assert {"iron-ore", "copper-ore", "coal", "stone", "crude-oil"} <= kinds
    assert doc["map"]["water"] and doc["map"]["trees"]
    steps = [st for s in doc["seats"] for st in s["steps"]]
    assert any(st["noop"] for st in steps)
    assert any(st["error"] for st in steps)
    assert any(s["dead"] for s in doc["seats"])
    assert any(st["belts"] for st in steps) and any(st["pipes"] for st in steps)
    statuses = {e[4] for st in steps for e in st["entities"]}
    assert "working" in statuses and "no_fuel" in statuses and "full_output" in statuses
    names = {e[0] for st in steps for e in st["entities"]}
    for expected in ("burner-mining-drill", "stone-furnace", "iron-chest", "burner-inserter",
                     "small-electric-pole", "assembling-machine-1", "boiler", "steam-engine",
                     "offshore-pump", "lab", "electric-mining-drill"):
        assert expected in names, expected


def test_validator_rejects_malformed_docs():
    doc = json.loads(SYNTHETIC.read_text())

    def broken(mutate):
        d = json.loads(json.dumps(doc))
        mutate(d)
        return d

    with pytest.raises(AssertionError, match="format"):
        validate_replay(broken(lambda d: d.update(format="x")))
    with pytest.raises(AssertionError, match=r"entities\[0\]"):
        validate_replay(broken(lambda d: d["seats"][0]["steps"][0]["entities"].__setitem__(0, ["a", 1, 2])))
    with pytest.raises(AssertionError, match=r"direction"):
        validate_replay(broken(lambda d: d["seats"][0]["steps"][0]["entities"][0].__setitem__(3, 9)))
    with pytest.raises(AssertionError, match="tokens"):
        validate_replay(broken(lambda d: d["config"].__setitem__("tokens", ["t"])))
    with pytest.raises(AssertionError, match=r"steps\[1\]\.step"):
        validate_replay(broken(lambda d: d["seats"][0]["steps"][1].__setitem__("step", 5)))
    with pytest.raises(AssertionError, match="end_reason"):
        validate_replay(broken(lambda d: d["result"].__setitem__("end_reason", "bogus")))


# --------------------------------------------------------------------------
# page contract (static inspection of viewer/index.html)
# --------------------------------------------------------------------------

def _index_html() -> str:
    return (VIEWER_DIR / "index.html").read_text()


def test_index_reads_replay_query_param_and_falls_back_to_replay_data():
    html = _index_html()
    assert re.search(r'URLSearchParams\(location\.search\)', html)
    assert re.search(r'\.get\("replay"\)', html)
    assert '"/replay-data"' in html, "container replay mode fallback"


def test_index_references_bundle_assets_relatively_only():
    html = _index_html()
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    assert srcs, "no script tags"
    for src in srcs:
        assert src.startswith("./"), f"non-relative script src {src!r}"
    assert "./viewer.js" in srcs and "./replay_pack.js" in srcs
    # no absolute or remote asset URLs anywhere (fonts, CDNs, images)
    for m in re.finditer(r'(?:src|href)="([^"]+)"', html):
        url = m.group(1)
        assert not url.startswith(("http://", "https://", "//")), f"remote asset {url!r}"
    assert "@import" not in html and "fonts.googleapis" not in html
    # the wasm module is instantiated through the MODULARIZE factory
    assert "createFactorioViewer(" in html


def test_index_surfaces_failures_and_shell_hooks():
    html = _index_html()
    assert "onAbort" in html, "Module.onAbort must paint an error"
    assert 'id="banner"' in html and 'role="alert"' in html
    assert "viewer_stage_ptr" in html, "abort message includes the C stage note"
    assert 'role="status"' in html, "loading plate"
    assert "prefers-reduced-motion" in html
    assert 'get("spoilers")' in html
    assert 'src: "ctf-shell", type: "esc"' in html, "Escape relay for Observatory's TheaterOverlay"
    assert 'target = "_top"' in html, "failure card link opens the replay JSON directly"
    assert "game_version" in html


# --------------------------------------------------------------------------
# build hook + node-level checks
# --------------------------------------------------------------------------

def test_build_replay_viewer_hook_asserts_every_bundle_file():
    hook = (REPO_ROOT / "tools" / "build_replay_viewer.sh").read_text()
    assert "--target wasm-builder" in hook
    assert "/src/viewer/dist/." in hook
    for name in BUNDLE_FILES:
        assert name in hook, f"hook does not assert {name}"
    assert os.access(REPO_ROOT / "tools" / "build_replay_viewer.sh", os.X_OK)
    assert os.access(VIEWER_DIR / "build_viewer.sh", os.X_OK)


def _node() -> str | None:
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="node not installed")
def test_replay_pack_node_harness():
    proc = subprocess.run([_node(), str(PACK_HARNESS), str(SYNTHETIC)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "viewer_pack_harness: OK" in proc.stdout


def _skip_or_fail_not_built():
    """With COGAME_REQUIRE_WASM_BUILD set a missing build artifact is a
    failure, never a silent skip."""
    if os.environ.get("COGAME_REQUIRE_WASM_BUILD"):
        pytest.fail(NOT_BUILT + " (COGAME_REQUIRE_WASM_BUILD is set)")
    pytest.skip(NOT_BUILT)


def test_build_viewer_outputs_exist():
    if not (VIEWER_DIST / "viewer.wasm").exists():
        _skip_or_fail_not_built()
    for name in BUNDLE_FILES:
        assert (VIEWER_DIST / name).exists(), f"viewer/dist/{name} missing"
    assert VIEWER_CORE_JS.exists() and VIEWER_CORE_JS.with_suffix(".wasm").exists()
    # dist/index.html is the source page, byte for byte
    assert (VIEWER_DIST / "index.html").read_bytes() == (VIEWER_DIR / "index.html").read_bytes()
    assert (VIEWER_DIST / "replay_pack.js").read_bytes() == (VIEWER_DIR / "replay_pack.js").read_bytes()
    js = (VIEWER_DIST / "viewer.js").read_text(errors="replace")
    assert "createFactorioViewer" in js, "MODULARIZE export name"


def test_headless_wasm_smoke():
    if not VIEWER_CORE_JS.exists():
        _skip_or_fail_not_built()
    if _node() is None:
        if os.environ.get("COGAME_REQUIRE_WASM_BUILD"):
            pytest.fail("node required for the headless smoke (COGAME_REQUIRE_WASM_BUILD is set)")
        pytest.skip("node not installed")
    args = [_node(), str(SMOKE)] + [str(p) for p in _fixture_paths()]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "smoke: OK" in proc.stdout
    assert "canary:" in proc.stdout
