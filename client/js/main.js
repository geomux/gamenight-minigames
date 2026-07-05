/* main.js — screens, lobby UI, input capture, HUD. Renderer lives in render.js. */
"use strict";

const $ = (id) => document.getElementById(id);

const App = {
  you: null,             // {id, name, host}
  players: new Map(),    // pid -> row from the room msg
  room: null,            // latest room msg
  round: null,           // latest round msg
  phase: "join",         // join | lobby | countdown | playing | results
  latestSnap: null,
  needPw: false,
  resTimer: null,
  introTimer: null,
};

const store = {
  key: (k) => `gn_${location.host}_${k}`,
  get: (k) => localStorage.getItem(store.key(k)) || "",
  set: (k, v) => localStorage.setItem(store.key(k), v),
};

/* restart a CSS animation on demand by toggling its class off and back on */
function bump(el, cls) {
  el.classList.remove(cls);
  void el.offsetWidth;   // force reflow so the browser notices the class left
  el.classList.add(cls);
}

/* ============================== screens ============================== */

function showScreen(name) {
  for (const s of ["scr-join", "scr-lobby", "scr-game"]) {
    $(s).classList.toggle("hidden", s !== "scr-" + name);
  }
}

function applyHostUI() {
  const isHost = !!(App.you && App.you.host);
  document.querySelectorAll(".hostonly").forEach((el) => el.classList.toggle("hidden", !isHost));
  $("lobby-wait").classList.toggle("hidden", isHost);
  $("game-select").disabled = !isHost;
}

/* =============================== toasts =============================== */

function toast(msg, ms = 3500) {
  Sfx.toast();
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("toast-out");
    el.addEventListener("animationend", () => el.remove(), { once: true });
  }, ms);
}

/* ================================ lobby ================================ */

function renderLobby() {
  const room = App.room;
  if (!room) return;

  // player chips
  const list = $("lobby-players");
  list.textContent = "";
  const players = room.players;
  $("lobby-count").textContent = `${players.filter((p) => !p.bot).length}/${room.maxP}`;
  const connCount = players.filter((p) => p.conn).length;
  $("btn-start").classList.toggle("pulse-ready", connCount >= 2);
  for (const p of players) {
    const chip = document.createElement("div");
    chip.className = "pchip" + (p.conn ? "" : " off");
    chip.style.borderLeftColor = p.color;
    const dot = document.createElement("span");
    dot.className = "pdot";
    dot.style.background = p.color;
    dot.style.color = p.color;
    const name = document.createElement("span");
    name.className = "pname";
    name.textContent = p.name + (App.you && p.id === App.you.id ? " (you)" : "");
    const tag = document.createElement("span");
    tag.className = "ptag";
    tag.textContent = p.bot ? "BOT" : p.host ? "HOST" : p.conn ? "" : "OFFLINE";
    chip.append(dot, name, tag);
    list.appendChild(chip);
  }

  // scoreboard
  const sb = $("lobby-score");
  sb.textContent = "";
  const ranked = [...players].sort((a, b) => b.wins - a.wins || b.pts - a.pts);
  for (const p of ranked) {
    const tr = document.createElement("tr");
    const c1 = document.createElement("td");
    c1.textContent = p.name;
    c1.style.color = p.color;
    const c2 = document.createElement("td");
    c2.textContent = `${p.wins} win${p.wins === 1 ? "" : "s"}`;
    const c3 = document.createElement("td");
    c3.textContent = `${p.pts} pts`;
    c3.className = "dim";
    tr.append(c1, c2, c3);
    sb.appendChild(tr);
  }

  // game picker
  const sel = $("game-select");
  sel.textContent = "";
  for (const g of room.games) {
    const opt = document.createElement("option");
    opt.value = g.id;
    opt.textContent = g.name;
    if (g.id === room.gameId) opt.selected = true;
    sel.appendChild(opt);
  }
  const game = room.games.find((g) => g.id === room.gameId);
  $("game-tag").textContent = game ? game.tag : "";

  renderSettingsForm();
  applyHostUI();
}

function renderSettingsForm() {
  const room = App.room;
  const form = $("settings-form");
  const isHost = !!(App.you && App.you.host);
  form.textContent = "";
  for (const s of room.schema) {
    const row = document.createElement("div");
    row.className = "setrow" + (room.settings[s.k] !== s.def ? " changed" : "");
    const label = document.createElement("label");
    label.textContent = s.label;
    row.appendChild(label);

    let ctl;
    if (s.type === "bool") {
      ctl = document.createElement("input");
      ctl.type = "checkbox";
      ctl.checked = !!room.settings[s.k];
      ctl.onchange = () => Net.send({ t: "host", a: "set", k: s.k, v: ctl.checked });
    } else {
      // ints get a select of the whole range; choices get their list
      ctl = document.createElement("select");
      const opts = s.type === "int"
        ? Array.from({ length: s.max - s.min + 1 }, (_, i) => s.min + i)
        : s.choices;
      for (const o of opts) {
        const opt = document.createElement("option");
        opt.value = String(o);
        opt.textContent = String(o);
        if (String(o) === String(room.settings[s.k])) opt.selected = true;
        ctl.appendChild(opt);
      }
      ctl.onchange = () => {
        const raw = ctl.value;
        const v = s.type === "int" || typeof s.def === "number" ? Number(raw) : raw;
        Net.send({ t: "host", a: "set", k: s.k, v });
      };
    }
    ctl.disabled = !isHost;
    row.appendChild(ctl);
    form.appendChild(row);
  }
}

/* Settings chips shown in intro + HUD: only what differs from defaults. */
function settingChips(settings, schema) {
  const chips = [];
  for (const s of schema || []) {
    const v = settings[s.k];
    if (v === s.def || s.k === "bots" || s.k === "bot_skill") continue;
    chips.push(s.type === "bool" ? s.label : `${s.label}: ${v}`);
  }
  const bots = settings.bots;
  if (bots > 0) chips.push(`${bots} bot${bots === 1 ? "" : "s"} (${settings.bot_skill})`);
  return chips;
}

function chipEls(chips, cls) {
  const frag = document.createDocumentFragment();
  for (const c of chips) {
    const el = document.createElement("span");
    el.className = "chip " + cls;
    el.textContent = c;
    frag.appendChild(el);
  }
  return frag;
}

/* ================================ round ================================ */

function startRound(m) {
  App.round = m;
  App.phase = m.phase;
  App.latestSnap = null;
  showScreen("game");
  Renderer.startRound(m.arena, m.roster, App.players);

  $("hud-round").textContent = `ROUND ${m.round}`;
  $("hud-game").textContent = m.game.name;
  const chips = settingChips(m.settings, App.room ? App.room.schema : []);
  $("hud-chips").textContent = "";
  $("hud-chips").appendChild(chipEls(chips, "mod"));
  $("hud-controls").textContent = m.game.controls;
  $("hud-controls").style.opacity = 1;
  $("results").classList.add("hidden");
  $("badge-out").classList.add("hidden");
  sizeStage();   // after HUD text is set, so its measured height is accurate

  const inRoster = App.you && m.roster.includes(App.you.id);
  $("badge-spec").classList.toggle("hidden", !!inRoster);

  // big action-charge meter (DASH / SNOWBALL / FIRE), only when this game
  // has an action and I'm actually playing
  App.hasAction = !!(m.arena && m.arena.action && inRoster);
  $("charge-label").textContent = (m.arena && m.arena.action) || "";
  $("charge-meter").classList.toggle("hidden", !App.hasAction);
  $("charge-meter").classList.remove("ready");
  Touch.setActionLabel((m.arena && m.arena.action) || "");

  // intro overlay with countdown
  if (m.phase === "countdown") {
    $("intro").classList.remove("hidden");
    $("intro-roundno").textContent = `ROUND ${m.round}`;
    $("intro-name").textContent = m.game.name;
    $("intro-tag").textContent = m.game.tag;
    $("intro-controls").textContent = m.game.controls;
    $("intro-chips").textContent = "";
    $("intro-chips").appendChild(chipEls(chips, "mod"));
    let left = Math.ceil(m.secs ?? 3);
    $("intro-count").textContent = left;
    bump($("intro-count"), "pop");
    Sfx.tick();
    clearInterval(App.introTimer);
    App.introTimer = setInterval(() => {
      left--;
      if (left > 0) {
        $("intro-count").textContent = left;
        bump($("intro-count"), "pop");
        Sfx.tick();
      }
    }, 1000);
  } else {
    $("intro").classList.add("hidden");
  }
}

function onGo() {
  App.phase = "playing";
  clearInterval(App.introTimer);
  $("intro").classList.add("hidden");
  Renderer.flash("GO!");
  Sfx.go();
  sendKeys(true);
  setTimeout(() => { $("hud-controls").style.opacity = 0; }, 4000);
}

function onEnd(m) {
  App.phase = "results";
  $("charge-meter").classList.add("hidden");
  setPaused(false);
  const totals = new Map(m.totals.map(([pid, wins, pts]) => [pid, { wins, pts }]));
  const winners = m.winner.map((pid) => (App.players.get(pid) || {}).name || "?");
  $("res-winner").textContent = winners.join(" & ") || "NOBODY";
  const winnerColor = m.winner.length === 1 ? (App.players.get(m.winner[0]) || {}).color : null;
  $("res-winner").style.color = winnerColor || "";   // "" restores the CSS gold default
  const body = $("res-table");
  body.textContent = "";
  const MEDAL = { 1: "🥇", 2: "🥈", 3: "🥉" };
  const MEDAL_COLOR = { 1: "var(--gold)", 2: "#cfd8e3", 3: "#d8975a" };
  for (const [pid, place, pts] of m.placements) {
    const p = App.players.get(pid) || { name: "?", color: "#888" };
    const tr = document.createElement("tr");
    const c0 = document.createElement("td");
    c0.textContent = MEDAL[place] || "#" + place;
    c0.style.color = MEDAL_COLOR[place] || "var(--dim)";
    const c1 = document.createElement("td");
    c1.textContent = p.name;
    c1.style.color = p.color;
    const c2 = document.createElement("td");
    c2.textContent = "+" + pts;
    const t = totals.get(pid) || { wins: 0, pts: 0 };
    const c3 = document.createElement("td");
    c3.textContent = `${t.wins}W · ${t.pts}p`;
    c3.className = "dim";
    tr.append(c0, c1, c2, c3);
    body.appendChild(tr);
  }
  $("results").classList.remove("hidden");
  Renderer.celebrate(m.winner.map((pid) => (App.players.get(pid) || {}).color || "#ffd43b"));
  Sfx.win();

  let left = m.auto || 14;
  clearInterval(App.resTimer);
  $("res-auto").textContent = `lobby in ${left}s`;
  App.resTimer = setInterval(() => {
    left--;
    $("res-auto").textContent = left > 0 ? `lobby in ${left}s` : "";
    if (left <= 0) clearInterval(App.resTimer);
  }, 1000);
  applyHostUI();
}

/* ================================ input ================================ */

const keys = { u: false, d: false, l: false, r: false, a: false };
const KEYMAP = {
  KeyW: "u", ArrowUp: "u", KeyS: "d", ArrowDown: "d",
  KeyA: "l", ArrowLeft: "l", KeyD: "r", ArrowRight: "r", Space: "a",
};

function sendKeys(force) {
  Net.send({ t: "input", k: keys });
}

window.addEventListener("keydown", (e) => {
  if (e.code === "Escape") {
    // host pause menu: Esc freezes the round for everyone, Esc again resumes
    if (App.you && App.you.host && (App.phase === "playing" || App.phase === "countdown")) {
      e.preventDefault();
      Net.send({ t: "host", a: "pause" });
    }
    return;
  }
  if (document.activeElement && /INPUT|SELECT/.test(document.activeElement.tagName)) return;
  const k = KEYMAP[e.code];
  if (!k) return;
  e.preventDefault();          // game keys never scroll/type, even on repeat
  if (e.repeat) return;        // ignore OS key-repeat; state is already latched
  if (!keys[k]) { keys[k] = true; sendKeys(); }
});
window.addEventListener("keyup", (e) => {
  const k = KEYMAP[e.code];
  if (!k) return;
  if (keys[k]) { keys[k] = false; sendKeys(); }
});
setInterval(() => { if (App.phase === "playing") sendKeys(); }, 1000);  // self-heal

/* stuck-keys guard: alt-tab / app-switch mid-round must never leave a key
   latched forever — zero everything and push the cleared state right away. */
function clearAllKeys() {
  keys.u = keys.d = keys.l = keys.r = keys.a = false;
  sendKeys();
}
window.addEventListener("blur", clearAllKeys);
document.addEventListener("visibilitychange", () => { if (document.hidden) clearAllKeys(); });

/* mobile touch layer drives the same key state the keyboard does */
Touch.init((k, v) => { if (keys[k] !== v) { keys[k] = v; sendKeys(); } });

/* ============================ stage sizing ============================ */

function viewportSize() {
  // visualViewport tracks the actually-visible area on mobile (browser
  // chrome, keyboard) far more reliably than innerWidth/Height.
  const vv = window.visualViewport;
  return vv ? { w: vv.width, h: vv.height } : { w: window.innerWidth, h: window.innerHeight };
}

function cssVarPx(name) {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
}

function sizeStage() {
  const stage = $("stage");
  const { w: vw, h: vh } = viewportSize();
  const hudH = $("hud").offsetHeight || 34;           // measured, not guessed
  const sab = cssVarPx("--sab"), sal = cssVarPx("--sal"), sar = cssVarPx("--sar");
  const topGap = hudH + 4;                            // #scr-game is flex-start now,
  const availW = Math.max(100, vw - sal - sar);        // so this exactly clears the HUD
  const availH = Math.max(100, vh - topGap - sab - 6);
  const scale = Math.min(availW / 960, availH / 540);
  const w = Math.floor(960 * scale), h = Math.floor(540 * scale);
  stage.style.width = w + "px";
  stage.style.height = h + "px";
  stage.style.marginTop = topGap + "px";
  $("world").style.width = w + "px";
  $("world").style.height = h + "px";
  Renderer.resize(w, h);
}
window.addEventListener("resize", () => { if (App.phase !== "join") sizeStage(); });
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => { if (App.phase !== "join") sizeStage(); });
}

/* =============================== wiring =============================== */

Net.on("hello", (m) => {
  App.needPw = m.needPw;
  $("pw-hint").textContent = m.needPw
    ? "a password is required to join — the host has it"
    : "friends leave this empty · host types the host password";
  if (!$("in-name").value) $("in-name").value = store.get("name");
  if (!$("in-pw").value) $("in-pw").value = store.get("pw");
});

Net.on("joined", (m) => {
  App.you = m.you;
  store.set("sess", m.sess);
  store.set("name", m.you.name);
  $("join-err").textContent = "";
  App.phase = "lobby";
  showScreen("lobby");
  applyHostUI();
});

Net.on("err", (m) => {
  if (App.phase === "join") $("join-err").textContent = m.msg;
  else toast(m.msg);
});

Net.on("room", (m) => {
  App.room = m;
  App.players = new Map(m.players.map((p) => [p.id, p]));
  if (App.you) {
    const me = App.players.get(App.you.id);
    if (me) App.you.host = me.host;
  }
  if (m.state === "lobby" && App.phase !== "join" && App.phase !== "lobby") {
    App.phase = "lobby";
    clearInterval(App.resTimer);
    setPaused(false);
    showScreen("lobby");
  }
  if (App.phase !== "join") renderLobby();
});

Net.on("round", (m) => {
  startRound(m);
  if (m.preview) Renderer.addSnapshot(m.preview);
  setPaused(!!m.paused);
});
Net.on("go", onGo);

function setPaused(on) {
  App.paused = on;
  $("pause").classList.toggle("hidden", !on);
  $("pause-sub").textContent =
    on && !(App.you && App.you.host) ? "the host paused the round" : "";
  if (on) applyHostUI();
}
Net.on("pause", (m) => setPaused(m.on));

Net.on("s", (m) => {
  App.latestSnap = m;
  Renderer.addSnapshot(m);
  const rows = m.e || m.heads || [];   // all games: [pid, x, y, alive, charge?, …]
  const alive = rows.filter((e) => e[3]).length;
  $("hud-alive").textContent = `${alive} ALIVE`;
  if (App.you) {
    const mine = rows.find((e) => e[0] === App.you.id);
    const isOut = !!(mine && !mine[3] && App.phase === "playing");
    const wasOut = !$("badge-out").classList.contains("hidden");
    $("badge-out").classList.toggle("hidden", !isOut);
    if (isOut && !wasOut) { Sfx.out(); bump($("badge-out"), "shake-in"); }
    if (App.hasAction) {
      const dead = !mine || !mine[3];
      $("charge-meter").classList.toggle("hidden", dead || App.phase === "results");
      if (!dead) {
        const charge = Math.max(0, Math.min(1, mine[4] ?? 0));
        $("charge-fill").style.height = Math.round(charge * 100) + "%";
        const nowReady = charge >= 1;
        const wasReady = $("charge-meter").classList.contains("ready");
        $("charge-meter").classList.toggle("ready", nowReady);
        if (nowReady && !wasReady) bump($("charge-meter"), "punch");
      }
    }
  }
});

Net.on("fx", (m) => { Renderer.fx(m.ev); Sfx.fx(m.ev); });
Net.on("end", onEnd);
Net.on("toast", (m) => toast(m.msg));

Net.on("kicked", (m) => {
  Net.forgetJoin();
  store.set("sess", "");
  App.phase = "join";
  showScreen("join");
  $("join-err").textContent = m.msg;
});

let pingT0 = 0;
Net.on("pong", (m) => {
  const ms = Math.round(performance.now() - m.ts);
  $("hud-ping").textContent = ms + "ms";
  const dot = $("hud-ping-dot");
  dot.classList.remove("good", "okay", "bad");
  dot.classList.add(ms < 60 ? "good" : ms < 120 ? "okay" : "bad");
});
setInterval(() => Net.send({ t: "ping", ts: performance.now() }), 2000);

Net.onStatus((up) => $("reconnect").classList.toggle("hidden", up));

/* join button */
function doJoin() {
  Sfx.init();   // unlock/resume AudioContext on this first real user gesture
  const name = $("in-name").value.trim();
  if (!name) { $("join-err").textContent = "pick a name first"; return; }
  const pw = $("in-pw").value;
  store.set("name", name);
  store.set("pw", pw);
  Net.join(name, pw, store.get("sess"));
}
$("btn-join").onclick = doJoin;
$("in-name").addEventListener("keydown", (e) => { if (e.key === "Enter") doJoin(); });
$("in-pw").addEventListener("keydown", (e) => { if (e.key === "Enter") doJoin(); });

/* host controls */
$("btn-start").onclick = () => Net.send({ t: "host", a: "start" });
$("btn-again").onclick = () => Net.send({ t: "host", a: "start" });
$("btn-lobby").onclick = () => Net.send({ t: "host", a: "lobby" });
$("btn-reset-scores").onclick = () => Net.send({ t: "host", a: "reset_scores" });
$("btn-resume").onclick = () => Net.send({ t: "host", a: "pause", v: false });
$("btn-endround").onclick = () => Net.send({ t: "host", a: "abort" });
$("game-select").onchange = (e) => Net.send({ t: "host", a: "set_game", g: e.target.value });

/* sound: a tiny UI-click blip on any button, plus the mute toggle */
document.addEventListener("click", (e) => {
  if (e.target instanceof Element && e.target.closest(".btn, .mute-btn")) {
    Sfx.init();       // any button click is a valid gesture — also revives iOS audio
    Sfx.click();
  }
});
/* iOS suspends the AudioContext on app-switch; any tap after returning
   (pointerup = touchend, a valid activation gesture everywhere) revives it. */
window.addEventListener("pointerup", () => Sfx.init(), { passive: true });
let muted = store.get("mute") === "1";
function updateMuteBtn() {
  $("btn-mute").textContent = muted ? "🔇" : "🔊";
  $("btn-mute").classList.toggle("muted", muted);
}
Sfx.setMuted(muted);
updateMuteBtn();
$("btn-mute").onclick = () => {
  Sfx.init();
  muted = !muted;
  store.set("mute", muted ? "1" : "0");
  Sfx.setMuted(muted);
  updateMuteBtn();
};

Net.connect();
