/* sfx.js — tiny WebAudio synth. No audio files, no CDNs: every sound here is
   an oscillator or a noise burst with a short envelope, lo-fi/bleepy on
   purpose to match the pixel brand. Self-contained: main.js calls into the
   handful of functions below (init/fx/tick/go/win/click/toast/out/setMuted);
   this file never touches the DOM or the network itself. */
"use strict";

const Sfx = (() => {
  let ctx = null;
  let master = null;
  let noiseBuf = null;
  let muted = false;
  const lastPlay = Object.create(null);   // per fx-kind rate limiting

  function ensureCtx() {
    if (ctx) return ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
    master = ctx.createGain();
    master.gain.value = 0.32;                 // tasteful overall volume
    const lp = ctx.createBiquadFilter();       // soft top-end = lo-fi character
    lp.type = "lowpass";
    lp.frequency.value = 7200;
    master.connect(lp);
    lp.connect(ctx.destination);
    noiseBuf = buildNoise();
    return ctx;
  }

  function buildNoise() {
    const len = Math.max(1, Math.floor(ctx.sampleRate * 0.5));
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    return buf;
  }

  /* Must be called from a real user gesture (autoplay policy). Safe to call
     repeatedly — cheap no-op once the context is running. */
  function init() {
    const c = ensureCtx();
    if (c && c.state === "suspended") c.resume().catch(() => {});
  }

  function setMuted(v) { muted = !!v; }

  function rateOk(kind, minGapMs) {
    if (!minGapMs) return true;
    const now = performance.now();
    const last = lastPlay[kind];
    if (last !== undefined && now - last < minGapMs) return false;
    lastPlay[kind] = now;
    return true;
  }

  /* ------------------------------ voices ------------------------------ */

  function tone(freq, dur, opts = {}) {
    if (!ctx || muted) return;
    const { type = "square", gain = 0.2, glideTo = null, delay = 0 } = opts;
    const t0 = ctx.currentTime + delay;
    const osc = ctx.createOscillator();
    osc.type = type;
    osc.frequency.setValueAtTime(Math.max(1, freq), t0);
    if (glideTo) osc.frequency.exponentialRampToValueAtTime(Math.max(1, glideTo), t0 + dur);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t0 + Math.min(0.012, dur * 0.25));
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    osc.connect(g);
    g.connect(master);
    osc.start(t0);
    osc.stop(t0 + dur + 0.02);
  }

  function noise(dur, opts = {}) {
    if (!ctx || muted || !noiseBuf) return;
    const { gain = 0.22, filterFreq = null, filterType = "lowpass", delay = 0 } = opts;
    const t0 = ctx.currentTime + delay;
    const src = ctx.createBufferSource();
    src.buffer = noiseBuf;
    let node = src;
    if (filterFreq) {
      const f = ctx.createBiquadFilter();
      f.type = filterType;
      f.frequency.value = filterFreq;
      node.connect(f);
      node = f;
    }
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0002, gain), t0 + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    node.connect(g);
    g.connect(master);
    src.start(t0);
    src.stop(t0 + dur + 0.02);
  }

  /* --------------------------- fx event table --------------------------- */
  // Convention matches PROTOCOL.md's fx table. Unknown kinds (future
  // additions like ["boost", pid]) are ignored silently.

  const FX = {
    dash: () => tone(680, 0.09, { type: "square", gain: 0.18, glideTo: 1200 }),
    hit: (ev) => {
      const inten = Math.max(0, Math.min(1, ev[3] ?? 0.5));
      tone(110 + inten * 50, 0.09, { type: "square", gain: 0.16 + inten * 0.14 });
      noise(0.07, { gain: 0.12 + inten * 0.16, filterFreq: 1500 + inten * 1300 });
    },
    fall: () => tone(320, 0.35, { type: "sawtooth", gain: 0.16, glideTo: 55 }),
    die: () => {
      noise(0.16, { gain: 0.26, filterFreq: 1200 });
      tone(220, 0.22, { type: "square", gain: 0.2, glideTo: 50 });
    },
    wall: () => tone(85, 0.09, { type: "square", gain: 0.15 }),
    throw: () => tone(520, 0.07, { type: "triangle", gain: 0.15, glideTo: 900 }),
    bonk: () => {
      tone(140, 0.09, { type: "square", gain: 0.18 });
      noise(0.05, { gain: 0.14, filterFreq: 900 });
    },
    splat: () => noise(0.14, { gain: 0.24, filterFreq: 1500 }),
    wipe: () => noise(0.4, { gain: 0.28, filterFreq: 2200 }),
    shoot: () => tone(900, 0.045, { type: "square", gain: 0.11, glideTo: 400 }),
    hitp: () => {
      tone(180, 0.09, { type: "square", gain: 0.18 });
      noise(0.06, { gain: 0.15, filterFreq: 1600 });
    },
    clash: () => {
      tone(700, 0.06, { type: "triangle", gain: 0.16 });
      noise(0.05, { gain: 0.14, filterFreq: 3000 });
    },
    down: () => {
      noise(0.3, { gain: 0.3, filterFreq: 900 });
      tone(160, 0.3, { type: "sawtooth", gain: 0.2, glideTo: 40 });
    },
    thud: () => tone(110, 0.1, { type: "square", gain: 0.18 }),
    puff: () => noise(0.08, { gain: 0.13, filterFreq: 2500 }),
    boost: () => tone(500, 0.12, { type: "sawtooth", gain: 0.17, glideTo: 1400 }),
  };

  const RATE_LIMIT = {
    shoot: 80, hit: 45, dash: 70, die: 50, bonk: 90, splat: 90, wipe: 250,
    hitp: 90, clash: 90, down: 150, thud: 90, puff: 70, wall: 120, throw: 70,
    boost: 100,
  };

  function fx(events) {
    if (!ctx || muted || !events) return;
    for (const ev of events) {
      const kind = ev[0];
      const voice = FX[kind];
      if (!voice) continue;             // unknown/future kind: ignore silently
      if (!rateOk(kind, RATE_LIMIT[kind])) continue;
      voice(ev);
    }
  }

  /* --------------------------- local UI voices --------------------------- */

  function tick() { tone(700, 0.05, { type: "square", gain: 0.18 }); }

  function go() {
    tone(420, 0.05, { type: "square", gain: 0.24 });
    tone(880, 0.18, { type: "square", gain: 0.22, delay: 0.05 });
  }

  function win() {
    const notes = [523.25, 659.25, 783.99, 1046.5];
    notes.forEach((f, i) => tone(f, 0.18, { type: "square", gain: 0.18, delay: i * 0.09 }));
  }

  function click() { tone(1000, 0.03, { type: "square", gain: 0.12 }); }

  function toastBlip() { tone(760, 0.05, { type: "triangle", gain: 0.14 }); }

  function out() {
    tone(90, 0.22, { type: "sawtooth", gain: 0.22, glideTo: 35 });
    noise(0.15, { gain: 0.16, filterFreq: 500 });
  }

  return { init, fx, tick, go, win, click, toast: toastBlip, out, setMuted };
})();
