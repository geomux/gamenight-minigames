/* render.js — chunky lo-fi canvas renderer.
   Two layers: #world is a small 480x270 buffer upscaled with pixelated
   rendering (that's where the Terraria-ish chunk comes from), #overlay is a
   crisp full-res canvas for name tags and big text. */
"use strict";

const Renderer = (() => {
  const W = 480, H = 270;          // world buffer size
  const S = 0.5;                   // server coords (960x540) -> world buffer
  const world = document.getElementById("world");
  const overlay = document.getElementById("overlay");
  const wctx = world.getContext("2d");
  const octx = overlay.getContext("2d");

  let cssW = 960, cssH = 540, dpr = 1;

  // round state
  let arena = null;
  let meta = new Map();            // pid -> {name, color}
  let selfPid = null;              // local player id, for the "you" halo
  let snaps = [];                  // [{t, R, wind, e:Map(pid->[x,y,alive,cd,r])}]
  let snapGap = 66;                // ema of ms between snapshots

  // fx state
  let particles = [];              // chunky squares
  let ghosts = [];                 // dash afterimages
  let fallers = [];                // players tumbling into the void
  let shake = 0;
  let flashMsg = null;             // {text, t0}
  let lastFrame = performance.now();

  // pre-rendered layers
  let bg = null;                   // starfield + nebula
  let floorPat = null;             // dithered platform pattern

  // light-cycles state: trails live on a persistent offscreen canvas so we
  // never redraw thousands of cells per frame
  let cellMap = new Map();         // "x,y" -> pid
  let trailCv = null, trailCtx = null, gridDots = null;
  let cellW = 5, cellH = 5;

  // avalanche-run state
  let obsMap = new Map();          // id -> [x, y, type]  (world coords)
  let skiSnowPat = null, skiSpeck = null;
  let skiBalls = new Map();        // bid -> {x,y,vx,vy,t} dead-reckoning registry

  // aces-high state
  let skyBg = null, cloudFar = null, cloudNear = null;
  let isleCv = null;               // cached island layer for this round
  let planeBullets = new Map();    // bid -> {x,y,vx,vy,t} dead-reckoning registry

  // bumper-ball state
  let bumperBall = new Map();      // single-entry dead-reckoning registry (ball has no id; key 0)
  let bumperTeamOf = new Map();    // pid -> 0 (red) | 1 (blue), from the arena payload
  const BUMPER_R = 13;             // player body radius (fixed; not sent over the wire)
  const TEAM_HUES = ["#ff3b5c", "#2fb8ff"];   // red / blue team-ring accent colors

  /* ------------------------------ helpers ------------------------------ */

  function shade(hex, f) {
    const n = parseInt(hex.slice(1), 16);
    const r = Math.max(0, Math.min(255, ((n >> 16) & 255) * f));
    const g = Math.max(0, Math.min(255, ((n >> 8) & 255) * f));
    const b = Math.max(0, Math.min(255, (n & 255) * f));
    return `rgb(${r | 0},${g | 0},${b | 0})`;
  }

  function mix(hex, hex2, t) {
    const n1 = parseInt(hex.slice(1), 16), n2 = parseInt(hex2.slice(1), 16);
    const r = ((n1 >> 16) & 255) * (1 - t) + ((n2 >> 16) & 255) * t;
    const g = ((n1 >> 8) & 255) * (1 - t) + ((n2 >> 8) & 255) * t;
    const b = (n1 & 255) * (1 - t) + (n2 & 255) * t;
    return `rgb(${r | 0},${g | 0},${b | 0})`;
  }

  /* ------------------------ projectile dead-reckoning ------------------------
     Bullets/snowballs fly in perfect straight lines, so instead of the
     buffered snapshot-to-snapshot interpolation used for players (which
     trades latency for smoothing over *unknown* future input), each is
     tracked by id and extrapolated from its last-known velocity every
     frame: zero added latency, exact motion, no more teleport-on-snapshot. */

  function upsertProjectiles(reg, rows) {
    const seen = new Set();
    const t = performance.now();
    for (const row of rows) {
      const [id, x, y, vx, vy] = row;
      seen.add(id);
      reg.set(id, { x, y, vx, vy, t });
    }
    for (const id of [...reg.keys()]) {
      if (!seen.has(id)) reg.delete(id);
    }
  }

  function liveProjectiles(reg, capMs = 150) {
    const now = performance.now();
    const out = [];
    for (const p of reg.values()) {
      const dt = Math.min(capMs, Math.max(0, now - p.t)) / 1000;
      out.push({ x: p.x + p.vx * dt, y: p.y + p.vy * dt, vx: p.vx, vy: p.vy });
    }
    return out;
  }

  function buildBg() {
    bg = document.createElement("canvas");
    bg.width = W; bg.height = H;
    const c = bg.getContext("2d");
    c.fillStyle = "#0a0a18";
    c.fillRect(0, 0, W, H);
    // faint nebula blobs
    for (const [x, y, r, col] of [[90, 60, 120, "rgba(90,60,160,.10)"],
                                  [390, 200, 140, "rgba(40,140,160,.08)"],
                                  [250, 40, 100, "rgba(160,60,120,.06)"]]) {
      const g = c.createRadialGradient(x, y, 0, x, y, r);
      g.addColorStop(0, col);
      g.addColorStop(1, "transparent");
      c.fillStyle = g;
      c.fillRect(0, 0, W, H);
    }
    // stars
    for (let i = 0; i < 110; i++) {
      const b = Math.random();
      c.fillStyle = `rgba(${200 + b * 55},${200 + b * 55},255,${0.25 + b * 0.5})`;
      c.fillRect((Math.random() * W) | 0, (Math.random() * H) | 0, b > 0.85 ? 2 : 1, b > 0.85 ? 2 : 1);
    }
  }

  function buildFloor() {
    const p = document.createElement("canvas");
    p.width = 8; p.height = 8;
    const c = p.getContext("2d");
    c.fillStyle = "#7a6248";                    // warm dirt base
    c.fillRect(0, 0, 8, 8);
    c.fillStyle = "#64503a";                    // dark speckle
    for (const [x, y] of [[1, 2], [5, 0], [3, 5], [7, 4], [0, 6], [6, 7]]) c.fillRect(x, y, 1, 1);
    c.fillStyle = "#8d7355";                    // light speckle
    for (const [x, y] of [[2, 0], [6, 2], [0, 3], [4, 7], [7, 1]]) c.fillRect(x, y, 1, 1);
    floorPat = wctx.createPattern(p, "repeat");
  }

  /* --------------------------- cycles helpers --------------------------- */

  function buildGridDots(gw, gh) {
    gridDots = document.createElement("canvas");
    gridDots.width = W; gridDots.height = H;
    const c = gridDots.getContext("2d");
    c.fillStyle = "rgba(140,160,255,.07)";
    for (let x = 0; x <= gw; x += 4) {
      for (let y = 0; y <= gh; y += 4) c.fillRect(x * cellW, y * cellH, 1, 1);
    }
  }

  function paintCell(x, y, pid) {
    const m = meta.get(pid);
    if (!m || !trailCtx) return;
    const px = x * cellW, py = y * cellH;
    trailCtx.fillStyle = shade(m.color, 0.62);
    trailCtx.fillRect(px, py, cellW, cellH);
    trailCtx.fillStyle = m.color;
    trailCtx.fillRect(px + 1, py + 1, cellW - 2, cellH - 2);
  }

  function clearCells(pid) {
    for (const [key, owner] of cellMap) {
      if (owner === pid) cellMap.delete(key);
    }
    if (!trailCtx) return;
    trailCtx.clearRect(0, 0, W, H);
    for (const [key, owner] of cellMap) {
      const [x, y] = key.split(",");
      paintCell(+x, +y, owner);
    }
  }

  function drawCycles(s) {
    if (gridDots) wctx.drawImage(gridDots, 0, 0);
    if (trailCv) wctx.drawImage(trailCv, 0, 0);
    // closing walls
    const m = s.margin || 0;
    if (m > 0) {
      const mx = m * cellW, my = m * cellH;
      const pulse = 0.16 + 0.08 * Math.sin(performance.now() / 200);
      wctx.fillStyle = `rgba(255,70,100,${pulse})`;
      wctx.fillRect(0, 0, W, my);
      wctx.fillRect(0, H - my, W, my);
      wctx.fillRect(0, my, mx, H - 2 * my);
      wctx.fillRect(W - mx, my, mx, H - 2 * my);
      wctx.strokeStyle = "rgba(255,90,120,.85)";
      wctx.lineWidth = 1;
      wctx.strokeRect(mx + 0.5, my + 0.5, W - 2 * mx - 1, H - 2 * my - 1);
    }
    // heads
    for (const e of s.ents) {
      if (!e.alive) continue;
      const pm = meta.get(e.pid) || { color: "#888" };
      if (e.boosting) {                       // short speed-line trail
        const spd = Math.hypot(e.vx, e.vy) || 1;
        const dx = e.vx / spd, dy = e.vy / spd;
        wctx.strokeStyle = "rgba(255,255,255,.6)";
        wctx.lineWidth = 1;
        for (let i = 1; i <= 3; i++) {
          wctx.globalAlpha = 0.5 / i;
          const bx = e.x - dx * cellW * i * 1.4, by = e.y - dy * cellH * i * 1.4;
          wctx.beginPath();
          wctx.moveTo(bx, by);
          wctx.lineTo(bx - dx * cellW * 0.8, by - dy * cellH * 0.8);
          wctx.stroke();
        }
      }
      wctx.globalAlpha = e.boosting ? 0.45 : 0.3;
      wctx.fillStyle = pm.color;
      const gr = e.boosting ? 1.3 : 1;
      wctx.fillRect(e.x - cellW * gr, e.y - cellH * gr, cellW * 2 * gr, cellH * 2 * gr);   // glow
      wctx.globalAlpha = 1;
      wctx.fillStyle = e.boosting ? mix(pm.color, "#ffffff", 0.55) : pm.color;
      wctx.fillRect(e.x - cellW / 2, e.y - cellH / 2, cellW, cellH);
      wctx.fillStyle = "#fff";
      wctx.fillRect(e.x - 1, e.y - 1, 2, 2);
    }
  }

  /* ----------------------------- ski helpers ----------------------------- */

  function buildSkiLayers() {
    const tile = document.createElement("canvas");
    tile.width = 64; tile.height = 64;
    let c = tile.getContext("2d");
    c.fillStyle = "#cfdbea";
    c.fillRect(0, 0, 64, 64);
    for (let i = 0; i < 30; i++) {
      c.fillStyle = Math.random() < 0.5 ? "#bccadd" : "#e6eef9";
      c.fillRect((Math.random() * 64) | 0, (Math.random() * 64) | 0, 1, 1);
    }
    skiSnowPat = wctx.createPattern(tile, "repeat");
    skiSpeck = document.createElement("canvas");   // slower parallax layer
    skiSpeck.width = W; skiSpeck.height = H;
    c = skiSpeck.getContext("2d");
    c.fillStyle = "rgba(120,140,170,.16)";
    for (let i = 0; i < 44; i++) {
      c.fillRect((Math.random() * W) | 0, (Math.random() * H) | 0,
                 (2 + Math.random() * 6) | 0, 2);
    }
  }

  function drawTree(x, y) {
    wctx.fillStyle = "rgba(60,80,110,.30)";
    wctx.beginPath(); wctx.ellipse(x + 2, y + 5, 6, 3, 0, 0, Math.PI * 2); wctx.fill();
    wctx.fillStyle = "#5a3d20";
    wctx.fillRect(x - 1, y + 3, 2, 4);
    wctx.fillStyle = "#245233";
    wctx.beginPath(); wctx.moveTo(x - 6, y + 4); wctx.lineTo(x + 6, y + 4); wctx.lineTo(x, y - 4); wctx.fill();
    wctx.fillStyle = "#2e6b3f";
    wctx.beginPath(); wctx.moveTo(x - 5, y); wctx.lineTo(x + 5, y); wctx.lineTo(x, y - 8); wctx.fill();
    wctx.fillStyle = "#eef4fb";
    wctx.fillRect(x - 1, y - 8, 3, 1);
  }

  function drawRock(x, y) {
    wctx.fillStyle = "rgba(60,80,110,.30)";
    wctx.beginPath(); wctx.ellipse(x + 2, y + 5, 7, 3, 0, 0, Math.PI * 2); wctx.fill();
    wctx.fillStyle = "#8a93a3";
    wctx.beginPath();
    wctx.moveTo(x - 7, y + 4); wctx.lineTo(x - 4, y - 4); wctx.lineTo(x + 3, y - 5);
    wctx.lineTo(x + 7, y + 2); wctx.lineTo(x + 4, y + 5);
    wctx.fill();
    wctx.fillStyle = "#6d7686";
    wctx.fillRect(x, y, 4, 3);
    wctx.fillStyle = "#eef4fb";
    wctx.fillRect(x - 3, y - 4, 5, 1);
  }

  function drawSkier(e) {
    const m = meta.get(e.pid) || { color: "#888" };
    const { x, y } = e;
    wctx.fillStyle = "rgba(60,80,110,.30)";
    wctx.beginPath(); wctx.ellipse(x + 1, y + 4, 5, 2.5, 0, 0, Math.PI * 2); wctx.fill();
    wctx.save();
    wctx.translate(x, y);
    if (e.tumble > 0) wctx.rotate(Math.sin(performance.now() / 40) * 1.2);
    else wctx.rotate(Math.max(-0.6, Math.min(0.6, e.vx * 0.025)));
    wctx.strokeStyle = "#5a3d20";
    wctx.lineWidth = 1.5;
    wctx.beginPath();
    wctx.moveTo(-3, 2); wctx.lineTo(-3, 9);
    wctx.moveTo(3, 2); wctx.lineTo(3, 9);
    wctx.stroke();
    wctx.fillStyle = m.color;
    wctx.beginPath(); wctx.arc(0, 0, 6, 0, Math.PI * 2); wctx.fill();
    wctx.lineWidth = 1.2;
    wctx.strokeStyle = shade(m.color, 0.55);
    wctx.stroke();
    wctx.fillStyle = "#12122a";               // goggles
    wctx.fillRect(-3, -2, 6, 2);
    wctx.fillStyle = "rgba(255,255,255,.6)";
    wctx.fillRect(-2, -2, 1, 1);
    wctx.restore();
    if (e.tumble > 0) {                       // dizzy stars
      const t = performance.now() / 150;
      wctx.fillStyle = "#ffe14d";
      for (let i = 0; i < 3; i++) {
        const a = t + i * 2.09;
        wctx.fillRect(x + Math.cos(a) * 9 - 1, y - 8 + Math.sin(a) * 3, 2, 2);
      }
    } else if (Math.abs(e.vx) > 10 && Math.random() < 0.5) {   // carving hard: kick up spray
      const dir = e.vx > 0 ? -1 : 1;
      particles.push({
        x: x + dir * 4, y: y + 6,
        vx: dir * (20 + Math.random() * 25), vy: 15 + Math.random() * 20,
        life: 0.22 + Math.random() * 0.14, t: 0, size: 1,
        color: Math.random() < 0.5 ? "#fff" : "#dfe9f7", grav: 50,
      });
    }
  }

  function drawAvalanche() {
    const t = performance.now();
    const band = 13;
    wctx.fillStyle = "#e8eef7";
    wctx.fillRect(0, 0, W, band - 4);
    for (let i = 0; i < 26; i++) {            // churning front
      const x = (i * 19 + t / (30 + (i % 5) * 9)) % W;
      const y = band - 6 + Math.sin(t / 90 + i) * 3;
      wctx.fillStyle = i % 3 ? "#f6fafe" : "#c3d2e4";
      wctx.beginPath(); wctx.arc(x, y, 4 + (i % 3) * 2, 0, Math.PI * 2); wctx.fill();
    }
    if (Math.random() < 0.5) {
      particles.push({ x: Math.random() * W, y: band, vx: (Math.random() - 0.5) * 20,
                       vy: 30 + Math.random() * 40, life: 0.4, t: 0, size: 1,
                       color: "#fff", grav: 0 });
    }
  }

  function drawSnowball(x, y, vx, vy) {
    const spd = Math.hypot(vx, vy) || 1;
    const dx = vx / spd, dy = vy / spd;
    // ground shadow, separated from the ball so it reads as airborne
    wctx.fillStyle = "rgba(20,30,55,.30)";
    wctx.beginPath(); wctx.ellipse(x + 1.5, y + 4, 3, 1.5, 0, 0, Math.PI * 2); wctx.fill();
    // short velocity-aligned motion streak (2 ghost dots max)
    for (let i = 2; i >= 1; i--) {
      wctx.fillStyle = `rgba(220,232,248,${0.16 * i})`;
      wctx.beginPath();
      wctx.arc(x - dx * i * 3.2, y - dy * i * 3.2, 2.6 - i * 0.5, 0, Math.PI * 2);
      wctx.fill();
    }
    // body: subtle blue tint pops it off the snow, crisp navy outline
    wctx.fillStyle = "#e4edfb";
    wctx.beginPath(); wctx.arc(x, y, 3.8, 0, Math.PI * 2); wctx.fill();
    wctx.lineWidth = 1.2;
    wctx.strokeStyle = "#12122a";
    wctx.stroke();
    wctx.fillStyle = "rgba(255,255,255,.85)";
    wctx.fillRect(x - 1.6, y - 1.8, 1.4, 1.4);
  }

  function drawSki(s) {
    const camPx = s.cam * S;
    wctx.save();                              // base snow, full scroll speed
    wctx.translate(0, -(camPx % 64));
    wctx.fillStyle = skiSnowPat;
    wctx.fillRect(0, 0, W, H + 64);
    wctx.restore();
    const off = (camPx * 0.5) % H;            // speckle layer at half speed
    wctx.drawImage(skiSpeck, 0, -off);
    wctx.drawImage(skiSpeck, 0, H - off);
    wctx.fillStyle = "rgba(90,110,140,.25)";  // side banks
    wctx.fillRect(0, 0, 4, H);
    wctx.fillRect(W - 4, 0, 4, H);
    for (const [oid, ob] of obsMap) {
      const sy = (ob[1] - s.cam) * S;
      if (sy < -20) { obsMap.delete(oid); continue; }
      if (sy > H + 20) continue;
      if (ob[2] === 0) drawTree(ob[0] * S, sy); else drawRock(ob[0] * S, sy);
    }
    for (const b of liveProjectiles(skiBalls)) {   // snowballs, dead-reckoned
      drawSnowball(b.x * S, (b.y - s.cam) * S, b.vx, b.vy);
    }
    for (const e of s.ents) {
      if (e.alive) drawSkier(e);
    }
    drawAvalanche();
  }

  /* ---------------------------- planes helpers ---------------------------- */

  function mkClouds(alpha, n, size) {
    const cv = document.createElement("canvas");
    cv.width = W; cv.height = 70;
    const c = cv.getContext("2d");
    c.fillStyle = `rgba(255,225,200,${alpha})`;
    for (let i = 0; i < n; i++) {
      const x = Math.random() * W, y = 14 + Math.random() * 38;
      for (let j = 0; j < 5; j++) {
        c.beginPath();
        c.arc(x + (j - 2) * size * 0.7, y + ((j % 2) - 0.5) * 4,
              size - Math.abs(j - 2) * 3, 0, Math.PI * 2);
        c.fill();
      }
    }
    return cv;
  }

  function buildSkyLayers() {
    skyBg = document.createElement("canvas");
    skyBg.width = W; skyBg.height = H;
    const c = skyBg.getContext("2d");
    const g = c.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "#141b33");
    g.addColorStop(0.55, "#3a2c4e");
    g.addColorStop(1, "#8a4a2e");
    c.fillStyle = g;
    c.fillRect(0, 0, W, H);
    const sg = c.createRadialGradient(360, 218, 4, 360, 218, 60);   // low sun
    sg.addColorStop(0, "rgba(255,190,110,.9)");
    sg.addColorStop(0.25, "rgba(255,160,80,.35)");
    sg.addColorStop(1, "transparent");
    c.fillStyle = sg;
    c.fillRect(280, 150, 170, 120);
    c.fillStyle = "#ffce8f";
    c.beginPath(); c.arc(360, 218, 9, 0, Math.PI * 2); c.fill();
    for (let i = 0; i < 40; i++) {
      c.fillStyle = `rgba(255,240,220,${0.2 + Math.random() * 0.4})`;
      c.fillRect((Math.random() * W) | 0, (Math.random() * H * 0.4) | 0, 1, 1);
    }
    c.fillStyle = "rgba(20,16,30,.75)";       // distant zeppelin
    c.beginPath(); c.ellipse(96, 60, 26, 8, -0.05, 0, Math.PI * 2); c.fill();
    c.fillRect(88, 66, 14, 5);
    c.fillRect(120, 57, 6, 6);
    cloudFar = mkClouds(0.10, 5, 10);
    cloudNear = mkClouds(0.16, 4, 16);
  }

  function buildIslandLayer(islands) {
    isleCv = document.createElement("canvas");
    isleCv.width = W;
    isleCv.height = H;
    const c = isleCv.getContext("2d");
    for (const [wx, wy, wr] of islands) {
      const x = wx * S, y = wy * S, r = wr * S;
      c.fillStyle = "#4a3628";                    // rocky body
      c.beginPath(); c.arc(x, y, r, 0, Math.PI * 2); c.fill();
      c.fillStyle = "#5d4433";                    // strata speckles
      for (let i = 0; i < 6; i++) {
        c.fillRect(x - r + Math.random() * r * 1.6,
                   y + r * 0.15 + Math.random() * r * 0.6, 3, 2);
      }
      c.fillStyle = "#3f8a4d";                    // grass dome
      c.beginPath(); c.ellipse(x, y - r * 0.18, r * 0.98, r * 0.62, 0, Math.PI, Math.PI * 2);
      c.fill();
      c.fillStyle = "#54a862";
      c.beginPath(); c.ellipse(x, y - r * 0.26, r * 0.85, r * 0.45, 0, Math.PI, Math.PI * 2);
      c.fill();
      c.fillStyle = "#79c184";                    // grass tufts
      for (let i = 0; i < 5; i++) {
        c.fillRect(x - r * 0.7 + Math.random() * r * 1.4,
                   y - r * 0.5 + Math.random() * r * 0.25, 2, 1);
      }
      c.fillStyle = "#4a3628";                    // drifting crumbs below
      c.fillRect(x - r * 0.25, y + r * 1.18, 3, 3);
      c.fillRect(x + r * 0.4, y + r * 1.05, 2, 2);
    }
  }

  function drawGusts(t) {
    if (!arena || !arena.gusts) return;
    for (const [wx, wy, wr, a100] of arena.gusts) {
      const x = wx * S, y = wy * S, r = wr * S, ang = a100 / 100;
      const ca = Math.cos(ang), sa = Math.sin(ang);
      wctx.fillStyle = "rgba(160,220,255,.05)";
      wctx.beginPath(); wctx.arc(x, y, r, 0, Math.PI * 2); wctx.fill();
      wctx.strokeStyle = "rgba(190,235,255,.55)";
      wctx.lineWidth = 1;
      for (let i = 0; i < 7; i++) {               // streaks flow along the wind
        const ph = (t * 0.00035 * (1 + (i % 3) * 0.25) + i * 0.143) % 1;
        const off = (i / 7 - 0.5) * 2 * r * 0.8;
        const along = (ph - 0.5) * 2 * r;
        const px = x + ca * along - sa * off;
        const py = y + sa * along + ca * off;
        if (Math.hypot(px - x, py - y) > r) continue;
        wctx.globalAlpha = 0.2 + 0.5 * Math.sin(ph * Math.PI);
        wctx.beginPath();
        wctx.moveTo(px - ca * 7, py - sa * 7);
        wctx.lineTo(px, py);
        wctx.stroke();
      }
      wctx.globalAlpha = 1;
    }
  }

  function drawPlane(e, t) {
    const m = meta.get(e.pid) || { color: "#888" };
    const bank = Math.max(-1, Math.min(1, (e.turn || 0) * 6));  // banking suggestion from turn rate
    wctx.save();
    wctx.translate(e.x, e.y);
    wctx.rotate(e.ang);
    wctx.fillStyle = shade(m.color, 0.62);    // wings (foreshorten + shift into the bank)
    wctx.fillRect(-2, -7 + bank * 2.5, 5, 14 - Math.abs(bank) * 4);
    wctx.fillStyle = m.color;                 // fuselage
    wctx.fillRect(-7, -2.5, 14, 5);
    wctx.fillStyle = "#e2b25a";               // brass nose
    wctx.fillRect(6, -1.5, 2, 3);
    wctx.fillStyle = shade(m.color, 0.5);     // tail
    wctx.fillRect(-8, -4, 3, 8);
    wctx.fillStyle = "rgba(255,255,255,.7)";  // cockpit glint
    wctx.fillRect(0, -1, 2, 2);
    wctx.strokeStyle = "rgba(240,240,255,.65)";
    wctx.lineWidth = 1;
    const pr = 4 * Math.abs(Math.sin(t / 25));  // spinning prop
    wctx.beginPath(); wctx.moveTo(8.5, -pr); wctx.lineTo(8.5, pr); wctx.stroke();
    wctx.restore();
    if (e.hp === 1 && Math.random() < 0.35) { // smoking on the last heart
      particles.push({ x: e.x - Math.cos(e.ang) * 8, y: e.y - Math.sin(e.ang) * 8,
                       vx: (Math.random() - 0.5) * 12, vy: (Math.random() - 0.5) * 12 - 6,
                       life: 0.5 + Math.random() * 0.3, t: 0, size: 2,
                       color: "rgba(120,120,130,.8)", grav: -14 });
    }
  }

  function drawBullet(x, y, vx, vy) {
    const spd = Math.hypot(vx, vy) || 1;
    const dx = vx / spd, dy = vy / spd;
    const len = 7;                              // short velocity-aligned streak
    const gx = x - dx * len, gy = y - dy * len;
    const grad = wctx.createLinearGradient(gx, gy, x, y);
    grad.addColorStop(0, "rgba(255,180,40,0)");
    grad.addColorStop(1, "rgba(255,212,59,.9)");
    wctx.strokeStyle = grad;
    wctx.lineWidth = 2;
    wctx.beginPath(); wctx.moveTo(gx, gy); wctx.lineTo(x, y); wctx.stroke();
    wctx.fillStyle = "#3a2408";                  // dark outline
    wctx.beginPath(); wctx.arc(x, y, 2.3, 0, Math.PI * 2); wctx.fill();
    wctx.fillStyle = "#ffd43b";                  // bright yellow core, high contrast on the sunset sky
    wctx.beginPath(); wctx.arc(x, y, 1.5, 0, Math.PI * 2); wctx.fill();
    wctx.fillStyle = "rgba(255,255,255,.9)";
    wctx.fillRect(x - 0.5, y - 0.5, 1, 1);
  }

  function drawPlanes(s) {
    const t = performance.now();
    wctx.drawImage(skyBg, 0, 0);
    const o1 = (t * 0.004) % W;               // far clouds drift (parallax)
    wctx.drawImage(cloudFar, -o1, 52);
    wctx.drawImage(cloudFar, W - o1, 52);
    drawGusts(t);
    if (isleCv) wctx.drawImage(isleCv, 0, 0);
    const o2 = (t * 0.012) % W;               // near clouds pass over islands
    wctx.drawImage(cloudNear, -o2, 150);
    wctx.drawImage(cloudNear, W - o2, 150);
    for (const b of liveProjectiles(planeBullets)) {   // bullets, dead-reckoned
      const x = (((b.x % 960) + 960) % 960) * S;
      const y = (((b.y % 540) + 540) % 540) * S;
      drawBullet(x, y, b.vx, b.vy);
    }
    for (const e of s.ents) {
      if (!e.alive) continue;
      if (e.inv && (((t / 90) | 0) % 2)) continue;   // hit blink
      drawPlane(e, t);
      // riding a gust: boost streaks behind the plane
      for (const [gx, gy, gr] of arena.gusts || []) {
        if (Math.hypot(e.x - gx * S, e.y - gy * S) < gr * S) {
          wctx.strokeStyle = "rgba(190,235,255,.6)";
          wctx.lineWidth = 1;
          const ca = Math.cos(e.ang), sa = Math.sin(e.ang);
          for (const o of [-3, 3]) {
            wctx.beginPath();
            wctx.moveTo(e.x - ca * 9 - sa * o, e.y - sa * 9 + ca * o);
            wctx.lineTo(e.x - ca * 17 - sa * o, e.y - sa * 17 + ca * o);
            wctx.stroke();
          }
          break;
        }
      }
    }
  }

  function drawHearts(ents) {
    const sx = cssW / W, sy = cssH / H;
    octx.font = "9px ui-monospace, Menlo, monospace";
    octx.textAlign = "center";
    octx.fillStyle = "#ff5a6e";
    for (const e of ents) {
      if (!e.alive || e.hp == null) continue;
      octx.fillText("♥".repeat(e.hp), e.x * sx, (e.y + 13) * sy + 8);
    }
  }

  /* --------------------------- bumper-ball helpers --------------------------- */

  function drawGoalMouth(edgeX, y0, y1, color) {
    const depth = 10, dir = edgeX === 0 ? 1 : -1;
    wctx.fillStyle = "rgba(8,10,20,.55)";           // net pocket shadow
    wctx.fillRect(edgeX === 0 ? 0 : W - depth, y0, depth, y1 - y0);
    wctx.strokeStyle = "rgba(230,240,255,.35)";     // net crosshatch
    wctx.lineWidth = 1;
    for (let yy = y0; yy <= y1; yy += 4) {
      wctx.beginPath(); wctx.moveTo(edgeX, yy); wctx.lineTo(edgeX + dir * depth, yy); wctx.stroke();
    }
    for (let xx = 0; xx <= depth; xx += 4) {
      const x = edgeX + dir * xx;
      wctx.beginPath(); wctx.moveTo(x, y0); wctx.lineTo(x, y1); wctx.stroke();
    }
    wctx.strokeStyle = color;                       // team-colored goal line + posts
    wctx.lineWidth = 2;
    const lineX = edgeX === 0 ? 0.5 : W - 0.5;
    wctx.beginPath(); wctx.moveTo(lineX, y0); wctx.lineTo(lineX, y1); wctx.stroke();
    wctx.beginPath(); wctx.moveTo(edgeX, y0); wctx.lineTo(edgeX + dir * depth, y0); wctx.stroke();
    wctx.beginPath(); wctx.moveTo(edgeX, y1); wctx.lineTo(edgeX + dir * depth, y1); wctx.stroke();
  }

  function drawBumperField() {
    const goalH = (arena.goalH || 180) * S;
    const gy0 = H / 2 - goalH / 2, gy1 = H / 2 + goalH / 2;
    wctx.strokeStyle = "rgba(255,255,255,.16)";
    wctx.lineWidth = 1;
    wctx.beginPath(); wctx.moveTo(W / 2, 0); wctx.lineTo(W / 2, H); wctx.stroke();
    wctx.beginPath(); wctx.arc(W / 2, H / 2, 34, 0, Math.PI * 2); wctx.stroke();
    wctx.fillStyle = "rgba(255,255,255,.4)";
    wctx.beginPath(); wctx.arc(W / 2, H / 2, 2, 0, Math.PI * 2); wctx.fill();
    drawGoalMouth(0, gy0, gy1, TEAM_HUES[0]);
    drawGoalMouth(W, gy0, gy1, TEAM_HUES[1]);
  }

  function drawTeamRing(e, team) {
    if (team == null) return;                       // thin rim so 12 player colors still read by team
    wctx.beginPath();
    wctx.arc(e.x, e.y, e.r + 2.2, 0, Math.PI * 2);
    wctx.strokeStyle = TEAM_HUES[team];
    wctx.lineWidth = 2;
    wctx.stroke();
  }

  function drawBumperBall(x, y, vx, vy) {
    const spd = Math.hypot(vx, vy);
    if (spd > 40) {                                  // short motion streak at speed
      const dx = vx / spd, dy = vy / spd;
      for (let i = 2; i >= 1; i--) {
        wctx.fillStyle = `rgba(255,225,140,${0.16 * i})`;
        wctx.beginPath();
        wctx.arc(x - dx * i * 3.4, y - dy * i * 3.4, 4.6 - i * 0.6, 0, Math.PI * 2);
        wctx.fill();
      }
    }
    wctx.fillStyle = "rgba(0,0,0,.35)";
    wctx.beginPath(); wctx.ellipse(x + 1, y + 3, 4.6, 2.2, 0, 0, Math.PI * 2); wctx.fill();
    wctx.fillStyle = "#fff6d8";                       // white/gold body
    wctx.beginPath(); wctx.arc(x, y, 5, 0, Math.PI * 2); wctx.fill();
    wctx.lineWidth = 1.3;
    wctx.strokeStyle = "#3a2f14";
    wctx.stroke();
    wctx.fillStyle = "#d9b85e";                       // a single dark patch hints "ball", not "marble"
    wctx.beginPath(); wctx.arc(x - 1.3, y - 1.1, 1.5, 0, Math.PI * 2); wctx.fill();
    wctx.fillStyle = "rgba(255,255,255,.9)";
    wctx.fillRect(x - 2, y - 2.3, 1.3, 1.3);
  }

  function drawKickoffShimmer() {
    const pulse = 0.3 + 0.35 * Math.sin(performance.now() / 140);
    wctx.strokeStyle = `rgba(255,255,255,${pulse})`;
    wctx.lineWidth = 2;
    wctx.beginPath(); wctx.arc(W / 2, H / 2, 38, 0, Math.PI * 2); wctx.stroke();
  }

  function drawBumper(s) {
    drawBumperField();
    for (const b of liveProjectiles(bumperBall)) {    // ball, dead-reckoned
      drawBumperBall(b.x * S, b.y * S, b.vx, b.vy);
    }
    const ents = s.ents.slice().sort((a, b) => a.y - b.y);
    for (const e of ents) {
      drawTeamRing(e, bumperTeamOf.get(e.pid));
      drawPlayer(e);
    }
    if (s.ko) drawKickoffShimmer();
  }

  function drawBumperScore(score) {
    if (!score) return;
    octx.font = "900 22px ui-monospace, Menlo, monospace";
    const rt = String(score[0]), mid = " — ", bt = String(score[1]);
    const wR = octx.measureText(rt).width, wM = octx.measureText(mid).width, wB = octx.measureText(bt).width;
    const x0 = cssW / 2 - (wR + wM + wB) / 2, y = 24;
    let x = x0;
    octx.textAlign = "left";
    octx.fillStyle = "rgba(0,0,0,.7)";
    octx.fillText(rt + mid + bt, x0 + 2, y + 2);
    octx.fillStyle = TEAM_HUES[0];
    octx.fillText(rt, x, y); x += wR;
    octx.fillStyle = "rgba(255,255,255,.85)";
    octx.fillText(mid, x, y); x += wM;
    octx.fillStyle = TEAM_HUES[1];
    octx.fillText(bt, x, y);
    octx.textAlign = "center";
  }

  /* ------------------------------ round api ------------------------------ */

  function startRound(a, roster, playersMeta) {
    arena = a;
    meta = new Map();
    for (const [pid, p] of playersMeta) meta.set(pid, { name: p.name, color: p.color });
    snaps = [];
    particles = [];
    ghosts = [];
    fallers = [];
    shake = 0;
    cellMap = new Map();
    obsMap = new Map();
    skiBalls = new Map();
    planeBullets = new Map();
    bumperBall = new Map();
    bumperTeamOf = new Map();
    if (a.g === "bumper") {
      for (const t of [0, 1]) {
        for (const pid of (a.teams && a.teams[t]) || []) bumperTeamOf.set(pid, t);
      }
    }
    if (a.g === "cycles") {
      cellW = W / a.gw;
      cellH = H / a.gh;
      trailCv = document.createElement("canvas");
      trailCv.width = W;
      trailCv.height = H;
      trailCtx = trailCv.getContext("2d");
      buildGridDots(a.gw, a.gh);
    }
    if (a.g === "ski" && !skiSnowPat) buildSkiLayers();
    if (a.g === "planes") {
      if (!skyBg) buildSkyLayers();
      buildIslandLayer(a.islands || []);
    }
    if (!bg) buildBg();
    if (!floorPat) buildFloor();
  }

  function addSnapshot(m) {
    const now = performance.now();
    if (snaps.length) {
      const gap = now - snaps[snaps.length - 1].t;
      if (gap > 5 && gap < 1000) snapGap = snapGap * 0.85 + gap * 0.15;
    }
    if (m.g === "cycles") {
      for (const [x, y, pid] of m.cells || []) {
        cellMap.set(x + "," + y, pid);
        paintCell(x, y, pid);
      }
      const e = new Map();
      for (const row of m.heads) {
        // new: [pid,gx,gy,alive,boost01,dx,dy,boosting] (8 fields)
        // legacy: [pid,gx,gy,alive,dx,dy] (6 fields) — tolerate both
        const [pid, gx, gy, alive, f4, f5, f6, f7] = row;
        e.set(pid, row.length >= 8
          ? [gx, gy, alive, f5, f6, f4, f7]     // -> [x,y,alive,dx,dy,boost,boosting]
          : [gx, gy, alive, f4, f5, 0, 0]);
      }
      snaps.push({ t: now, margin: m.margin || 0, e, cyc: true });
    } else if (m.g === "ski") {
      for (const [oid, x, y, k] of m.obs || []) obsMap.set(oid, [x, y, k]);
      const e = new Map();
      for (const row of m.e) e.set(row[0], row.slice(1)); // [x,y,alive,cd,tumble]
      upsertProjectiles(skiBalls, m.balls || []);          // [bid,x,y,vx,vy]
      snaps.push({ t: now, ski: true, cam: m.cam, spd: m.spd, e });
    } else if (m.g === "planes") {
      const e = new Map();
      for (const row of m.e) e.set(row[0], row.slice(1)); // [x,y,alive,cd,ang,hp,inv]
      upsertProjectiles(planeBullets, m.b || []);          // [bid,x,y,vx,vy]
      snaps.push({ t: now, pln: true, e });
    } else if (m.g === "bumper") {
      const e = new Map();
      for (const row of m.e) e.set(row[0], row.slice(1)); // [x,y,alive,dash01]
      const ball = m.ball || [480, 270, 0, 0];
      upsertProjectiles(bumperBall, [[0, ball[0], ball[1], ball[2], ball[3]]]);
      snaps.push({ t: now, bmp: true, score: m.score || [0, 0], ko: m.ko, e });
    } else {
      const e = new Map();
      for (const row of m.e) e.set(row[0], row.slice(1)); // [x,y,alive,cd,r]
      snaps.push({ t: now, R: m.R, wind: m.wind || null, e });
    }
    if (snaps.length > 5) snaps.shift();
  }

  function resize(w, h) {
    cssW = w; cssH = h;
    dpr = window.devicePixelRatio || 1;
    overlay.width = Math.round(w * dpr);
    overlay.height = Math.round(h * dpr);
    overlay.style.width = w + "px";
    overlay.style.height = h + "px";
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  /* -------------------------------- fx api -------------------------------- */

  function spark(x, y, n, colors, speed = 90) {
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2;
      const v = speed * (0.3 + Math.random());
      particles.push({
        x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v,
        life: 0.35 + Math.random() * 0.4, t: 0,
        size: Math.random() < 0.3 ? 2 : 1,
        color: colors[(Math.random() * colors.length) | 0],
        grav: 0,
      });
    }
  }

  function latestPos(pid) {
    const s = snaps[snaps.length - 1];
    if (!s) return null;
    const e = s.e.get(pid);
    return e ? { x: e[0] * S, y: e[1] * S, r: (e[4] || 13) * S } : null;
  }

  function worldPos(pid) {          // generic screen pos from the latest snap
    const s = snaps[snaps.length - 1];
    const e = s && s.e.get(pid);
    return e ? { x: e[0] * S, y: e[1] * S } : null;
  }

  function skiPos(pid) {            // ski world y -> screen via the camera
    const s = snaps[snaps.length - 1];
    const e = s && s.ski && s.e.get(pid);
    return e ? { x: e[0] * S, y: (e[1] - s.cam) * S } : null;
  }

  function cyclePos(pid) {          // cycles head screen pos (grid -> buffer px)
    const s = snaps[snaps.length - 1];
    const e = s && s.cyc && s.e.get(pid);
    return e ? { x: (e[0] + 0.5) * cellW, y: (e[1] + 0.5) * cellH } : null;
  }

  function planeHeading(pid) {      // current heading (radians) from the latest snap
    const s = snaps[snaps.length - 1];
    const e = s && s.pln && s.e.get(pid);
    return e ? (e[4] || 0) / 100 : null;
  }

  function fx(events) {
    for (const ev of events) {
      const kind = ev[0];
      if (kind === "hit") {
        const [, x, y, i] = ev;
        spark(x * S, y * S, 4 + Math.round(i * 12), ["#fff", "#ffe14d", "#ffa53b"]);
        shake = Math.max(shake, 1.5 + i * 5);
      } else if (kind === "dash") {
        const pid = ev[1];
        const p = latestPos(pid);
        const m = meta.get(pid);
        if (p && m) ghosts.push({ x: p.x, y: p.y, r: p.r, color: m.color, life: 0.3, t: 0 });
        shake = Math.max(shake, 1.2);
      } else if (kind === "die") {
        const [, pid, gx, gy] = ev;
        const m = meta.get(pid) || { color: "#fff" };
        spark((gx + 0.5) * cellW, (gy + 0.5) * cellH, 22, [m.color, "#fff", "#ffe14d"], 70);
        shake = Math.max(shake, 5);
      } else if (kind === "clear") {
        clearCells(ev[1]);
      } else if (kind === "wall") {
        shake = Math.max(shake, 2);
      } else if (kind === "boost") {
        const pid = ev[1];
        const p = cyclePos(pid);
        const m = meta.get(pid);
        if (p) spark(p.x, p.y, 10, [m ? m.color : "#fff", "#fff"], 70);
        shake = Math.max(shake, 1.5);
      } else if (kind === "bonk") {
        const [, , x, sy] = ev;          // ski events carry screen-relative y
        spark(x * S, sy * S, 10, ["#fff", "#8a6248", "#e6eef9"], 70);
        shake = Math.max(shake, 3);
      } else if (kind === "splat") {
        const p = skiPos(ev[1]);
        if (p) {
          spark(p.x, p.y, 18, ["#fff", "#e6eef9", "#bcd0ea"], 95);
          shake = Math.max(shake, 4);
        }
      } else if (kind === "wipe") {
        const [, , x, sy] = ev;
        spark(x * S, sy * S, 28, ["#fff", "#e6eef9"], 115);
        shake = Math.max(shake, 7);
      } else if (kind === "shoot") {
        const p = worldPos(ev[1]);
        if (p) {
          const ang = planeHeading(ev[1]);
          const nx = ang != null ? Math.cos(ang) : 0, ny = ang != null ? Math.sin(ang) : 0;
          spark(p.x + nx * 8, p.y + ny * 8, 5, ["#ffd43b", "#fff", "#ffb347"], 85);
        }
      } else if (kind === "hitp") {
        const [, , x, y] = ev;
        spark(x * S, y * S, 14, ["#ffd43b", "#ff8a5a", "#fff"], 100);
        shake = Math.max(shake, 4);
      } else if (kind === "clash") {
        const [, x, y] = ev;
        spark(x * S, y * S, 8, ["#e2b25a", "#fff"], 80);
        shake = Math.max(shake, 2.5);
      } else if (kind === "thud") {
        const [, , x, y] = ev;
        spark(x * S, y * S, 9, ["#5d4433", "#79c184", "#fff"], 75);
        shake = Math.max(shake, 2.5);
      } else if (kind === "puff") {
        const [, x, y] = ev;
        spark(x * S, y * S, 5, ["#ccc", "#fff"], 40);
      } else if (kind === "down") {
        const [, pid, x, y] = ev;
        const m = meta.get(pid) || { color: "#888" };
        spark(x * S, y * S, 34, [m.color, "#ffd43b", "#ff8a5a", "#666"], 125);
        fallers.push({ x: x * S, y: y * S, vx: (Math.random() - 0.5) * 30, vy: 26,
                       r: 6, color: m.color, rot: 0, vr: 9, t: 0, life: 1.1 });
        shake = Math.max(shake, 7);
      } else if (kind === "goal") {
        const [, team, x, y] = ev;
        spark(x * S, y * S, 40, [TEAM_HUES[team] || "#fff", "#fff", "#ffd43b"], 130);
        shake = Math.max(shake, 8);
        flash("GOAL!");
      } else if (kind === "fall") {
        const [, pid, x, y, vx, vy] = ev;
        const m = meta.get(pid) || { color: "#888", name: "?" };
        fallers.push({
          x: x * S, y: y * S, vx: vx * S * 0.7, vy: vy * S * 0.7,
          r: (latestPos(pid) || { r: 6.5 }).r, color: m.color, name: m.name,
          rot: 0, vr: (Math.random() - 0.5) * 10, t: 0, life: 1.1,
        });
        spark(x * S, y * S, 16, [m.color, "#fff"]);
        shake = Math.max(shake, 6);
      }
    }
  }

  function flash(text) { flashMsg = { text, t0: performance.now() }; }

  function celebrate(colors) {
    const cols = colors.length ? colors : ["#ffd43b"];
    for (let i = 0; i < 130; i++) {
      particles.push({
        x: W / 2 + (Math.random() - 0.5) * 160,
        y: H / 2 + (Math.random() - 0.5) * 60,
        vx: (Math.random() - 0.5) * 160,
        vy: -40 - Math.random() * 120,
        life: 1.2 + Math.random(), t: 0,
        size: Math.random() < 0.4 ? 2 : 1,
        color: [...cols, "#fff", "#ffe14d"][(Math.random() * (cols.length + 2)) | 0],
        grav: 130,
      });
    }
  }

  /* ----------------------------- interpolation ----------------------------- */

  function sample() {
    if (!snaps.length) return null;
    const delay = Math.min(200, Math.max(50, snapGap * 1.25 + 15));
    let rt = performance.now() - delay;
    let s0 = snaps[0], s1 = snaps[snaps.length - 1];
    for (let i = snaps.length - 1; i > 0; i--) {
      if (snaps[i - 1].t <= rt) { s0 = snaps[i - 1]; s1 = snaps[i]; break; }
    }
    // buffer ran dry (no fresher snapshot yet): extrapolate briefly along the
    // last known velocity instead of freezing on s1.
    if (rt > s1.t) rt = Math.min(rt, s1.t + 120);
    const span = s1.t - s0.t;
    const a = span > 16 ? Math.max(0, (rt - s0.t) / span)
                        : Math.max(0, Math.min(1, span > 0 ? (rt - s0.t) / span : 1));
    if (s1.cyc) {
      const out = { margin: s1.margin, ents: [], cyc: true };
      for (const [pid, e1] of s1.e) {
        const e0 = (s0.cyc && s0.e.get(pid)) || e1;
        out.ents.push({
          pid,
          x: (e0[0] + (e1[0] - e0[0]) * a + 0.5) * cellW,
          y: (e0[1] + (e1[1] - e0[1]) * a + 0.5) * cellH,
          vx: e1[3], vy: e1[4],
          alive: e1[2] === 1, r: cellW * 0.8,
          boost: e1[5] || 0, boosting: e1[6] === 1,
        });
      }
      return out;
    }
    if (s1.ski) {
      const cam = s0.ski ? s0.cam + (s1.cam - s0.cam) * a : s1.cam;
      const out = { ski: true, cam, spd: s1.spd, ents: [] };
      for (const [pid, e1] of s1.e) {
        const e0 = (s0.ski && s0.e.get(pid)) || e1;
        const wx = e0[0] + (e1[0] - e0[0]) * a;
        const wy = e0[1] + (e1[1] - e0[1]) * a;
        out.ents.push({ pid, x: wx * S, y: (wy - cam) * S,
                        vx: e1[0] - e0[0], alive: e1[2] === 1,
                        cd: e1[3], tumble: e1[4], r: 6 });
      }
      return out;
    }
    if (s1.pln) {
      const out = { pln: true, ents: [] };
      for (const [pid, e1] of s1.e) {
        const e0 = (s0.pln && s0.e.get(pid)) || e1;
        let x0 = e0[0], y0 = e0[1];
        if (Math.abs(e1[0] - x0) > 480) x0 += e1[0] > x0 ? 960 : -960;  // wrap
        if (Math.abs(e1[1] - y0) > 270) y0 += e1[1] > y0 ? 540 : -540;
        const a0 = (e0[4] || 0) / 100, a1 = (e1[4] || 0) / 100;
        let da = a1 - a0;
        if (da > Math.PI) da -= 2 * Math.PI;
        else if (da < -Math.PI) da += 2 * Math.PI;
        out.ents.push({
          pid,
          x: (((x0 + (e1[0] - x0) * a) % 960) + 960) % 960 * S,
          y: (((y0 + (e1[1] - y0) * a) % 540) + 540) % 540 * S,
          ang: a0 + da * a, turn: da,
          alive: e1[2] === 1, cd: e1[3], hp: e1[5], inv: e1[6] === 1, r: 6,
        });
      }
      return out;
    }
    if (s1.bmp) {
      const out = { bmp: true, score: s1.score || [0, 0], ko: s1.ko, ents: [] };
      for (const [pid, e1] of s1.e) {
        const e0 = (s0.bmp && s0.e.get(pid)) || e1;
        out.ents.push({
          pid,
          x: (e0[0] + (e1[0] - e0[0]) * a) * S,
          y: (e0[1] + (e1[1] - e0[1]) * a) * S,
          vx: e1[0] - e0[0], vy: e1[1] - e0[1],
          alive: true, cd: e1[3], r: BUMPER_R * S,
        });
      }
      return out;
    }
    const out = { R: (s0.R + (s1.R - s0.R) * a) * S, wind: s1.wind, ents: [] };
    for (const [pid, e1] of s1.e) {
      const e0 = s0.e.get(pid) || e1;
      out.ents.push({
        pid,
        x: (e0[0] + (e1[0] - e0[0]) * a) * S,
        y: (e0[1] + (e1[1] - e0[1]) * a) * S,
        vx: e1[0] - e0[0], vy: e1[1] - e0[1],
        alive: e1[2] === 1, cd: e1[3], r: (e1[4] || 13) * S,
      });
    }
    return out;
  }

  /* -------------------------------- drawing -------------------------------- */

  function drawRing(R) {
    const cx = (arena.cx || 480) * S, cy = (arena.cy || 270) * S;
    // void shadow under the platform
    wctx.fillStyle = "rgba(0,0,0,.45)";
    wctx.beginPath();
    wctx.ellipse(cx + 3, cy + 5, R + 4, R + 4, 0, 0, Math.PI * 2);
    wctx.fill();
    // dirt floor
    wctx.fillStyle = floorPat;
    wctx.beginPath();
    wctx.arc(cx, cy, R, 0, Math.PI * 2);
    wctx.fill();
    // depth: darken toward the rim
    const g = wctx.createRadialGradient(cx, cy, R * 0.4, cx, cy, R);
    g.addColorStop(0, "rgba(255,240,200,.06)");
    g.addColorStop(1, "rgba(0,0,0,.34)");
    wctx.fillStyle = g;
    wctx.beginPath();
    wctx.arc(cx, cy, R, 0, Math.PI * 2);
    wctx.fill();
    // rim + highlight
    const danger = R < 46;
    wctx.lineWidth = 3;
    wctx.strokeStyle = danger
      ? `rgba(255,80,80,${0.6 + 0.4 * Math.sin(performance.now() / 120)})`
      : "#3d2f22";
    wctx.beginPath();
    wctx.arc(cx, cy, R - 1, 0, Math.PI * 2);
    wctx.stroke();
    wctx.lineWidth = 1;
    wctx.strokeStyle = "rgba(255,235,200,.25)";
    wctx.beginPath();
    wctx.arc(cx, cy, R - 3, Math.PI * 1.05, Math.PI * 1.55);
    wctx.stroke();
    // crumbling edge dust while shrinking
    if (snaps.length > 1 && snaps[snaps.length - 1].R < snaps[0].R && Math.random() < 0.35) {
      const a = Math.random() * Math.PI * 2;
      particles.push({
        x: cx + Math.cos(a) * R, y: cy + Math.sin(a) * R,
        vx: Math.cos(a) * 18, vy: Math.sin(a) * 18 + 22,
        life: 0.5, t: 0, size: 1, color: "#64503a", grav: 60,
      });
    }
  }

  function drawWind(wind) {
    if (!wind) return;
    const t = performance.now() / 1000;
    const cx = W / 2, cy = 26;
    const ang = Math.atan2(wind[1], wind[0]);
    wctx.save();
    wctx.translate(cx + Math.sin(t * 2) * 2, cy);
    wctx.rotate(ang);
    wctx.strokeStyle = "rgba(160,220,255,.7)";
    wctx.fillStyle = "rgba(160,220,255,.7)";
    wctx.lineWidth = 2;
    wctx.beginPath(); wctx.moveTo(-10, 0); wctx.lineTo(8, 0); wctx.stroke();
    wctx.beginPath(); wctx.moveTo(8, -4); wctx.lineTo(14, 0); wctx.lineTo(8, 4); wctx.fill();
    wctx.restore();
  }

  function drawPlayer(e) {
    const m = meta.get(e.pid) || { color: "#888", name: "?" };
    const { x, y, r } = e;
    // shadow
    wctx.fillStyle = "rgba(0,0,0,.35)";
    wctx.beginPath(); wctx.ellipse(x + 1, y + r * 0.75, r * 0.9, r * 0.45, 0, 0, Math.PI * 2); wctx.fill();
    // body
    wctx.fillStyle = m.color;
    wctx.beginPath(); wctx.arc(x, y, r, 0, Math.PI * 2); wctx.fill();
    wctx.lineWidth = 1.5;
    wctx.strokeStyle = shade(m.color, 0.55);
    wctx.stroke();
    // glint
    wctx.fillStyle = "rgba(255,255,255,.55)";
    wctx.fillRect(x - r * 0.45, y - r * 0.55, Math.max(1, r * 0.22), Math.max(1, r * 0.22));
    // eyes look along velocity
    const sp = Math.hypot(e.vx, e.vy);
    const dx = sp > 0.5 ? e.vx / sp : 0, dy = sp > 0.5 ? e.vy / sp : 0;
    const ex = x + dx * r * 0.35, ey = y + dy * r * 0.35 - r * 0.1;
    const eyeGap = Math.max(2, r * 0.36);
    const px = -dy, py = dx; // perpendicular
    wctx.fillStyle = "#12122a";
    const er = Math.max(1, r * 0.16);
    wctx.fillRect(ex + px * eyeGap / 2 - er / 2, ey + py * eyeGap / 2 - er / 2, er, er);
    wctx.fillRect(ex - px * eyeGap / 2 - er / 2, ey - py * eyeGap / 2 - er / 2, er, er);
    // dash charged: little glowing pip
    if (e.cd >= 1) {
      wctx.fillStyle = "rgba(255,255,255,.9)";
      wctx.fillRect(x - 1, y - r - 4, 2, 2);
    }
  }

  function roundRectPath(c, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    c.beginPath();
    c.moveTo(x + r, y);
    c.arcTo(x + w, y, x + w, y + h, r);
    c.arcTo(x + w, y + h, x, y + h, r);
    c.arcTo(x, y + h, x, y, r);
    c.arcTo(x, y, x + w, y, r);
    c.closePath();
  }
  function drawNameTags(ents) {
    const sx = cssW / W, sy = cssH / H;
    octx.textAlign = "center";
    const now = performance.now();

    for (const e of ents) {
      if (!e.alive) continue;
      const m = meta.get(e.pid);
      if (!m) continue;

      const x = e.x * sx;
      const y = (e.y - e.r) * sy - 12;   // moved up a bit for bigger text

      const isSelf = selfPid != null && e.pid === selfPid;

      if (isSelf) {
        // === YOU - BIG & flashy ===
        octx.font = "700 26px ui-monospace, Menlo, Consolas, monospace";

        // Strong white glow
        octx.shadowColor = "#ffffff";
        octx.shadowBlur = 18;
        octx.fillStyle = "#ffffff";
        octx.fillText(m.name, x, y);

        // Colored glow
        octx.shadowBlur = 10;
        octx.shadowColor = m.color;
        octx.fillStyle = m.color;
        octx.fillText(m.name, x, y);

        // Main crisp text
        octx.shadowBlur = 0;
        octx.fillStyle = "#ffffff";
        octx.fillText(m.name, x, y);

        // Down arrow
        octx.font = "700 14px ui-monospace, Menlo, Consolas, monospace";
        octx.fillStyle = "#fff";
        octx.fillText("▼", x, y + 22);

      } else {
        // === Other players / bots - Still bigger ===
        octx.font = "600 19px ui-monospace, Menlo, Consolas, monospace";

        // Black shadow + white halo
        octx.shadowColor = "#000000";
        octx.shadowBlur = 10;
        octx.fillStyle = "#000000";
        octx.fillText(m.name, x + 1.5, y + 1.5);

        octx.shadowBlur = 6;
        octx.shadowColor = "#ffffff";
        octx.fillStyle = "#ffffff";
        octx.fillText(m.name, x, y);

        // Main colored text
        octx.shadowBlur = 0;
        octx.fillStyle = m.color;
        octx.fillText(m.name, x, y);
      }
    }
  }

  function drawFallers(dt) {
    for (const f of fallers) {
      f.t += dt;
      f.x += f.vx * dt; f.y += f.vy * dt;
      f.vx *= 0.99; f.vy += 60 * dt;   // drift down into the void
      f.rot += f.vr * dt;
      const k = 1 - f.t / f.life;
      if (k <= 0) continue;
      wctx.save();
      wctx.translate(f.x, f.y);
      wctx.rotate(f.rot);
      wctx.globalAlpha = Math.min(1, k * 1.4);
      const r = f.r * (0.3 + 0.7 * k);
      wctx.fillStyle = f.color;
      wctx.beginPath(); wctx.arc(0, 0, r, 0, Math.PI * 2); wctx.fill();
      // X_X eyes
      wctx.strokeStyle = "#12122a";
      wctx.lineWidth = 1;
      for (const s of [-1, 1]) {
        wctx.beginPath();
        wctx.moveTo(s * r * 0.35 - 1.5, -1.5); wctx.lineTo(s * r * 0.35 + 1.5, 1.5);
        wctx.moveTo(s * r * 0.35 + 1.5, -1.5); wctx.lineTo(s * r * 0.35 - 1.5, 1.5);
        wctx.stroke();
      }
      wctx.restore();
      wctx.globalAlpha = 1;
    }
    fallers = fallers.filter((f) => f.t < f.life);
  }

  function drawParticles(dt) {
    for (const p of particles) {
      p.t += dt;
      p.x += p.vx * dt; p.y += p.vy * dt;
      p.vy += (p.grav || 0) * dt;
      const k = 1 - p.t / p.life;
      if (k <= 0) continue;
      wctx.globalAlpha = Math.min(1, k * 1.6);
      wctx.fillStyle = p.color;
      wctx.fillRect(p.x | 0, p.y | 0, p.size, p.size);
    }
    wctx.globalAlpha = 1;
    particles = particles.filter((p) => p.t < p.life);
  }

  function drawGhosts(dt) {
    for (const g of ghosts) {
      g.t += dt;
      const k = 1 - g.t / g.life;
      if (k <= 0) continue;
      wctx.globalAlpha = k * 0.4;
      wctx.fillStyle = g.color;
      wctx.beginPath(); wctx.arc(g.x, g.y, g.r * (0.8 + 0.4 * k), 0, Math.PI * 2); wctx.fill();
    }
    wctx.globalAlpha = 1;
    ghosts = ghosts.filter((g) => g.t < g.life);
  }

  function drawFlash() {
    if (!flashMsg) return;
    const dt = (performance.now() - flashMsg.t0) / 1000;
    if (dt > 0.9) { flashMsg = null; return; }
    const k = dt < 0.15 ? dt / 0.15 : 1 - (dt - 0.15) / 0.75;
    octx.save();
    octx.globalAlpha = Math.max(0, k);
    octx.font = `900 ${Math.round(cssH * 0.16)}px ui-monospace, Menlo, monospace`;
    octx.textAlign = "center";
    octx.fillStyle = "#000";
    octx.fillText(flashMsg.text, cssW / 2 + 4, cssH / 2 + 4);
    octx.fillStyle = "#ffd43b";
    octx.fillText(flashMsg.text, cssW / 2, cssH / 2);
    octx.restore();
  }

  function frame() {
    requestAnimationFrame(frame);
    const now = performance.now();
    const dt = Math.min(0.05, (now - lastFrame) / 1000);
    lastFrame = now;
    if (!arena) return;

    wctx.setTransform(1, 0, 0, 1, 0, 0);
    wctx.clearRect(0, 0, W, H);
    if (bg) wctx.drawImage(bg, 0, 0);

    // screenshake
    if (shake > 0.1) {
      wctx.setTransform(1, 0, 0, 1,
        (Math.random() - 0.5) * shake, (Math.random() - 0.5) * shake);
      shake *= Math.pow(0.001, dt);   // fast decay
    }

    const s = sample();
    octx.clearRect(0, 0, cssW, cssH);
    if (s) {
      if (s.cyc) {
        drawCycles(s);
      } else if (s.ski) {
        drawSki(s);
      } else if (s.pln) {
        drawPlanes(s);
      } else if (s.bmp) {
        drawBumper(s);
      } else {
        drawRing(s.R);
        drawWind(s.wind);
        drawGhosts(dt);
        const alive = s.ents.filter((e) => e.alive);
        alive.sort((a, b) => a.y - b.y);
        for (const e of alive) drawPlayer(e);
      }
      drawFallers(dt);
      drawParticles(dt);
      drawNameTags(s.ents);
      if (s.pln) drawHearts(s.ents);
      if (s.bmp) drawBumperScore(s.score);
    } else if (arena.g === "cycles") {
      drawCycles({ margin: 0, ents: [] });
      drawParticles(dt);
    } else if (arena.g === "ski") {
      if (skiSnowPat) drawSki({ cam: 0, spd: 0, ents: [] });
      drawParticles(dt);
    } else if (arena.g === "planes") {
      if (skyBg) drawPlanes({ ents: [] });
      drawParticles(dt);
    } else if (arena.g === "bumper") {
      drawBumperField();
      drawParticles(dt);
    } else {
      drawRing((arena.R0 || 232) * S);
    }
    drawFlash();
  }

  requestAnimationFrame(frame);

  function setSelf(pid) { selfPid = pid == null ? null : pid; }

  return { startRound, addSnapshot, resize, fx, flash, celebrate, setSelf };
})();
