'use strict';

// static_replay_worker.js — the Dedicated Worker that owns the wasm replay
// runtime (factorio_replay.js, Nim -> emscripten) and the OffscreenCanvas
// the board is composited on. Copied from coworld-ctf's
// replay-viewer/static_replay_worker.js with the ctf_* exports renamed and
// two cogame-factorio differences: `init` may carry the replay bytes the
// page already fetched for its chrome (`replayBytes`) instead of a URL, and
// there is no sim hash to mismatch-check (the replay is a list of
// snapshots, not a re-simulation).

// broadcast_core.js is shared with the native Window client and its vendored
// Snappy module publishes through `window`. A classic Worker can provide that
// alias without introducing a second implementation or bundle step.
self.window = self;

var Module = {};
var runtimeReady = false;
var initMessage = null;
var runtimeLoaded = false;
var core = null;
var minimapSurface = null;
var failed = false;
var disposed = false;
// Load-time profile (worker start -> first frame). Reported to the page as
// a 'perf' message and logged, so a slow first frame names its stage.
var perfT0 = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
var perfMarks = {};
function perfNow() {
  return ((typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now()) - perfT0;
}
function perfMark(name) {
  perfMarks[name] = Math.round(perfNow());
  console.info('[replay-worker] ' + name + ' @ ' + perfMarks[name] + ' ms');
}

function stageNote() {
  // The fixed progress buffer survives an ABORTING_MALLOC failure even though
  // the Emscripten call stack does not.
  try {
    var length = Module._factorio_stage_len ? Module._factorio_stage_len() : 0;
    if (!length) return '';
    var pointer = Module._factorio_stage_ptr();
    return new TextDecoder().decode(
      Module.HEAPU8.slice(pointer, pointer + length));
  } catch (ignored) {
    return '';
  }
}

function runtimeError() {
  var length = Module._factorio_error_len();
  if (!length) {
    var stage = stageNote();
    return stage
      ? 'Replay runtime failed while: ' + stage
      : 'Replay runtime rejected the replay';
  }
  var pointer = Module._factorio_error_ptr();
  return new TextDecoder().decode(
    Module.HEAPU8.slice(pointer, pointer + length));
}

function runtimeProfile() {
  try {
    var length = Module._factorio_profile_len ? Module._factorio_profile_len() : 0;
    if (!length) return '';
    var pointer = Module._factorio_profile_ptr();
    return new TextDecoder().decode(Module.HEAPU8.slice(pointer, pointer + length));
  } catch (ignored) {
    return '';
  }
}

function reportFailure(error) {
  if (failed || disposed) return;
  failed = true;
  postMessage({
    type: 'error',
    message: error && error.message ? error.message : String(error),
    stage: stageNote()
  });
}

function copyIntoRuntime(bytes, callback) {
  var pointer = Module._malloc(bytes.length);
  try {
    Module.HEAPU8.set(bytes, pointer);
    return callback(pointer, bytes.length);
  } finally {
    Module._free(pointer);
  }
}

function ingestPacket() {
  var length = Module._factorio_packet_len();
  if (!length) throw new Error('Replay runtime produced an empty frame');
  var pointer = Module._factorio_packet_ptr();
  // BroadcastCore parses synchronously and copies any retained compressed
  // sprite bytes, so it can read the WASM heap view directly. This avoids a
  // full packet allocation/copy on every replay frame.
  core.ingest(Module.HEAPU8.subarray(pointer, pointer + length));
}

function sendRuntimeInput(bytes) {
  if (!runtimeLoaded) return;
  copyIntoRuntime(bytes, function (pointer, length) {
    Module._factorio_input(pointer, length);
  });
}

function createBroadcastCore(message) {
  core = self.BroadcastCore.create({
    canvas: message.canvas,
    websocket: false,
    playoutBuffer: false,
    viewportWidth: message.width,
    viewportHeight: message.height,
    devicePixelRatio: message.dpr,
    onText: function (text) {
      postMessage({ type: 'text', text: text });
    },
    onStatus: function (status) {
      postMessage({ type: 'status', status: status });
    },
    onFirstFrame: function () {
      postMessage({ type: 'firstFrame' });
    },
    onTransform: function (transform) {
      postMessage({ type: 'transform', transform: transform });
    },
    onSendPacket: sendRuntimeInput
  });
  if (minimapSurface) core.attachMinimap(minimapSurface);
  core.start();
}

// The sprite atlas ships inside the preloaded FS as a PNG. Decoding it with
// pixie inside wasm costs ~150 ms native and 1-4 s in the browser (Liftoff
// code, contended laptop); the browser's own image decoder does it in tens of
// ms, so hand the straight-alpha RGBA to the runtime instead. Anything
// missing (no OffscreenCanvas / createImageBitmap, node) falls back to the
// in-wasm decode.
async function handOverAtlas() {
  if (typeof createImageBitmap !== 'function' || typeof OffscreenCanvas !== 'function') {
    console.info('[replay-worker] no createImageBitmap/OffscreenCanvas here');
    return false;
  }
  if (!Module.FS || !Module._factorio_set_atlas) {
    console.info('[replay-worker] runtime exports no FS/set_atlas');
    return false;
  }
  var png;
  try {
    png = Module.FS.readFile('assets/atlas.png');
  } catch (error) {
    console.info('[replay-worker] atlas.png not in the preloaded FS', error);
    return false;
  }
  var bitmap = await createImageBitmap(new Blob([png], { type: 'image/png' }),
    { premultiplyAlpha: 'none', colorSpaceConversion: 'none' });
  var width = bitmap.width;
  var height = bitmap.height;
  var surface = new OffscreenCanvas(width, height);
  var ctx = surface.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(bitmap, 0, 0);
  var image = ctx.getImageData(0, 0, width, height);
  var accepted = copyIntoRuntime(image.data, function (pointer) {
    return Module._factorio_set_atlas(pointer, width, height, 0);
  });
  if (accepted !== 1) {
    bitmap.close();
    console.info('[replay-worker] runtime rejected the decoded atlas (' + accepted + ')');
    return false;
  }
  // The 16 px/tile sheet (large boards): a 2:1 box filter, drawn by the
  // canvas (premultiplied, so alpha-weighted) instead of per pixel in wasm.
  var halfW = width >> 1;
  var halfH = height >> 1;
  var half = new OffscreenCanvas(halfW, halfH);
  var halfCtx = half.getContext('2d', { willReadFrequently: true });
  halfCtx.imageSmoothingEnabled = true;
  halfCtx.imageSmoothingQuality = 'high';
  halfCtx.drawImage(bitmap, 0, 0, halfW * 2, halfH * 2, 0, 0, halfW, halfH);
  bitmap.close();
  var halfImage = halfCtx.getImageData(0, 0, halfW, halfH);
  copyIntoRuntime(halfImage.data, function (pointer) {
    return Module._factorio_set_atlas(pointer, halfW, halfH, 1);
  });
  return true;
}

async function start() {
  if (!runtimeReady || !initMessage || runtimeLoaded || failed || disposed) return;
  var message = initMessage;
  initMessage = null;
  try {
    perfMark('start');
    createBroadcastCore(message);
    var atlasHandedOver = false;
    try {
      atlasHandedOver = await handOverAtlas();
    } catch (error) {
      console.warn('[replay-worker] native atlas decode failed; wasm decodes the PNG', error);
    }
    perfMark(atlasHandedOver ? 'atlas decoded natively' : 'atlas left to wasm');
    var bytes;
    if (message.replayBytes) {
      bytes = new Uint8Array(message.replayBytes);
    } else {
      var response = await fetch(message.replayUrl, {
        credentials: 'omit',
        mode: 'cors'
      });
      if (!response.ok) {
        throw new Error('Replay request returned HTTP ' + response.status);
      }
      bytes = new Uint8Array(await response.arrayBuffer());
    }
    if (!bytes.length) throw new Error('Replay response was empty');
    perfMark('replay bytes ready');
    var loaded = copyIntoRuntime(bytes, function (pointer, length) {
      return Module._factorio_load_replay(pointer, length);
    });
    if (!loaded) throw new Error(runtimeError());
    runtimeLoaded = true;
    perfMark('wasm load_replay done (' + Module._factorio_packet_len() + ' B first packet)');
    console.info('[replay-worker] wasm load profile: ' + runtimeProfile());
    ingestPacket();
    perfMark('first packet ingested');
    postMessage({ type: 'loaded', perf: perfMarks });
  } catch (error) {
    reportFailure(error);
  }
}

function advance(frames) {
  if (!runtimeLoaded || failed || disposed) return;
  try {
    var count = Math.max(1, Math.min(6, Number(frames) || 1));
    for (var i = 0; i < count; i++) {
      if (Module._factorio_frame() < 0) throw new Error(runtimeError());
      ingestPacket();
    }
    postMessage({
      type: 'advanced',
      // Presentation stat for the page (the core draws over here, a thread
      // away): total frames blitted, so the page can read draws-per-second.
      draws: core ? core.getPaceStats().draws : 0
    });
  } catch (error) {
    reportFailure(error);
  }
}

Module.locateFile = function (path) {
  return new URL(path, self.location.href).toString();
};
Module.onAbort = function (what) {
  var stage = stageNote();
  reportFailure(new Error('Replay runtime ran out of memory (' + what +
    ') — wasm32 is limited to 2 GB' +
    (stage ? '. Failed while: ' + stage : '')));
};
Module.onRuntimeInitialized = function () {
  runtimeReady = true;
  perfMark('wasm runtime initialized');
  // Tell the page the wasm has booted (it re-arms its no-data timeout).
  postMessage({ type: 'boot' });
  start();
};
self.Module = Module;

self.onmessage = function (event) {
  var message = event.data || {};
  try {
    if (message.type === 'init') {
      initMessage = message;
      start();
    } else if (message.type === 'advance') {
      advance(message.frames);
    } else if (message.type === 'command' && core) {
      core.sendCommand(message.text || '');
    } else if (message.type === 'click' && core) {
      core.clickMap(Number(message.x) || 0, Number(message.y) || 0);
    } else if (message.type === 'input' && runtimeLoaded) {
      sendRuntimeInput(new Uint8Array(message.bytes));
    } else if (message.type === 'resize' && core) {
      core.setViewportSize(message.width, message.height, message.dpr);
    } else if (message.type === 'view' && core) {
      // The canvas is an OffscreenCanvas here, so wheel/drag land on the main
      // thread's placeholder element and arrive as view commands. The core's
      // transform (and the transform echoed back for click mapping) stays the
      // single source of truth either way.
      if (message.action === 'zoom') core.zoomAt(message.factor, message.x, message.y);
      else if (message.action === 'setZoom') core.setZoom(message.level, message.x, message.y);
      else if (message.action === 'pan') core.panBy(message.dx, message.dy);
      else if (message.action === 'panMap') core.panByMap(message.dx, message.dy);
      else if (message.action === 'panTo') core.panTo(message.x, message.y);
      else if (message.action === 'reset') core.resetView();
    } else if (message.type === 'minimap') {
      // The board pixels live here, so the minimap is drawn here too. The page
      // transferred its canvas across; hold it until the core exists.
      minimapSurface = message.canvas || null;
      if (core && minimapSurface) core.attachMinimap(minimapSurface);
    } else if (message.type === 'dispose') {
      disposed = true;
      if (core) core.stop();
      close();
    }
  } catch (error) {
    reportFailure(error);
  }
};

perfMark('worker script start');
importScripts('./broadcast_core.js', './factorio_replay.js');
perfMark('importScripts done');
