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
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
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
  sizeStage();

  $("hud-round").textContent = `ROUND ${m.round}`;
  $("hud-game").textContent = m.game.name;
  const chips = settingChips(m.settings, App.room ? App.room.schema : []);
  $("hud-chips").textContent = "";
  $("hud-chips").appendChild(chipEls(chips, "mod"));
  $("hud-controls").textContent = m.game.controls;
  $("hud-controls").style.opacity = 1;
  $("results").classList.add("hidden");
  $("badge-out").classList.add("hidden");

  const inRoster = App.you && m.roster.includes(App.you.id);
  $("badge-spec").classList.toggle("hidden", !!inRoster);

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
    clearInterval(App.introTimer);
    App.introTimer = setInterval(() => {
      left--;
      if (left > 0) $("intro-count").textContent = left;
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
  sendKeys(true);
  setTimeout(() => { $("hud-controls").style.opacity = 0; }, 4000);
}

function onEnd(m) {
  App.phase = "results";
  const totals = new Map(m.totals.map(([pid, wins, pts]) => [pid, { wins, pts }]));
  const winners = m.winner.map((pid) => (App.players.get(pid) || {}).name || "?");
  $("res-winner").textContent = winners.join(" & ") || "NOBODY";
  const body = $("res-table");
  body.textContent = "";
  for (const [pid, place, pts] of m.placements) {
    const p = App.players.get(pid) || { name: "?", color: "#888" };
    const tr = document.createElement("tr");
    const c0 = document.createElement("td");
    c0.textContent = "#" + place;
    c0.style.color = place === 1 ? "var(--gold)" : "var(--dim)";
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
  if (document.activeElement && /INPUT|SELECT/.test(document.activeElement.tagName)) return;
  const k = KEYMAP[e.code];
  if (!k) return;
  e.preventDefault();
  if (!keys[k]) { keys[k] = true; sendKeys(); }
});
window.addEventListener("keyup", (e) => {
  const k = KEYMAP[e.code];
  if (!k) return;
  if (keys[k]) { keys[k] = false; sendKeys(); }
});
setInterval(() => { if (App.phase === "playing") sendKeys(); }, 1000);  // self-heal

/* ============================ stage sizing ============================ */

function sizeStage() {
  const stage = $("stage");
  const availW = window.innerWidth;
  const availH = window.innerHeight - 70;
  const scale = Math.min(availW / 960, availH / 540);
  const w = Math.floor(960 * scale), h = Math.floor(540 * scale);
  stage.style.width = w + "px";
  stage.style.height = h + "px";
  $("world").style.width = w + "px";
  $("world").style.height = h + "px";
  Renderer.resize(w, h);
}
window.addEventListener("resize", () => { if (App.phase !== "join") sizeStage(); });

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
    showScreen("lobby");
  }
  if (App.phase !== "join") renderLobby();
});

Net.on("round", (m) => {
  startRound(m);
  if (m.preview) Renderer.addSnapshot(m.preview);
});
Net.on("go", onGo);

Net.on("s", (m) => {
  App.latestSnap = m;
  Renderer.addSnapshot(m);
  const rows = m.e || m.heads || [];   // both games: [pid, x, y, alive, ...]
  const alive = rows.filter((e) => e[3]).length;
  $("hud-alive").textContent = `${alive} ALIVE`;
  if (App.you) {
    const mine = rows.find((e) => e[0] === App.you.id);
    $("badge-out").classList.toggle("hidden", !(mine && !mine[3] && App.phase === "playing"));
  }
});

Net.on("fx", (m) => Renderer.fx(m.ev));
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
  $("hud-ping").textContent = Math.round(performance.now() - m.ts) + "ms";
});
setInterval(() => Net.send({ t: "ping", ts: performance.now() }), 2000);

Net.onStatus((up) => $("reconnect").classList.toggle("hidden", up));

/* join button */
function doJoin() {
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
$("game-select").onchange = (e) => Net.send({ t: "host", a: "set_game", g: e.target.value });

Net.connect();
