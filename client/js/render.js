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

  /* ------------------------------ helpers ------------------------------ */

  function shade(hex, f) {
    const n = parseInt(hex.slice(1), 16);
    const r = Math.max(0, Math.min(255, ((n >> 16) & 255) * f));
    const g = Math.max(0, Math.min(255, ((n >> 8) & 255) * f));
    const b = Math.max(0, Math.min(255, (n & 255) * f));
    return `rgb(${r | 0},${g | 0},${b | 0})`;
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
      wctx.globalAlpha = 0.3;
      wctx.fillStyle = pm.color;
      wctx.fillRect(e.x - cellW, e.y - cellH, cellW * 2, cellH * 2);   // glow
      wctx.globalAlpha = 1;
      wctx.fillStyle = pm.color;
      wctx.fillRect(e.x - cellW / 2, e.y - cellH / 2, cellW, cellH);
      wctx.fillStyle = "#fff";
      wctx.fillRect(e.x - 1, e.y - 1, 2, 2);
    }
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
    if (a.g === "cycles") {
      cellW = W / a.gw;
      cellH = H / a.gh;
      trailCv = document.createElement("canvas");
      trailCv.width = W;
      trailCv.height = H;
      trailCtx = trailCv.getContext("2d");
      buildGridDots(a.gw, a.gh);
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
      for (const row of m.heads) e.set(row[0], row.slice(1)); // [x,y,alive,dx,dy]
      snaps.push({ t: now, margin: m.margin || 0, e, cyc: true });
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
    const delay = Math.min(260, Math.max(80, snapGap * 1.6));
    const rt = performance.now() - delay;
    let s0 = snaps[0], s1 = snaps[snaps.length - 1];
    for (let i = snaps.length - 1; i > 0; i--) {
      if (snaps[i - 1].t <= rt) { s0 = snaps[i - 1]; s1 = snaps[i]; break; }
    }
    const span = s1.t - s0.t;
    const a = span > 0 ? Math.min(1, Math.max(0, (rt - s0.t) / span)) : 1;
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

  function drawNameTags(ents) {
    const sx = cssW / W, sy = cssH / H;
    octx.font = "600 11px ui-monospace, Menlo, Consolas, monospace";
    octx.textAlign = "center";
    for (const e of ents) {
      if (!e.alive) continue;
      const m = meta.get(e.pid);
      if (!m) continue;
      const x = e.x * sx, y = (e.y - e.r) * sy - 7;
      octx.fillStyle = "rgba(0,0,0,.75)";
      octx.fillText(m.name, x + 1, y + 1);
      octx.fillStyle = m.color;
      octx.fillText(m.name, x, y);
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
      } else {
        drawRing(s.R);
        drawWind(s.wind);
        drawGhosts(dt);
        const alive = s.ents.filter((e) => e.alive);
        alive.sort((a, b) => a.y - b.y);
        for (const e of alive) drawPlayer(e);
        drawFallers(dt);
      }
      drawParticles(dt);
      drawNameTags(s.ents);
    } else if (arena.g === "cycles") {
      drawCycles({ margin: 0, ents: [] });
      drawParticles(dt);
    } else {
      drawRing((arena.R0 || 232) * S);
    }
    drawFlash();
  }

  requestAnimationFrame(frame);

  return { startRound, addSnapshot, resize, fx, flash, celebrate };
})();
