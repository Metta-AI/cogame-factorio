// cogame-factorio replay viewer: JS-side replay parsing + packing.
//
// Loaded by viewer/index.html as a classic <script> (exposes
// window.ReplayPack) and by tests/viewer_pack_harness.js under node
// (module.exports). No DOM, no wasm: pure functions over the replay
// document (docs/REPLAY.md) producing the compact typed arrays that
// index.html pushes into the wasm map renderer (viewer/viewer_main.c).
//
// ---------------------------------------------------------------------
// Typed-array contract JS -> C (all row-major, one row per item)
// ---------------------------------------------------------------------
//   terrain (pushed once, viewer_set_terrain):
//     resources: Int32Array  [kind_id, tile_x, tile_y, amount_bucket] * n
//                kind_id from RESOURCE_KINDS (see below), amount_bucket
//                0..7 (log2 scale, see amountBucket)
//     water:     Int32Array  [tile_x, tile_y, run_length_east] * n
//     trees:     Int32Array  [tile_x, tile_y] * n
//     bounds:    x0, y0, x1, y1 (half-open tile bounds)
//   step (pushed on seat/step change, viewer_set_step):
//     entities:  Float32Array [kind_id, x, y, w, h, dir, status_id] * n
//                kind_id from ENTITY_KINDS, x/y = entity center (FLE
//                convention, +x east, +y south), w/h in tiles, dir 0/2/4/6
//                (N/E/S/W) or -1 unknown, status_id from STATUS_CLASSES
//                (0 none, 1 working, 2 idle, 3 problem)
//     belts:     Float32Array [x, y, dir] * n     (belt tile centers)
//     pipes:     Float32Array [x, y] * n          (pipe tile centers)
//     character: [x, y] or null
//   palettes (pushed once, viewer_set_entity_palette /
//   viewer_set_resource_palette): Uint8Array [r, g, b] * n_kinds, indexed
//   by kind_id. C has no built-in colours: kinds are opaque ints.
//
// The C side never sees names; JS keeps per-row name/status strings
// (packStep(...).labels) for the hover tooltip via viewer_pick().
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.ReplayPack = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const FORMAT = "cogame-factorio-replay";
  const VERSION = 1;

  // Entity kind ids. Order is the id; colours are the JS-owned palette
  // that both the HTML legend and the wasm renderer use.
  const ENTITY_KINDS = [
    { id: 0, key: "other", label: "other", color: [150, 150, 160] },
    { id: 1, key: "drill", label: "mining drill", color: [214, 160, 74] },
    { id: 2, key: "furnace", label: "furnace", color: [222, 96, 62] },
    { id: 3, key: "chest", label: "chest", color: [178, 140, 92] },
    { id: 4, key: "inserter", label: "inserter", color: [246, 208, 74] },
    { id: 5, key: "pole", label: "electric pole", color: [122, 200, 232] },
    { id: 6, key: "assembler", label: "assembling machine", color: [92, 176, 232] },
    { id: 7, key: "boiler", label: "boiler", color: [200, 90, 130] },
    { id: 8, key: "steam_engine", label: "steam engine", color: [160, 110, 200] },
    { id: 9, key: "offshore_pump", label: "offshore pump", color: [70, 170, 210] },
    { id: 10, key: "lab", label: "lab", color: [120, 220, 150] },
    { id: 11, key: "oil", label: "pumpjack / refinery / chem plant", color: [180, 130, 60] },
    { id: 12, key: "belt_special", label: "underground / splitter", color: [230, 200, 60] },
    { id: 13, key: "fluid", label: "pipe-to-ground / pump / tank", color: [90, 150, 180] },
    { id: 14, key: "power", label: "solar / accumulator", color: [60, 120, 200] },
    { id: 15, key: "military", label: "turret / wall / radar", color: [190, 70, 70] },
  ];

  const ENTITY_KIND_BY_NAME = {
    "burner-mining-drill": 1, "electric-mining-drill": 1,
    "stone-furnace": 2, "steel-furnace": 2, "electric-furnace": 2,
    "wooden-chest": 3, "iron-chest": 3, "steel-chest": 3,
    "burner-inserter": 4, "inserter": 4, "fast-inserter": 4,
    "long-handed-inserter": 4, "filter-inserter": 4, "stack-inserter": 4,
    "stack-filter-inserter": 4,
    "small-electric-pole": 5, "medium-electric-pole": 5, "big-electric-pole": 5,
    "substation": 5,
    "assembling-machine-1": 6, "assembling-machine-2": 6, "assembling-machine-3": 6,
    "boiler": 7,
    "steam-engine": 8,
    "offshore-pump": 9,
    "lab": 10,
    "pumpjack": 11, "oil-refinery": 11, "chemical-plant": 11, "centrifuge": 11,
    "underground-belt": 12, "fast-underground-belt": 12,
    "express-underground-belt": 12, "splitter": 12, "fast-splitter": 12,
    "express-splitter": 12,
    "pipe-to-ground": 13, "pump": 13, "storage-tank": 13,
    "solar-panel": 14, "accumulator": 14,
    "gun-turret": 15, "stone-wall": 15, "radar": 15, "gate": 15,
  };

  const RESOURCE_KINDS = [
    { id: 0, key: "iron-ore", label: "iron ore", color: [104, 132, 158] },
    { id: 1, key: "copper-ore", label: "copper ore", color: [214, 122, 62] },
    { id: 2, key: "coal", label: "coal", color: [14, 14, 18] },
    { id: 3, key: "stone", label: "stone", color: [180, 160, 120] },
    { id: 4, key: "crude-oil", label: "crude oil", color: [96, 56, 118] },
    { id: 5, key: "other", label: "other resource", color: [120, 200, 120] },
  ];
  const RESOURCE_KIND_BY_NAME = {
    "iron-ore": 0, "copper-ore": 1, "coal": 2, "stone": 3, "crude-oil": 4,
  };

  // Status classes: the renderer tints outlines by class, not by the ~45
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

  function entityKind(name) {
    const k = ENTITY_KIND_BY_NAME[name];
    return k === undefined ? 0 : k;
  }
  function resourceKind(name) {
    const k = RESOURCE_KIND_BY_NAME[name];
    return k === undefined ? 5 : k;
  }
  function statusClass(status) {
    if (!status) return 0;
    if (status === "working" || status === "normal") return 1;
    if (STATUS_IDLE.has(status)) return 2;
    if (STATUS_PROBLEM.has(status)) return 3;
    return 0;
  }
  // 0..7 on a log2 scale: <64 -> 0, 64 -> 1, 128 -> 2, ... >=4096 -> 7.
  function amountBucket(amount) {
    const a = Number(amount);
    if (!(a > 0)) return 0;
    const b = Math.floor(Math.log2(a)) - 5;
    return Math.max(0, Math.min(7, b));
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

  function packTerrain(map) {
    const res = new Int32Array(map.resources.length * 4);
    map.resources.forEach((r, i) => {
      res[i * 4] = resourceKind(r[0]);
      res[i * 4 + 1] = Math.floor(r[1]);
      res[i * 4 + 2] = Math.floor(r[2]);
      res[i * 4 + 3] = amountBucket(r[3]);
    });
    const water = new Int32Array(map.water.length * 3);
    map.water.forEach((w, i) => {
      water[i * 3] = Math.floor(w[0]);
      water[i * 3 + 1] = Math.floor(w[1]);
      water[i * 3 + 2] = Math.max(1, Math.floor(w[2]));
    });
    const treesSrc = map.trees || [];
    const trees = new Int32Array(treesSrc.length * 2);
    treesSrc.forEach((t, i) => {
      trees[i * 2] = Math.floor(t[0]);
      trees[i * 2 + 1] = Math.floor(t[1]);
    });
    const b = map.bounds;
    return { resources: res, water, trees, bounds: [b.x0, b.y0, b.x1, b.y1] };
  }

  function packStep(step) {
    const ents = new Float32Array(step.entities.length * 7);
    const labels = new Array(step.entities.length);
    step.entities.forEach((e, i) => {
      const o = i * 7;
      ents[o] = entityKind(e[0]);
      ents[o + 1] = e[1];
      ents[o + 2] = e[2];
      ents[o + 3] = Math.max(1, e[5]);
      ents[o + 4] = Math.max(1, e[6]);
      ents[o + 5] = e[3];
      ents[o + 6] = statusClass(e[4]);
      labels[i] = { name: e[0], status: e[4] || "", x: e[1], y: e[2] };
    });
    const belts = new Float32Array(step.belts.length * 3);
    step.belts.forEach((b, i) => {
      belts[i * 3] = b[0]; belts[i * 3 + 1] = b[1]; belts[i * 3 + 2] = b[2];
    });
    const pipes = new Float32Array(step.pipes.length * 2);
    step.pipes.forEach((p, i) => { pipes[i * 2] = p[0]; pipes[i * 2 + 1] = p[1]; });
    const character = step.character ? [step.character.x, step.character.y] : null;
    return { entities: ents, belts, pipes, character, labels };
  }

  function packPalette(kinds) {
    const out = new Uint8Array(kinds.length * 3);
    kinds.forEach((k, i) => { out.set(k.color, i * 3); });
    return out;
  }

  // Bounding box of everything drawn in a step (entities, belts, pipes,
  // character) as [minx, miny, maxx, maxy], or null when empty. Used for
  // the initial auto-fit (the C side has the same logic; this one lets
  // tests check it without wasm).
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

  return {
    FORMAT, VERSION,
    ENTITY_KINDS, ENTITY_KIND_BY_NAME, RESOURCE_KINDS, RESOURCE_KIND_BY_NAME,
    STATUS_CLASSES,
    entityKind, resourceKind, statusClass, amountBucket,
    validateReplay, parseReplay, packTerrain, packStep, packPalette, stepBounds,
  };
});
