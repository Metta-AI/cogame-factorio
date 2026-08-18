"""Static wasm replay viewer checks (replay-viewer/, client/, viewer/,
tools/build_replay_viewer.sh).

Layers, cheapest first:

- fixture contract: tests/fixtures/synthetic_replay.json and
  tests/fixtures/sample_replay.json conform to docs/REPLAY.md v1 - a small
  structural validator lives here so the viewer's only input has a
  tripwire independent of the JS;
- page contract: client/replay_broadcast.html (shipped as dist/index.html)
  reads the `replay` query parameter, falls back to /replay-data,
  references bundle assets relatively only, surfaces Worker/wasm failures
  (with the stage note) on a fail card, honours ?spoilers= and relays
  Escape to the embedding shell;
- build hook: tools/build_replay_viewer.sh asserts every bundle file;
- chrome contract: the transport / scrubber / scorebug chrome ported from
  coworld-ctf (client/chrome_common.js + the page's DOM ids + shortcuts);
- sprite atlas: viewer/assets/atlas.{png,json} exist, the manifest is
  well-formed and covers the FLE lab entity kinds;
- build outputs + wasm smoke: viewer/dist/{index.html, factorio_replay.js,
  factorio_replay.wasm, factorio_replay.data, static_replay.js,
  static_replay_worker.js, broadcast_core.js, chrome_common.js,
  replay_doc.js} exist after viewer/build_viewer.sh, and
  tools/wasm_replay_smoke.cjs loads the EXACT emitted module under node
  against both fixtures plus an address-space canary generated on the fly
  (120 s watchdog; onAbort prints the stage note). Those skip when the wasm
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
CLIENT_DIR = REPO_ROOT / "client"
PAGE = CLIENT_DIR / "replay_broadcast.html"
ASSETS = VIEWER_DIR / "assets"
FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = FIXTURES / "synthetic_replay.json"
SAMPLE = FIXTURES / "sample_replay.json"
SMOKE = REPO_ROOT / "tools" / "wasm_replay_smoke.cjs"

BUNDLE_FILES = ("index.html", "factorio_replay.js", "factorio_replay.wasm", "factorio_replay.data",
                "static_replay.js", "static_replay_worker.js", "broadcast_core.js",
                "chrome_common.js", "replay_doc.js")
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
# page contract (static inspection of client/replay_broadcast.html)
# --------------------------------------------------------------------------

def _index_html() -> str:
    return PAGE.read_text()


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
    assert "./static_replay.js" in srcs and "./replay_doc.js" in srcs and "./chrome_common.js" in srcs
    # the wasm runtime lives in the Worker, never on the main thread
    assert "./factorio_replay.js" not in srcs and "./broadcast_core.js" not in srcs
    # no absolute or remote asset URLs anywhere (fonts, CDNs, images)
    for m in re.finditer(r'(?:src|href)="([^"]+)"', html):
        url = m.group(1)
        assert not url.startswith(("http://", "https://", "//")), f"remote asset {url!r}"
    assert "@import" not in html and "fonts.googleapis" not in html
    # the board core is the ctf-style Worker core
    assert "FactorioStaticReplay.createCore(" in html


def test_index_surfaces_failures_and_shell_hooks():
    html = _index_html()
    assert 'id="banner"' in html and 'role="alert"' in html
    assert 'id="failcard"' in html and "Board renderer failed" in html, "Worker/wasm failure -> fail card"
    worker = (CLIENT_DIR / "static_replay_worker.js").read_text()
    assert "Module.onAbort" in worker and "_factorio_stage_ptr" in worker, "abort reports the wasm stage note"
    assert "ABORTING_MALLOC" in (REPO_ROOT / "replay-viewer" / "config.nims").read_text()
    assert 'role="status"' in html, "loading plate"
    assert "prefers-reduced-motion" in html
    # ?spoilers= is read by the shared chrome's uiToggle (chrome_common.js)
    chrome = _chrome_common_js()
    assert "uiToggle('spoilers'" in chrome and "getSpoilers" in html
    assert 'src: "ctf-shell", type: "esc"' in html, "Escape relay for Observatory's TheaterOverlay"
    assert 'target = "_top"' in html, "failure card link opens the replay JSON directly"
    assert "game_version" in html
    # two-stage escalation: the Worker's 'boot' re-arms the no-data timer to the
    # stuck budget, and any timeout card is dismissed by the first frame
    assert "postMessage({ type: 'boot' })" in worker
    assert "onBoot: onWorkerBoot" in html and "armNoDataTimer(FAIL_STUCK_MS)" in html
    assert "clearFailCard()" in html and "timeoutCardShown && firstFrameSeen" in html
    # the Worker decodes the atlas natively and hands the pixels to the runtime
    assert "_factorio_set_atlas" in worker and "createImageBitmap" in worker
    assert "_factorio_set_atlas" in (REPO_ROOT / "replay-viewer" / "config.nims").read_text()


def _chrome_common_js() -> str:
    return (CLIENT_DIR / "chrome_common.js").read_text()


def _node() -> str | None:
    return shutil.which("node")


# --------------------------------------------------------------------------
# chrome contract (the coworld-ctf league-replayer chrome, ported)
# --------------------------------------------------------------------------

def test_chrome_common_is_the_ctf_port_and_self_contained():
    js = _chrome_common_js()
    assert "coworld-ctf" in js, "attribution to the ctf source"
    assert "window.ChromeCommon = function" in js
    for name in ("uiToggle", "stripSeatSuffix", "teamHeadline", "setName", "esc",
                 "renderTransport", "setMarkers", "setVerdict", "getSpoilers", "setSpoilers"):
        assert name in js, name
    # ctf's stripSeatSuffix regex, verbatim (the platform's " (N)" seat suffix)
    assert r"replace(/[\s_]*\(\d+\)\s*$/, '')" in js
    # bitworld-specific chrome is NOT vendored
    for absent in ("renderMomentum", "ingestLullSpans", "PERK_ICONS", "handicapInfo", "CTF_WIRE"):
        assert absent not in js, f"{absent} is bitworld-specific"
    # this file is loaded as a plain script: no imports, no external fetches
    assert "import " not in js and "fetch(" not in js and "http://" not in js and "https://" not in js


def test_index_carries_the_ctf_transport_and_scorebug_dom():
    html = _index_html()
    for el_id in ("transport", "btn-play", "btn-restart", "btn-back", "btn-end", "btn-spoilers",
                  "speedchips", "scrub", "scrub-fill", "scrub-head", "scrub-win", "scrub-hover",
                  "tick-clock", "win-chip", "scorebug", "seatchips", "clock-time", "clock-caption",
                  "standings", "endcard", "ec-headline", "ec-teams", "ec-replay"):
        assert f'id="{el_id}"' in html, el_id
    # step markers on the scrubber: error (red) / noop (grey) / dead
    for cls in (".beat-marker.error", ".beat-marker.noop", ".beat-marker.dead"):
        assert cls in html, cls
    # ctf palette tokens verbatim
    for token in ("--paper:#f2e8d8", "--amber:#e8a33d", "--stage-lo:#16110d", "--red:#e0523a",
                  "--pixfont:'rajdhani'"):
        assert token in html, token
    # the embedded scoreboard face is inlined (no external font) and licensed
    assert "data:font/ttf;base64," in html
    assert (VIEWER_DIR / "FONT_LICENSE.txt").exists()
    # every existing panel survives the chrome port
    for el_id in ("code", "output", "inventory", "flows", "result-body", "legend", "loader", "failcard",
                  "fit", "fitmap", "tooltip", "ro-step", "ro-tick", "ro-score", "ro-thr", "ro-ents"):
        assert f'id="{el_id}"' in html, el_id


def test_index_keyboard_shortcuts_mirror_the_ctf_transport():
    html = _index_html()
    for key in ('k === " "', 'k === "ArrowRight"', 'k === "ArrowLeft"', 'k === "Home"', 'k === "End"',
                'k === "["', 'k === "]"', 'k === "f"', 'k === "o"', 'k === "b"', 'k === "e"',
                'k === ","', 'k === "."', 'k === "+"', 'k === "-"', 'k >= "1" && k <= "9"'):
        assert key in html, key
    # a step is the timeline unit: playback pace is per step, speed chips scale it
    assert "BASE_STEP_MS" in html and "C.SPEEDS" in html
    # collapsible side plaques: q / w keys, edge tabs, persisted, ?panes=0
    for needle in ('k === "q"', 'k === "w"', 'id="tab-l"', 'id="tab-r"', 'C.uiToggle("panes", true)',
                   'localStorage.setItem(PANES_KEY', '<b>q</b>/<b>w</b> panes'):
        assert needle in html, needle


@pytest.mark.skipif(_node() is None, reason="node not installed")
def test_chrome_common_parses_under_node():
    for name in ("chrome_common.js", "replay_doc.js", "static_replay.js", "static_replay_worker.js", "broadcast_core.js"):
        proc = subprocess.run([_node(), "--check", str(CLIENT_DIR / name)],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, name + ": " + proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# sprite atlas (Factorio sheets cut by viewer/tools/build_atlas.py)
# --------------------------------------------------------------------------

LAB_KINDS = ("burner-mining-drill", "electric-mining-drill", "stone-furnace", "steel-furnace",
             "electric-furnace", "iron-chest", "wooden-chest", "burner-inserter", "inserter",
             "fast-inserter", "long-handed-inserter", "small-electric-pole", "medium-electric-pole",
             "big-electric-pole", "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
             "boiler", "steam-engine", "offshore-pump", "pipe", "pipe-to-ground", "lab", "pumpjack",
             "oil-refinery", "chemical-plant", "storage-tank", "solar-panel", "accumulator", "gun-turret",
             "stone-wall", "radar", "character", "transport-belt", "underground-belt", "splitter",
             "iron-ore", "copper-ore", "coal", "stone", "crude-oil", "water", "tree")


def test_sprite_atlas_manifest_covers_the_lab_kinds():
    assert (ASSETS / "atlas.png").exists() and (ASSETS / "atlas.json").exists()
    assert (ASSETS / "atlas.png").stat().st_size < 6 * 1024 * 1024, "atlas budget"
    m = json.loads((ASSETS / "atlas.json").read_text())
    assert m["tile_px"] == 32
    names = {e["name"] for e in m["sprites"]}
    for kind in LAB_KINDS:
        assert kind in names, f"atlas lacks {kind}"
    for key, idx in m["by_name"].items():
        e = m["sprites"][idx]
        assert key == f'{e["name"]}|{e["dir"]}'
        assert e["w"] > 0 and e["h"] > 0
    for d in ("north", "east", "south", "west"):
        assert f"burner-mining-drill|{d}" in m["by_name"]
        assert f"transport-belt|{d}" in m["by_name"]
    assert "iron-ore|8-1" in m["by_name"] and "iron-ore|1-8" in m["by_name"]
    assert (ASSETS / "README.md").exists(), "attribution note (Wube-owned art via FLE)"


# --------------------------------------------------------------------------
# build hook + node-level checks
# --------------------------------------------------------------------------

def test_build_replay_viewer_hook_asserts_every_bundle_file():
    hook = (REPO_ROOT / "tools" / "build_replay_viewer.sh").read_text()
    assert "--target wasm-builder" in hook
    assert "/workspace/viewer/dist/." in hook
    for name in BUNDLE_FILES:
        assert name in hook, f"hook does not assert {name}"
    assert os.access(REPO_ROOT / "tools" / "build_replay_viewer.sh", os.X_OK)
    assert os.access(VIEWER_DIR / "build_viewer.sh", os.X_OK)


def _skip_or_fail_not_built():
    """With COGAME_REQUIRE_WASM_BUILD set a missing build artifact is a
    failure, never a silent skip."""
    if os.environ.get("COGAME_REQUIRE_WASM_BUILD"):
        pytest.fail(NOT_BUILT + " (COGAME_REQUIRE_WASM_BUILD is set)")
    pytest.skip(NOT_BUILT)


def test_build_viewer_outputs_exist():
    if not (VIEWER_DIST / "factorio_replay.wasm").exists():
        _skip_or_fail_not_built()
    for name in BUNDLE_FILES:
        assert (VIEWER_DIST / name).exists(), f"viewer/dist/{name} missing"
    # dist/index.html is the source page, byte for byte; client files likewise
    assert (VIEWER_DIST / "index.html").read_bytes() == PAGE.read_bytes()
    for name in ("chrome_common.js", "replay_doc.js", "static_replay.js", "static_replay_worker.js", "broadcast_core.js"):
        assert (VIEWER_DIST / name).read_bytes() == (CLIENT_DIR / name).read_bytes(), name
    js = (VIEWER_DIST / "factorio_replay.js").read_text(errors="replace")
    for export in ("_factorio_load_replay", "_factorio_frame", "_factorio_input", "_factorio_packet_ptr",
                   "_factorio_packet_len", "_factorio_error_ptr", "_factorio_stage_ptr"):
        assert export in js, export
    # the sprite atlas rides the emscripten preload (.data), never a fetch
    assert (VIEWER_DIST / "factorio_replay.data").stat().st_size > 1024 * 1024


def _smoke(target: str, frames: int) -> subprocess.CompletedProcess:
    return subprocess.run([_node(), str(SMOKE), str(VIEWER_DIST), target, str(frames)],
                          capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("fixture", ["synthetic_replay.json", "sample_replay.json"])
def test_wasm_smoke_renders_fixture(fixture: str):
    if not (VIEWER_DIST / "factorio_replay.wasm").exists():
        _skip_or_fail_not_built()
    if _node() is None:
        if os.environ.get("COGAME_REQUIRE_WASM_BUILD"):
            pytest.fail("node required for the wasm smoke (COGAME_REQUIRE_WASM_BUILD is set)")
        pytest.skip("node not installed")
    proc = _smoke(str(FIXTURES / fixture), 60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("ok: loaded " + fixture), proc.stdout


def test_wasm_smoke_address_space_canary():
    if not (VIEWER_DIST / "factorio_replay.wasm").exists():
        _skip_or_fail_not_built()
    if _node() is None:
        pytest.skip("node not installed")
    proc = _smoke("canary", 150)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok: loaded canary" in proc.stdout, proc.stdout
