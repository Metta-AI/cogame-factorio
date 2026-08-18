#!/usr/bin/env node
// Headless smoke for the replay viewer wasm (no browser, no pixels).
//
//   node viewer/smoke.cjs [replay.json ...]
//
// Loads build/viewer_core.{js,wasm} (viewer_main.c built with
// -DVIEWER_HEADLESS by viewer/build_viewer.sh), packs every step of every
// seat of each replay with viewer/replay_pack.js exactly as index.html
// does, pushes it through viewer_set_terrain / viewer_set_step, and
// exercises fit / pick / camera exports. Then runs an "address-space
// canary": a synthetic 200-step x 4-seat replay with 3000 entities per
// step generated on the fly (never materialised as one document) to
// prove repeated large allocations neither leak nor abort under
// -sABORTING_MALLOC. A 120 s watchdog fails the run; onAbort prints the
// C stage note so an OOM names the phase it died in.
"use strict";
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..");
const CORE_JS = path.join(REPO, "build", "viewer_core.js");
const RP = require(path.join(__dirname, "replay_pack.js"));

const WATCHDOG_MS = 120000;
const watchdog = setTimeout(() => {
  console.error(`smoke: watchdog fired after ${WATCHDOG_MS} ms`);
  process.exit(3);
}, WATCHDOG_MS);

function log(...a) { console.log("smoke:", ...a); }

async function main() {
  const fixtures = process.argv.slice(2);
  if (!fixtures.length) fixtures.push(path.join(REPO, "tests", "fixtures", "synthetic_replay.json"));
  if (!fs.existsSync(CORE_JS)) {
    console.error(`smoke: ${CORE_JS} missing - run viewer/build_viewer.sh`);
    process.exit(2);
  }
  const create = require(CORE_JS);
  let mod = null;
  const stageNote = () => {
    if (!mod) return "(module not ready)";
    try {
      const p = mod.ccall("viewer_stage_ptr", "number", [], []);
      const n = mod.ccall("viewer_stage_len", "number", [], []);
      return mod.UTF8ToString(p, n);
    } catch (e) { return "(stage unreadable: " + e.message + ")"; }
  };
  mod = await create({
    print: (t) => console.log("wasm:", t),
    printErr: (t) => console.error("wasm:", t),
    onAbort: (what) => {
      console.error(`smoke: wasm ABORT: ${what}; last stage note: ${stageNote()}`);
      process.exit(4);
    },
  });

  const call = (name, ret, types, args) => mod.ccall(name, ret, types, args);
  const withHeap = (ta, fn) => {
    if (ta.length === 0) return fn(0);
    const ptr = mod._malloc(ta.byteLength);
    try {
      mod.HEAPU8.set(new Uint8Array(ta.buffer, ta.byteOffset, ta.byteLength), ptr);
      return fn(ptr);
    } finally { mod._free(ptr); }
  };
  const pushTerrain = (map) => {
    const t = RP.packTerrain(map);
    withHeap(t.resources, (rp) => withHeap(t.water, (wp) => withHeap(t.trees, (tp) => {
      call("viewer_set_terrain", null, Array(10).fill("number"),
        [t.bounds[0], t.bounds[1], t.bounds[2], t.bounds[3],
         rp, t.resources.length / 4, wp, t.water.length / 3, tp, t.trees.length / 2]);
    })));
    return t;
  };
  const pushStep = (step) => {
    const p = RP.packStep(step);
    withHeap(p.entities, (ep) => withHeap(p.belts, (bp) => withHeap(p.pipes, (pp) => {
      call("viewer_set_step", null, Array(9).fill("number"),
        [ep, p.entities.length / 7, bp, p.belts.length / 3, pp, p.pipes.length / 2,
         p.character ? 1 : 0, p.character ? p.character[0] : 0, p.character ? p.character[1] : 0]);
    })));
    return p;
  };

  withHeap(RP.packPalette(RP.ENTITY_KINDS), (p) => call("viewer_set_entity_palette", null, ["number", "number"], [p, RP.ENTITY_KINDS.length]));
  withHeap(RP.packPalette(RP.RESOURCE_KINDS), (p) => call("viewer_set_resource_palette", null, ["number", "number"], [p, RP.RESOURCE_KINDS.length]));
  call("viewer_resize", null, ["number", "number"], [1280, 800]);

  // ---- real fixtures ------------------------------------------------------
  for (const f of fixtures) {
    const doc = RP.parseReplay(fs.readFileSync(f, "utf8"));
    const t = pushTerrain(doc.map);
    call("viewer_fit_terrain", null, [], []);
    let steps = 0, ents = 0, picks = 0;
    for (const seat of doc.seats) {
      for (const step of seat.steps) {
        const p = pushStep(step);
        call("viewer_fit", null, [], []);
        const scale = call("viewer_camera_scale", "number", [], []);
        if (!(scale > 0)) throw new Error(`bad camera scale ${scale}`);
        // pick every entity at its own centre: must return a row whose
        // centre coincides (later rows may overlap earlier ones)
        const cx = call("viewer_camera_x", "number", [], []);
        const cy = call("viewer_camera_y", "number", [], []);
        for (let i = 0; i < p.labels.length; i++) {
          const l = p.labels[i];
          const px = (l.x - cx) * scale + 640, py = (l.y - cy) * scale + 400;
          const row = call("viewer_pick", "number", ["number", "number"], [px, py]);
          if (row < 0) throw new Error(`${path.basename(f)} seat ${seat.slot} step ${step.step}: pick missed entity ${i} (${l.name} @ ${l.x},${l.y})`);
          const hit = p.labels[row];
          const e = step.entities[row];
          const hw = Math.max(1, e[5]) / 2, hh = Math.max(1, e[6]) / 2;
          if (!(l.x >= hit.x - hw - 1e-6 && l.x <= hit.x + hw + 1e-6 && l.y >= hit.y - hh - 1e-6 && l.y <= hit.y + hh + 1e-6)) {
            throw new Error(`pick returned row ${row} (${hit.name}) not covering ${l.name}`);
          }
          picks++;
        }
        // and a miss far outside the terrain
        const miss = call("viewer_pick", "number", ["number", "number"], [-1e6, -1e6]);
        if (miss !== -1) throw new Error(`pick far outside returned ${miss}`);
        steps++; ents += p.labels.length;
      }
    }
    log(`${path.basename(f)}: ${doc.seats.length} seats, ${steps} steps, ${ents} entity rows, ${picks} picks ok, ` +
        `${t.resources.length / 4} resource tiles, ${t.water.length / 3} water runs, ${t.trees.length / 2} trees`);
  }

  // ---- address-space canary ------------------------------------------------
  const SEATS = 4, STEPS = 200, ENTS = 3000, BELTS = 1500, PIPES = 500;
  const names = Object.keys(RP.ENTITY_KIND_BY_NAME);
  const statuses = ["working", "no_fuel", "full_output", "normal", ""];
  const bigMap = { bounds: { x0: -256, y0: -256, x1: 256, y1: 256 }, resources: [], water: [], trees: [] };
  for (let i = 0; i < 20000; i++) bigMap.resources.push(["iron-ore", (i % 200) - 100, Math.floor(i / 200) - 50, 100 + i]);
  for (let i = 0; i < 2000; i++) bigMap.water.push([-200 + (i % 40), -200 + Math.floor(i / 40), 5]);
  for (let i = 0; i < 5000; i++) bigMap.trees.push([(i * 7) % 500 - 250, (i * 13) % 500 - 250]);
  pushTerrain(bigMap);
  const heapBefore = mod.HEAPU8.length;
  let t0 = Date.now();
  for (let s = 0; s < SEATS; s++) {
    for (let k = 0; k < STEPS; k++) {
      const step = { step: k, entities: [], belts: [], pipes: [], character: { x: k, y: s } };
      for (let i = 0; i < ENTS; i++) {
        const nm = names[(i + k) % names.length];
        step.entities.push([nm, (i % 100) * 3 + 0.5, Math.floor(i / 100) * 3 + 0.5, (i % 4) * 2, statuses[i % statuses.length], 1 + (i % 3), 1 + ((i + 1) % 3)]);
      }
      for (let i = 0; i < BELTS; i++) step.belts.push([i % 50 + 0.5, 200 + Math.floor(i / 50) + 0.5, (i % 4) * 2]);
      for (let i = 0; i < PIPES; i++) step.pipes.push([-100 + (i % 25) + 0.5, 100 + Math.floor(i / 25) + 0.5]);
      pushStep(step);
      if (k % 50 === 0) {
        call("viewer_fit", null, [], []);
        call("viewer_pick", "number", ["number", "number"], [640, 400]);
      }
    }
  }
  const heapAfter = mod.HEAPU8.length;
  // the last pushed step is live: entity 0 sits at (0.5, 0.5)
  call("viewer_set_camera", null, ["number", "number", "number"], [0.5, 0.5, 8]);
  const row0 = call("viewer_pick", "number", ["number", "number"], [640, 400]);
  if (row0 < 0) throw new Error("canary: pick at entity 0 centre missed after 800 pushes");
  log(`canary: ${SEATS}x${STEPS} steps x ${ENTS} entities (+${BELTS} belts, ${PIPES} pipes) pushed in ${Date.now() - t0} ms; ` +
      `heap ${heapBefore >> 20} MB -> ${heapAfter >> 20} MB; stage="${stageNote()}"`);
  if (heapAfter > 512 * 1024 * 1024) throw new Error(`heap grew to ${heapAfter} bytes: leak?`);

  clearTimeout(watchdog);
  log("OK");
}

main().catch((e) => {
  console.error("smoke: FAILED:", e && e.stack || e);
  process.exit(1);
});
