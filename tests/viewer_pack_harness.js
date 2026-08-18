#!/usr/bin/env node
// Node checks for viewer/replay_pack.js (the JS half of the viewer's
// JS<->wasm packing contract). Run by tests/test_viewer.py:
//   node tests/viewer_pack_harness.js tests/fixtures/synthetic_replay.json
// Exits non-zero with a message on the first failed assertion.
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const RP = require(path.resolve(__dirname, "..", "viewer", "replay_pack.js"));

const fixture = process.argv[2] || path.resolve(__dirname, "fixtures", "synthetic_replay.json");
const doc = RP.parseReplay(fs.readFileSync(fixture, "utf8"));

// -- kind / status / bucket tables ------------------------------------------
assert.strictEqual(RP.entityKind("burner-mining-drill"), 1);
assert.strictEqual(RP.entityKind("stone-furnace"), 2);
assert.strictEqual(RP.entityKind("iron-chest"), 3);
assert.strictEqual(RP.entityKind("burner-inserter"), 4);
assert.strictEqual(RP.entityKind("small-electric-pole"), 5);
assert.strictEqual(RP.entityKind("assembling-machine-1"), 6);
assert.strictEqual(RP.entityKind("boiler"), 7);
assert.strictEqual(RP.entityKind("steam-engine"), 8);
assert.strictEqual(RP.entityKind("offshore-pump"), 9);
assert.strictEqual(RP.entityKind("lab"), 10);
assert.strictEqual(RP.entityKind("something-unknown"), 0, "unknown entity -> kind 0 (other)");
RP.ENTITY_KINDS.forEach((k, i) => assert.strictEqual(k.id, i, "ENTITY_KINDS ids are dense and ordered"));
RP.RESOURCE_KINDS.forEach((k, i) => assert.strictEqual(k.id, i, "RESOURCE_KINDS ids are dense and ordered"));
assert.strictEqual(RP.resourceKind("iron-ore"), 0);
assert.strictEqual(RP.resourceKind("copper-ore"), 1);
assert.strictEqual(RP.resourceKind("coal"), 2);
assert.strictEqual(RP.resourceKind("stone"), 3);
assert.strictEqual(RP.resourceKind("crude-oil"), 4);
assert.strictEqual(RP.resourceKind("uranium-ore"), 5, "unknown resource -> other");
assert.strictEqual(RP.statusClass(""), 0);
assert.strictEqual(RP.statusClass("working"), 1);
assert.strictEqual(RP.statusClass("normal"), 1);
assert.strictEqual(RP.statusClass("full_output"), 2);
assert.strictEqual(RP.statusClass("no_fuel"), 3);
assert.strictEqual(RP.statusClass("no_power"), 3);
assert.strictEqual(RP.statusClass("launching_rocket"), 0, "unclassified status -> none");
assert.strictEqual(RP.amountBucket(0), 0);
assert.strictEqual(RP.amountBucket(10), 0);
assert.strictEqual(RP.amountBucket(64), 1);
assert.strictEqual(RP.amountBucket(1000), 4);
assert.strictEqual(RP.amountBucket(1e9), 7);

// -- terrain packing ------------------------------------------------------------
const t = RP.packTerrain(doc.map);
assert.ok(t.resources instanceof Int32Array && t.resources.length === doc.map.resources.length * 4);
assert.ok(t.water instanceof Int32Array && t.water.length === doc.map.water.length * 3);
assert.ok(t.trees instanceof Int32Array && t.trees.length === (doc.map.trees || []).length * 2);
assert.deepStrictEqual(t.bounds, [doc.map.bounds.x0, doc.map.bounds.y0, doc.map.bounds.x1, doc.map.bounds.y1]);
doc.map.resources.forEach((r, i) => {
  assert.strictEqual(t.resources[i * 4], RP.resourceKind(r[0]));
  assert.strictEqual(t.resources[i * 4 + 1], r[1]);
  assert.strictEqual(t.resources[i * 4 + 2], r[2]);
  assert.strictEqual(t.resources[i * 4 + 3], RP.amountBucket(r[3]));
});
// palettes: 3 bytes per kind, indexed by id
const ep = RP.packPalette(RP.ENTITY_KINDS);
assert.ok(ep instanceof Uint8Array && ep.length === RP.ENTITY_KINDS.length * 3);
assert.deepStrictEqual(Array.from(ep.slice(3, 6)), RP.ENTITY_KINDS[1].color);

// -- step packing -----------------------------------------------------------
let rows = 0;
for (const seat of doc.seats) {
  for (const st of seat.steps) {
    const p = RP.packStep(st);
    assert.ok(p.entities instanceof Float32Array && p.entities.length === st.entities.length * 7);
    assert.ok(p.belts instanceof Float32Array && p.belts.length === st.belts.length * 3);
    assert.ok(p.pipes instanceof Float32Array && p.pipes.length === st.pipes.length * 2);
    assert.strictEqual(p.labels.length, st.entities.length);
    st.entities.forEach((e, i) => {
      const o = i * 7;
      // [kind, x, y, w, h, dir, status]  <-  [name, x, y, direction, status, width, height]
      assert.strictEqual(p.entities[o], RP.entityKind(e[0]));
      assert.ok(Math.abs(p.entities[o + 1] - e[1]) < 1e-4 && Math.abs(p.entities[o + 2] - e[2]) < 1e-4);
      assert.strictEqual(p.entities[o + 3], Math.max(1, e[5]));
      assert.strictEqual(p.entities[o + 4], Math.max(1, e[6]));
      assert.strictEqual(p.entities[o + 5], e[3]);
      assert.strictEqual(p.entities[o + 6], RP.statusClass(e[4]));
      assert.strictEqual(p.labels[i].name, e[0]);
      assert.strictEqual(p.labels[i].status, e[4] || "");
      rows++;
    });
    if (st.character) assert.deepStrictEqual(p.character, [st.character.x, st.character.y]);
    else assert.strictEqual(p.character, null);
    const b = RP.stepBounds(st);
    if (st.entities.length || st.belts.length || st.pipes.length || st.character) {
      assert.ok(b && b[0] <= b[2] && b[1] <= b[3], "stepBounds is a valid box");
    }
  }
}
assert.ok(rows > 0, "fixture has entity rows");

// -- validation rejects malformed docs ---------------------------------------
const bad = (mut, re) => {
  const d = JSON.parse(JSON.stringify(doc)); mut(d);
  assert.throws(() => RP.validateReplay(d), re);
};
bad((d) => { d.format = "nope"; }, /format/);
bad((d) => { d.version = 2; }, /version/);
bad((d) => { delete d.map; }, /map/);
bad((d) => { d.map.resources[0] = ["iron-ore", 1]; }, /resources\[0\]/);
bad((d) => { d.seats[0].steps[0].entities[0] = ["x", 1, 2]; }, /entities\[0\]/);
bad((d) => { d.seats[0].steps[0].belts = null; }, /belts/);
assert.throws(() => RP.parseReplay("{not json"), /invalid JSON/);

console.log(`viewer_pack_harness: OK (${doc.seats.length} seats, ${rows} entity rows, ${doc.map.resources.length} resource tiles)`);
