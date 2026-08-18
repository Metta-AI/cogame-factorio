// cogame-factorio replay viewer: replay document parsing + status helpers.
//
// Loaded by client/replay_broadcast.html as a classic <script> (exposes
// window.ReplayDoc) and usable under node (module.exports). No DOM, no
// wasm: pure functions over the replay document (docs/REPLAY.md). The
// board itself is drawn by the Nim/wasm renderer (replay-viewer/) from the
// same bytes; this module is what the page's chrome, hover and end card
// read.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ReplayDoc = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const FORMAT = "cogame-factorio-replay";
  const VERSION = 1;

  // FLE EntityStatus strings.
  const STATUS_CLASSES = [
    { id: 0, key: "none", label: "no status" },
    { id: 1, key: "working", label: "working / normal" },
    { id: 2, key: "idle", label: "idle (full output, waiting)" },
    { id: 3, key: "problem", label: "problem (no fuel / power / input)" },
  ];
  const STATUS_IDLE = new Set([
    "full_output", "full_burnt_result_output", "waiting_for_source_items",
    "waiting_for_space_in_destination", "fully_charged", "charging",
    "discharging", "networks_connected", "empty",
  ]);
  const STATUS_PROBLEM = new Set([
    "no_power", "low_power", "no_fuel", "no_recipe", "no_ingredients",
    "no_input_fluid", "no_research_in_progress", "no_minable_resources",
    "low_input_fluid", "fluid_ingredient_shortage", "item_ingredient_shortage",
    "missing_required_fluid", "missing_science_packs", "not_connected",
    "not_plugged_in_electric_network", "networks_disconnected", "disabled",
    "disabled_by_control_behavior", "disabled_by_script",
    "marked_for_deconstruction", "no_ammo", "low_temperature",
    "out_of_logistic_network", "recharging_after_power_outage",
  ]);

  function statusClass(status) {
    if (!status) return 0;
    if (status === "working" || status === "normal") return 1;
    if (STATUS_IDLE.has(status)) return 2;
    if (STATUS_PROBLEM.has(status)) return 3;
    return 0;
  }

  function fail(msg) { throw new Error("replay: " + msg); }
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);

  // Structural validation of the parts the viewer touches. Throws with a
  // path-ish message; returns the doc for chaining.
  function validateReplay(doc) {
    if (!doc || typeof doc !== "object") fail("not an object");
    if (doc.format !== FORMAT) fail(`format is ${JSON.stringify(doc.format)}, expected ${FORMAT}`);
    if (doc.version !== VERSION) fail(`version is ${doc.version}, expected ${VERSION}`);
    if (!Array.isArray(doc.names)) fail("names missing");
    if (!doc.map || typeof doc.map !== "object") fail("map missing");
    const b = doc.map.bounds;
    if (!b || !["x0", "y0", "x1", "y1"].every((k) => isNum(b[k]))) fail("map.bounds malformed");
    for (const key of ["resources", "water"]) {
      if (!Array.isArray(doc.map[key])) fail(`map.${key} missing`);
    }
    doc.map.resources.forEach((r, i) => {
      if (!Array.isArray(r) || r.length < 4 || typeof r[0] !== "string" ||
          !isNum(r[1]) || !isNum(r[2]) || !isNum(r[3])) fail(`map.resources[${i}] malformed`);
    });
    doc.map.water.forEach((w, i) => {
      if (!Array.isArray(w) || w.length < 3 || !w.slice(0, 3).every(isNum)) fail(`map.water[${i}] malformed`);
    });
    if (doc.map.trees !== undefined) {
      if (!Array.isArray(doc.map.trees)) fail("map.trees not an array");
      doc.map.trees.forEach((t, i) => {
        if (!Array.isArray(t) || t.length < 2 || !isNum(t[0]) || !isNum(t[1])) fail(`map.trees[${i}] malformed`);
      });
    }
    if (!Array.isArray(doc.seats)) fail("seats missing");
    doc.seats.forEach((seat, si) => {
      if (!seat || typeof seat !== "object") fail(`seats[${si}] malformed`);
      if (!Array.isArray(seat.steps)) fail(`seats[${si}].steps missing`);
      seat.steps.forEach((st, ki) => {
        const p = `seats[${si}].steps[${ki}]`;
        if (!st || typeof st !== "object") fail(`${p} malformed`);
        if (!Number.isInteger(st.step)) fail(`${p}.step not an integer`);
        if (typeof st.code !== "string") fail(`${p}.code not a string`);
        if (!Array.isArray(st.entities)) fail(`${p}.entities missing`);
        st.entities.forEach((e, ei) => {
          if (!Array.isArray(e) || e.length < 7 || typeof e[0] !== "string" ||
              !isNum(e[1]) || !isNum(e[2]) || !isNum(e[3]) ||
              typeof e[4] !== "string" || !isNum(e[5]) || !isNum(e[6])) {
            fail(`${p}.entities[${ei}] malformed`);
          }
        });
        if (!Array.isArray(st.belts)) fail(`${p}.belts missing`);
        st.belts.forEach((bt, bi) => {
          if (!Array.isArray(bt) || bt.length < 3 || !bt.slice(0, 3).every(isNum)) fail(`${p}.belts[${bi}] malformed`);
        });
        if (!Array.isArray(st.pipes)) fail(`${p}.pipes missing`);
        st.pipes.forEach((pp, pi) => {
          if (!Array.isArray(pp) || pp.length < 2 || !isNum(pp[0]) || !isNum(pp[1])) fail(`${p}.pipes[${pi}] malformed`);
        });
        if (st.character !== null && st.character !== undefined &&
            !(isNum(st.character.x) && isNum(st.character.y))) fail(`${p}.character malformed`);
      });
    });
    return doc;
  }

  function parseReplay(text) {
    let doc;
    try { doc = JSON.parse(text); }
    catch (e) { fail("invalid JSON: " + e.message); }
    return validateReplay(doc);
  }

  function stepBounds(step) {
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    const grow = (x, y, w, h) => {
      minx = Math.min(minx, x - w / 2); maxx = Math.max(maxx, x + w / 2);
      miny = Math.min(miny, y - h / 2); maxy = Math.max(maxy, y + h / 2);
    };
    for (const e of step.entities) grow(e[1], e[2], Math.max(1, e[5]), Math.max(1, e[6]));
    for (const b of step.belts) grow(b[0], b[1], 1, 1);
    for (const p of step.pipes) grow(p[0], p[1], 1, 1);
    if (step.character) grow(step.character.x, step.character.y, 1, 1);
    return minx === Infinity ? null : [minx, miny, maxx, maxy];
  }

  return { FORMAT, VERSION, STATUS_CLASSES, statusClass, validateReplay, parseReplay, stepBounds };
});
