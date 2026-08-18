'use strict';
// chrome_common.js — the replay chrome shared with coworld-ctf.
//
// Ported from coworld-ctf client/chrome_common.js (origin/main): the UI
// toggle reader (?spoilers=), the name helpers (stripSeatSuffix /
// teamHeadline / setName / esc), the clock formatter, the speed chips, the
// transport render (fill + playhead + tick clock), the scrubber marker layer
// with the [spoilers] gate, and the palette tokens — kept function-for-
// function so the two chromes read the same. Adapted to cogame-factorio's
// data model: the timeline unit is a STEP (per seat) rather than an engine
// tick, and playback is page-local (no websocket / postMessage command
// channel), so `ctx` carries plain callbacks instead of a command char
// sender. bitworld-specific chrome (teams, perks, momentum graph, lull
// spans, flag beats) is deliberately NOT vendored.
//
// Two extensions over the ctf scrubber (which is click-to-seek only):
// pointer DRAG seeks continuously, and hovering the track shows the step
// under the pointer.
//
// window.ChromeCommon(ctx) -> object of shared functions + constants.
// ctx fields (each required):
//  - getState():  the latest transport state {idx, n, playing, speed} or
//                 null before the replay is parsed.
//  - seek(idx):   move the playhead to step index idx (clamped by the page).
//  - setPlaying(bool)
//  - setSpeed(mult): select a playback speed multiplier from SPEEDS.
//  - onSpoilers(bool): OPTIONAL. Called after the [spoilers] toggle flips so
//                 the page can re-render its own spoiler-gated panels.
window.ChromeCommon = function (ctx) {
  var $ = function (id) { return document.getElementById(id); };

  // ---- palette (mirrors the ctf board tints so the chrome matches) ---------
  var RED = '#e0523a', BLUE = '#3f7cc4', AMBER = '#e8a33d', PAPER = '#f2e8d8';
  var GREEN = '#45a85e', YELLOW = '#ddc531', GHOST = '#8a7f72';

  // Playback speed multipliers (ctf: engine-authoritative WIRE.speeds; here a
  // page constant — 1× is the readable default pace, see BASE_STEP_MS in the
  // page).
  var SPEEDS = [0.5, 1, 2, 4, 8];

  // ---- UI toggles (externally configurable) --------------------------------
  // Chrome-level UI toggles read their initial value from the page URL, so
  // whatever triggers a replay can preconfigure the chrome by appending query
  // params. Accepted values: 1/0, true/false, on/off, yes/no; anything else
  // keeps the default. Pages can also flip a toggle at runtime through the
  // returned setters.
  function uiToggle(name, dflt) {
    var raw = null;
    try { raw = new URLSearchParams(location.search).get(name); } catch (e) {}
    if (raw == null) return dflt;
    raw = String(raw).toLowerCase();
    if (raw === '1' || raw === 'true' || raw === 'on' || raw === 'yes') return true;
    if (raw === '0' || raw === 'false' || raw === 'off' || raw === 'no') return false;
    return dflt;
  }

  // [spoilers] — whether story beats AHEAD of the playhead are visible before
  // playback gets there: scrubber error/noop/dead markers, final scores in
  // the scorebug + standings, the verdict. ON (default) preserves the classic
  // broadcast chrome; OFF keeps the timeline clean of anything the viewer
  // hasn't watched yet and reveals it as the playhead advances. URL:
  // ?spoilers=0 starts hidden.
  var spoilers = uiToggle('spoilers', true);
  function getSpoilers() { return spoilers; }
  function setSpoilers(on) {
    spoilers = !!on;
    var b = $('btn-spoilers');
    if (b) b.classList.toggle('on', spoilers);
    var s = ctx.getState();
    if (s) applySpoilers(s);
    if (ctx.onSpoilers) ctx.onSpoilers(spoilers);
  }
  // Optional [spoilers] transport button (pages that carry #btn-spoilers).
  (function () {
    var b = $('btn-spoilers');
    if (!b) return;
    b.classList.toggle('on', spoilers);
    b.addEventListener('click', function () { setSpoilers(!spoilers); });
  })();

  // ---- names ---------------------------------------------------------------
  function stripSeatSuffix(name) {
    // Strip the per-seat " (N)" suffix the hosted runtime appends to the SAME
    // policy's multiple connections ("softmaxwell (2)", "softmaxwell (7)"…) so
    // every seat of one policy collapses to a single shared identity. The join
    // path converts spaces to underscores, so the separator usually reads
    // "_(N)" by the time it is a player address. Distinct local self-play
    // names (Player1, Player2) carry no such suffix, so they stay distinct.
    return String(name || '').replace(/[\s_]*\(\d+\)\s*$/, '');
  }
  function teamHeadline(n) {
    // Format a policy identity for the scorebug headline: drop any host:port
    // suffix, restore the spaces the server encodes as underscores, and let the
    // plate's CSS ellipsis clip anything still too wide (no hard char cut, so a
    // full name reads whole when it fits).
    n = String(n || '');
    var at = n.indexOf('@'); if (at > 0) n = n.slice(0, at);
    n = n.replace(/_/g, ' ').trim();
    return (n || '?').toUpperCase();
  }
  function setName(id, txt) { var el = $(id); if (el && el.textContent !== txt) el.textContent = txt; }
  // HTML-escape for innerHTML-built rows. Escapes [&<>"] — the double-quote
  // matters because callers interpolate names into attribute values too.
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  // ---- clock ---------------------------------------------------------------
  function fmt(sec) {
    sec = Math.max(0, Math.round(sec));
    var m = Math.floor(sec / 60), r = sec % 60;
    return m + ':' + (r < 10 ? '0' : '') + r;
  }

  // ---- speed chips ---------------------------------------------------------
  // Built here (the page carries a #speedchips host); clicks go to ctx.setSpeed.
  var speedChipEls = {};
  (function () {
    var host = $('speedchips');
    if (!host) return;
    SPEEDS.forEach(function (v) {
      var b = document.createElement('button');
      b.className = 'chip';
      b.textContent = v + '×';
      b.setAttribute('aria-label', v + 'x speed');
      b.addEventListener('click', function () { ctx.setSpeed(v); });
      host.appendChild(b);
      speedChipEls[v] = b;
    });
  })();

  // ---- transport -----------------------------------------------------------
  // s = {idx, n, playing, speed}. The x-axis spans the seat's steps
  // [0, n-1]; step 0 on-screen is the seat's first recorded step.
  function frac(s, idx) {
    var span = Math.max(1, (s.n || 1) - 1);
    return Math.min(1, Math.max(0, idx / span));
  }
  function renderTransport(s) {
    var t = $('transport');
    if (t) t.classList.toggle('disabled', !s.n);
    var play = $('btn-play');
    if (play) play.textContent = s.playing ? '❘❘' : '▶';
    SPEEDS.forEach(function (sp) { var b = speedChipEls[sp]; if (b) b.classList.toggle('on', sp === s.speed); });
    var f = s.n ? frac(s, s.idx) : 0;
    $('scrub-fill').style.width = (f * 100) + '%';
    $('scrub-head').style.left = (f * 100) + '%';
    $('tick-clock').textContent = (s.n ? (s.idx + 1) : 0) + ' / ' + (s.n || 0);
    placeMarkers(s);
    applySpoilers(s);
  }

  // ---- [spoilers] gate ------------------------------------------------------
  // Runs every transport render (same synchronous pass, so no flash of
  // spoiled chrome before the gate lands). With spoilers OFF, markers ahead of
  // the playhead are held back and revealed as playback advances.
  function applySpoilers(s) {
    for (var i = 0; i < markerEls.length; i++) {
      var el = markerEls[i];
      var hide = !spoilers && el.__idx > s.idx;
      if (el.__hidden !== hide) {
        el.__hidden = hide;
        el.style.display = hide ? 'none' : '';
      }
    }
    var cap = $('scrub-win');
    if (cap) {
      var show = !!verdictCls && (spoilers || (s.n && s.idx >= s.n - 1));
      cap.className = 'scrub-win ' + (show ? 'show ' : '') + (verdictCls || '');
    }
  }

  // ---- scrubber markers ----------------------------------------------------
  // The page hands over the whole marker set for the selected seat
  // ({idx, kind, title}); kinds map to CSS classes (.beat-marker.error /
  // .noop / .dead). Placed lazily on the next transport render so the axis
  // (n) is known.
  var scrubEl = $('scrub');
  var markers = [];          // authoritative set for the current seat
  var markerEls = [];        // placed marker elements (spoiler gate re-checks these)
  var markersDirty = false;
  function setMarkers(list) {
    markers = list || [];
    markersDirty = true;
    var s = ctx.getState();
    if (s) { placeMarkers(s); applySpoilers(s); }
  }
  function placeMarkers(s) {
    if (!markersDirty || !scrubEl) return;
    markersDirty = false;
    for (var i = 0; i < markerEls.length; i++) markerEls[i].remove();
    markerEls = [];
    markers.forEach(function (m) {
      var el = document.createElement('div');
      el.className = 'beat-marker ' + m.kind;
      el.style.left = (frac(s, m.idx) * 100) + '%';
      if (m.title) el.title = m.title;
      el.__idx = m.idx;
      markerEls.push(el);
      scrubEl.appendChild(el);
    });
  }

  // Winner cap past the track's right end: `cls` colors it (a seat class the
  // page defines, or 'draw'); spoiler-gated until the playhead reaches the
  // last step.
  var verdictCls = null;
  function setVerdict(cls, label) {
    verdictCls = cls || null;
    var cap = $('scrub-win');
    if (cap && label) cap.title = label;
    var s = ctx.getState();
    if (s) applySpoilers(s);
  }

  // ---- scrubber pointer: click / drag to seek, hover step readout ----------
  function scrubIdx(ev) {
    var s = ctx.getState();
    if (!s || !s.n) return -1;
    var r = scrubEl.getBoundingClientRect();
    var f = Math.min(1, Math.max(0, (ev.clientX - r.left) / Math.max(1, r.width)));
    return Math.round(f * (s.n - 1));
  }
  (function () {
    if (!scrubEl) return;
    var hover = $('scrub-hover');
    var held = false;
    function showHover(ev) {
      if (!hover) return;
      var i = scrubIdx(ev);
      if (i < 0) { hover.style.display = 'none'; return; }
      var s = ctx.getState();
      hover.textContent = 'step ' + (i + 1) + ' / ' + s.n;
      hover.style.left = (frac(s, i) * 100) + '%';
      hover.style.display = 'block';
    }
    scrubEl.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      var i = scrubIdx(ev); if (i < 0) return;
      held = true;
      try { scrubEl.setPointerCapture(ev.pointerId); } catch (e) {}
      ctx.setPlaying(false);
      ctx.seek(i);
      ev.preventDefault();
    });
    scrubEl.addEventListener('pointermove', function (ev) {
      showHover(ev);
      if (!held) return;
      var i = scrubIdx(ev); if (i >= 0) ctx.seek(i);
    });
    function release(ev) {
      if (!held) return;
      held = false;
      try { scrubEl.releasePointerCapture(ev.pointerId); } catch (e) {}
    }
    scrubEl.addEventListener('pointerup', release);
    scrubEl.addEventListener('pointercancel', release);
    scrubEl.addEventListener('pointerleave', function () { if (hover) hover.style.display = 'none'; });
  })();

  return {
    // constants + helpers the page aliases locally
    $: $,
    RED: RED, BLUE: BLUE, AMBER: AMBER, PAPER: PAPER, GREEN: GREEN, YELLOW: YELLOW, GHOST: GHOST,
    SPEEDS: SPEEDS,
    // shared chrome
    stripSeatSuffix: stripSeatSuffix, teamHeadline: teamHeadline, setName: setName,
    esc: esc, fmt: fmt,
    renderTransport: renderTransport, setMarkers: setMarkers, setVerdict: setVerdict,
    scrubIdx: scrubIdx,
    // UI toggles ([spoilers] + the generic URL-param reader for future ones)
    uiToggle: uiToggle, getSpoilers: getSpoilers, setSpoilers: setSpoilers
  };
};
