/* render_check.js — headless exercise of every game's draw path in render.js.
   Catches runtime errors (bad indexes, missing helpers) that syntax checks
   can't. Run:  node tools/render_check.js                                   */
"use strict";

const fs = require("fs");
const path = require("path");

let T = 1000;
const anyProxy = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : anyProxy),
  set: () => true,
  apply: () => anyProxy,
});
const el = () => ({ getContext: () => anyProxy, width: 480, height: 270, style: {},
                    classList: { toggle() {}, add() {}, remove() {} } });
global.document = { getElementById: el, createElement: el };
global.window = { addEventListener() {}, devicePixelRatio: 1 };
global.performance = { now: () => T };
let rafCb = null;
global.requestAnimationFrame = (cb) => { rafCb = cb; };

const src = fs.readFileSync(path.join(__dirname, "..", "client", "js", "render.js"), "utf8");
const Renderer = eval(src + "\nRenderer;");

const meta = new Map([[1, { name: "A", color: "#ff5a5a" }], [2, { name: "B", color: "#3bd6ff" }]]);
const frames = (n) => { for (let i = 0; i < n; i++) { T += 16; rafCb(); } };

Renderer.resize(960, 540);

// --- sumo ---
Renderer.startRound({ g: "sumo", w: 960, h: 540, cx: 480, cy: 270, R0: 232, wind: true, action: "DASH" }, [1, 2], meta);
Renderer.addSnapshot({ t: "s", g: "sumo", R: 230, wind: [1, 0], e: [[1, 300, 270, 1, 0.5, 13], [2, 660, 270, 1, 1, 13]] });
T += 66;
Renderer.addSnapshot({ t: "s", g: "sumo", R: 228, wind: [0.9, 0.1], e: [[1, 310, 270, 1, 0.6, 13], [2, 650, 270, 0, 1, 13]] });
Renderer.fx([["dash", 1], ["hit", 480, 270, 0.8], ["fall", 2, 650, 270, 90, -20]]);
Renderer.flash("GO!");
frames(30);
Renderer.celebrate(["#ff5a5a"]);
frames(30);
console.log("sumo draw path OK");

// --- cycles ---
Renderer.startRound({ g: "cycles", w: 960, h: 540, gw: 96, gh: 54, wrap: false }, [1, 2], meta);
// new 8-field heads: [pid,gx,gy,alive,boost01,dx,dy,boosting]
Renderer.addSnapshot({ t: "s", g: "cycles", margin: 0,
                       heads: [[1, 30, 27, 1, 1, 1, 0, 1], [2, 60, 27, 1, 0, -1, 0, 0]],
                       cells: [[30, 27, 1], [60, 27, 2]] });
T += 66;
Renderer.addSnapshot({ t: "s", g: "cycles", margin: 2,
                       heads: [[1, 31, 27, 1, 1, 1, 0, 1], [2, 59, 27, 0, 0, -1, 0, 0]],
                       cells: [[31, 27, 1], [59, 27, 2]] });
Renderer.fx([["die", 2, 59, 27], ["wall", 2], ["clear", 2], ["boost", 1]]);
frames(30);
// legacy 6-field heads (pre-boost servers) must still parse without crashing
Renderer.addSnapshot({ t: "s", g: "cycles", margin: 2, heads: [[1, 32, 27, 1, 1, 0]], cells: [[32, 27, 1]] });
frames(5);
console.log("cycles draw path OK");

// --- ski ---
Renderer.startRound({ g: "ski", w: 960, h: 540, action: "SNOWBALL" }, [1, 2], meta);
Renderer.addSnapshot({ t: "s", g: "ski", cam: 0, spd: 150,
                       e: [[1, 300, 200, 1, 1, 0], [2, 600, 220, 1, 0.4, 0]],
                       obs: [[1, 400, 700, 0], [2, 500, 820, 1]],
                       balls: [[1, 310, 260, 0, 380], [2, 400, 240, -50, 300]] });
T += 66;
Renderer.addSnapshot({ t: "s", g: "ski", cam: 12, spd: 152,
                       e: [[1, 305, 212, 1, 1, 0.8], [2, 600, 230, 0, 0.4, 0]],
                       obs: [[3, 200, 900, 0]],
                       balls: [[1, 316, 285, 0, 380], [3, 500, 250, 40, 320]] });  // id 2 gone, id 3 new
Renderer.fx([["throw", 1], ["bonk", 1, 305, 200], ["splat", 1, 2], ["wipe", 2, 600, 30]]);
frames(30);
console.log("ski draw path OK");

// --- planes ---
Renderer.startRound({ g: "planes", w: 960, h: 540, lives: 3, action: "FIRE",
                      islands: [[480, 270, 40], [200, 150, 32]],
                      gusts: [[700, 400, 55, 120]] }, [1, 2], meta);
Renderer.addSnapshot({ t: "s", g: "planes",
                       e: [[1, 100, 100, 1, 1, 0, 3, 0], [2, 900, 500, 1, 0.5, 314, 1, 1]],
                       b: [[1, 400, 300, 460, 0], [2, 410, 300, 460, 0]] });
T += 66;
Renderer.addSnapshot({ t: "s", g: "planes",
                       e: [[1, 950, 110, 1, 1, 620, 3, 0], [2, 20, 490, 0, 0.5, 300, 0, 0]],
                       b: [[1, 420, 300, 460, 0], [3, 100, 100, -300, 200]] });   // id 2 gone, id 3 new; wrap + angle-wrap interp exercised
Renderer.fx([["shoot", 1], ["hitp", 2, 20, 490], ["clash", 400, 300],
             ["thud", 1, 440, 270], ["puff", 452, 265], ["down", 2, 20, 490]]);
frames(30);
console.log("planes draw path OK");

// --- bumper ---
Renderer.startRound({ g: "bumper", w: 960, h: 540, teams: [[1], [2]], goalH: 180, action: "DASH" }, [1, 2], meta);
Renderer.addSnapshot({ t: "s", g: "bumper", score: [0, 0],
                       e: [[1, 300, 270, 1, 0.5], [2, 660, 270, 1, 1]],
                       ball: [480, 270, 120, -40] });
T += 66;
Renderer.addSnapshot({ t: "s", g: "bumper", score: [1, 0], ko: 1.2,
                       e: [[1, 350, 270, 1, 0.6], [2, 610, 270, 1, 1]],
                       ball: [500, 260, 90, -30] });
Renderer.fx([["dash", 1], ["hit", 480, 270, 0.6], ["goal", 0, 960, 270]]);
frames(30);
// no-action variant (dash setting off): e-rows still 5-wide, charge pinned 0
Renderer.startRound({ g: "bumper", w: 960, h: 540, teams: [[1], [2]], goalH: 180 }, [1, 2], meta);
Renderer.addSnapshot({ t: "s", g: "bumper", score: [0, 0],
                       e: [[1, 300, 270, 1, 0], [2, 660, 270, 1, 0]],
                       ball: [480, 270, 0, 0] });
frames(10);
console.log("bumper draw path OK");

console.log("RENDERER HEADLESS EXERCISE PASSED");
