#!/usr/bin/env node
// Smoke-tests the STATIC WASM replay viewer bundle — the artifact the
// observatory actually serves — by loading a replay and rendering frames
// inside the wasm32 runtime, exactly as the page's Worker does.
//
// Why (from coworld-ctf, whose structure this viewer copies): the shipped
// module is --cpu:wasm32 — `int` is 32 bits and the address space ends at
// 2 GB. Overflow traps and allocation failures there are invisible to a
// native 64-bit run, so CI loads the exact emitted module.
//
// Usage: node tools/wasm_replay_smoke.cjs <dist-dir> <replay-file|canary> [frames]
//   Renders [frames] frames (default 60), stepping the replay forward with
//   the same `s:<step>` command the page sends, switching seat once, and
//   checks every packet parses as a sprite-protocol stream. `canary`
//   generates an address-space canary replay on the fly (2 seats x 120
//   steps x 2000 entities + belts + pipes on a 512x512 map, ~30 MB JSON)
//   to prove the wasm32 runtime neither aborts nor leaks across a long
//   scrub; the heap after must stay under 1 GB.

'use strict';
const fs = require('fs');
const path = require('path');

const distDir = path.resolve(process.argv[2] || 'viewer/dist');
const replayPath = process.argv[3];
const frameBudget = parseInt(process.argv[4] || '60', 10);
if (!replayPath) {
  console.error('usage: wasm_replay_smoke.cjs <dist-dir> <replay-file> [frames]');
  process.exit(2);
}

// A hung load (e.g. an allocation loop) must fail loudly, not stall the job.
const watchdog = setTimeout(() => {
  console.error('FAIL: smoke did not finish within 120s');
  process.exit(1);
}, 120000);

const Module = {
  locateFile: (p) => path.join(distDir, p),
  onRuntimeInitialized: run,
  onAbort: (what) => {
    // Allocation failure aborts (-s ABORTING_MALLOC=1) but leaves linear
    // memory intact: the stage buffer still says what exhausted it.
    const stage = readStageNote();
    console.error('FAIL: wasm runtime aborted: ' + what +
      (stage ? '\nruntime was: ' + stage : ''));
    process.exit(1);
  },
};

function readStageNote() {
  try {
    const length = Module._factorio_stage_len ? Module._factorio_stage_len() : 0;
    if (!length) return '';
    const pointer = Module._factorio_stage_ptr();
    return Buffer.from(Module.HEAPU8.subarray(pointer, pointer + length)).toString('utf8');
  } catch (ignored) {
    return '';
  }
}

function readRuntimeError() {
  const length = Module._factorio_error_len();
  if (length) {
    const pointer = Module._factorio_error_ptr();
    return Buffer.from(Module.HEAPU8.subarray(pointer, pointer + length)).toString('utf8');
  }
  const stage = readStageNote();
  return stage
    ? '(no error text; runtime was: ' + stage + ')'
    : '(runtime reported no error text)';
}

function sendText(text) {
  const bytes = Buffer.from(text, 'ascii');
  const packet = Buffer.alloc(bytes.length + 3);
  packet[0] = 0x81;
  packet.writeUInt16LE(bytes.length, 1);
  bytes.copy(packet, 3);
  const pointer = Module._malloc(packet.length);
  Module.HEAPU8.set(packet, pointer);
  Module._factorio_input(pointer, packet.length);
  Module._free(pointer);
}

// Walks a packet as sprite-protocol v1 messages; returns counts or throws.
function checkPacket() {
  const length = Module._factorio_packet_len();
  if (length <= 0) throw new Error('empty packet');
  const pointer = Module._factorio_packet_ptr();
  const bytes = Module.HEAPU8.subarray(pointer, pointer + length);
  let offset = 0;
  const counts = { sprites: 0, objects: 0, deletes: 0, layers: 0, viewports: 0, chrome: '' };
  while (offset < bytes.length) {
    const type = bytes[offset++];
    if (type === 0x01) {
      const id = bytes[offset] | (bytes[offset + 1] << 8);
      const compressed = bytes.readUInt32LE
        ? 0 : 0; // placeholder (Buffer API differs from Uint8Array)
      const clen = bytes[offset + 6] | (bytes[offset + 7] << 8) | (bytes[offset + 8] << 16) | (bytes[offset + 9] << 24);
      const labelLen = bytes[offset + 10 + clen] | (bytes[offset + 11 + clen] << 8);
      if (id === 4090) {
        counts.chrome = Buffer.from(bytes.subarray(offset + 12 + clen, offset + 12 + clen + labelLen)).toString('utf8');
      }
      offset += 12 + clen + labelLen;
      counts.sprites++;
    } else if (type === 0x02) { offset += 11; counts.objects++; }
    else if (type === 0x03) { offset += 2; counts.deletes++; }
    else if (type === 0x04) { }
    else if (type === 0x05) { offset += 5; counts.viewports++; }
    else if (type === 0x06) { offset += 3; counts.layers++; }
    else throw new Error('unknown message type 0x' + type.toString(16) + ' at ' + (offset - 1));
    if (offset > bytes.length) throw new Error('truncated message (type 0x' + type.toString(16) + ')');
  }
  return counts;
}

function canaryReplay() {
  const names = ['burner-mining-drill', 'stone-furnace', 'iron-chest', 'burner-inserter',
    'small-electric-pole', 'assembling-machine-1', 'boiler', 'steam-engine', 'offshore-pump',
    'lab', 'electric-mining-drill', 'oil-refinery', 'unknown-kind'];
  const statuses = ['working', 'no_fuel', 'full_output', 'normal', ''];
  const map = { bounds: { x0: -256, y0: -256, x1: 256, y1: 256 }, resources: [], water: [], trees: [], spawn: { x: 0, y: 0 } };
  for (let i = 0; i < 20000; i++) map.resources.push(['iron-ore', (i % 200) - 100, Math.floor(i / 200) - 50, 100 + i * 4]);
  for (let i = 0; i < 2000; i++) map.water.push([-200 + (i % 40), -200 + Math.floor(i / 40), 5]);
  for (let i = 0; i < 5000; i++) map.trees.push([(i * 7) % 500 - 250, (i * 13) % 500 - 250]);
  const seats = [];
  for (let s = 0; s < 2; s++) {
    const steps = [];
    for (let k = 0; k < 120; k++) {
      const st = { step: k, code: 'x', noop: false, output: '', error: false, score: k, throughput: null,
        tick: k * 60, wall_ms: 1, character: { x: k, y: s }, entities: [], belts: [], pipes: [], inventory: {}, flows_output: {} };
      for (let i = 0; i < 2000; i++) {
        st.entities.push([names[(i + k) % names.length], (i % 100) * 3 + 0.5 - 150, Math.floor(i / 100) * 3 + 0.5 - 30,
          (i % 4) * 2, statuses[i % statuses.length], 1 + (i % 3), 1 + ((i + 1) % 3)]);
      }
      for (let i = 0; i < 1500; i++) st.belts.push([i % 50 + 0.5, 100 + Math.floor(i / 50) + 0.5, (i % 4) * 2]);
      for (let i = 0; i < 500; i++) st.pipes.push([-100 + (i % 25) + 0.5, 100 + Math.floor(i / 25) + 0.5]);
      steps.push(st);
    }
    seats.push({ slot: s, name: 'canary-' + s, final_score: 1, dead: false, steps });
  }
  return Buffer.from(JSON.stringify({ format: 'cogame-factorio-replay', version: 1, game_version: '1',
    config: {}, names: ['canary-0', 'canary-1'], task: { key: 'open_play', goal_description: '' }, map, seats, result: {} }));
}

function run() {
  const bytes = replayPath === 'canary' ? canaryReplay() : fs.readFileSync(replayPath);
  const pointer = Module._malloc(bytes.length);
  Module.HEAPU8.set(bytes, pointer);
  const loaded = Module._factorio_load_replay(pointer, bytes.length);
  Module._free(pointer);
  if (loaded !== 1) {
    console.error('FAIL: factorio_load_replay rejected ' + path.basename(replayPath) +
      '\n' + readRuntimeError());
    process.exit(1);
  }
  let first;
  try { first = checkPacket(); } catch (e) {
    console.error('FAIL: first packet malformed: ' + e.message);
    process.exit(1);
  }
  if (first.layers < 1 || first.viewports < 1 || first.objects < 1) {
    console.error('FAIL: first frame lacks layer/viewport/objects: ' + JSON.stringify(first));
    process.exit(1);
  }
  let chrome;
  try { chrome = JSON.parse(first.chrome); } catch (e) {
    console.error('FAIL: chrome JSON unparsable: ' + first.chrome);
    process.exit(1);
  }
  if (chrome.kind !== 'factorio' || !chrome.board || !(chrome.board.w > 0)) {
    console.error('FAIL: chrome JSON missing board: ' + first.chrome);
    process.exit(1);
  }
  let packetBytes = 0;
  const steps = Math.max(1, chrome.steps || 1);
  for (let i = 0; i < frameBudget; i++) {
    sendText('s:' + (i % (steps + 1)));    // walks past the end once: must clamp
    if (i === Math.floor(frameBudget / 2)) sendText('v:1');  // seat switch (no-op on 1 seat)
    if (Module._factorio_frame() !== 1) {
      console.error('FAIL: factorio_frame died at frame ' + i + '\n' + readRuntimeError());
      process.exit(1);
    }
    try { checkPacket(); } catch (e) {
      console.error('FAIL: frame ' + i + ' packet malformed: ' + e.message);
      process.exit(1);
    }
    packetBytes += Module._factorio_packet_len();
  }
  if (replayPath === 'canary' && Module.HEAPU8.length > 1024 * 1024 * 1024) {
    console.error('FAIL: canary heap grew to ' + Module.HEAPU8.length + ' bytes');
    process.exit(1);
  }
  clearTimeout(watchdog);
  console.log('ok: loaded ' + path.basename(replayPath) + ' (board ' + chrome.board.w + 'x' +
    chrome.board.h + ' @ ' + chrome.board.tile + ' px/tile, ' + steps + ' steps), rendered ' +
    frameBudget + ' frames (' + packetBytes + ' packet bytes, heap ' +
    Math.round(Module.HEAPU8.length / 1024 / 1024) + ' MB)');
  process.exit(0);
}

// The bundle is injected with `Module` as a function parameter — a plain
// require() cannot configure it: the emitted `var Module` declaration
// hoists over any global we set.
const bundlePath = path.join(distDir, 'factorio_replay.js');
new Function('Module', 'require', '__filename', '__dirname',
  fs.readFileSync(bundlePath, 'utf8'))(Module, require, bundlePath, distDir);
