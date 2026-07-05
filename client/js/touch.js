/* touch.js — mobile control layer: a floating virtual joystick (left) and a
   big hold-to-fire action button (right), built with Pointer Events so a
   thumb on each side works simultaneously. Self-contained: it never touches
   `keys`/sendKeys directly — main.js passes an onKey(k, pressed) callback
   into init() and owns the actual wire protocol. Only ever shown to touch
   users (coarse pointer, or the first touch-type pointer we see). */
"use strict";

const Touch = (() => {
  const DEAD = 12;      // px of drag before a direction can engage
  const RADIUS = 44;    // px knob travel radius (visual clamp)
  const ON = 0.35;      // hysteresis: axis engages once its component passes this
  const OFF = 0.18;     // ...and disengages only once it drops below this

  let onKeyCb = null;
  let layer, leftZone, rightZone, stickEl, knobEl, btnEl;
  let stickPid = null, btnPid = null;
  let originX = 0, originY = 0;
  let actionOn = false;
  let revealed = false;
  const dirs = { u: false, d: false, l: false, r: false };

  function emit(k, v) { if (onKeyCb) onKeyCb(k, v); }

  function build() {
    const stage = document.getElementById("stage");
    if (!stage) return false;

    layer = document.createElement("div");
    layer.id = "touch-layer";

    leftZone = document.createElement("div");
    leftZone.className = "touch-zone touch-zone-l";

    rightZone = document.createElement("div");
    rightZone.className = "touch-zone touch-zone-r";

    stickEl = document.createElement("div");
    stickEl.id = "touch-stick";
    stickEl.innerHTML = '<div class="stick-base"></div><div class="stick-knob"></div>';
    knobEl = stickEl.querySelector(".stick-knob");

    btnEl = document.createElement("div");
    btnEl.id = "touch-btn";
    btnEl.textContent = "●";   // filled circle; generic action glyph

    rightZone.appendChild(btnEl);
    layer.append(leftZone, rightZone, stickEl);
    stage.appendChild(layer);
    layer.addEventListener("contextmenu", (e) => e.preventDefault());
    return true;
  }

  /* ------------------------------ joystick ------------------------------ */

  function showStickAt(clientX, clientY) {
    originX = clientX;
    originY = clientY;
    const r = layer.getBoundingClientRect();
    stickEl.style.left = (clientX - r.left) + "px";
    stickEl.style.top = (clientY - r.top) + "px";
    stickEl.classList.add("active");
    knobEl.style.transform = "translate(0px, 0px)";
  }

  function hideStick() { stickEl.classList.remove("active"); }

  function resetDirs() {
    for (const k of ["u", "d", "l", "r"]) {
      if (dirs[k]) { dirs[k] = false; emit(k, false); }
    }
  }

  function axisOn(cur, comp) { return cur ? comp > OFF : comp > ON; }

  function updateStick(clientX, clientY) {
    const dx = clientX - originX, dy = clientY - originY;
    const mag = Math.hypot(dx, dy);
    const clamped = Math.min(mag, RADIUS);
    knobEl.style.transform = mag > 0.001
      ? `translate(${(dx / mag) * clamped}px, ${(dy / mag) * clamped}px)`
      : "translate(0px, 0px)";

    if (mag < DEAD) { resetDirs(); return; }
    const nx = dx / mag, ny = dy / mag;
    const r = axisOn(dirs.r, nx), l = axisOn(dirs.l, -nx);
    const d = axisOn(dirs.d, ny), u = axisOn(dirs.u, -ny);
    if (r !== dirs.r) { dirs.r = r; emit("r", r); }
    if (l !== dirs.l) { dirs.l = l; emit("l", l); }
    if (d !== dirs.d) { dirs.d = d; emit("d", d); }
    if (u !== dirs.u) { dirs.u = u; emit("u", u); }
  }

  function wireLeft() {
    leftZone.addEventListener("pointerdown", (e) => {
      if (stickPid !== null) return;      // one finger drives the stick at a time
      stickPid = e.pointerId;
      e.currentTarget.setPointerCapture(stickPid);
      showStickAt(e.clientX, e.clientY);
      e.preventDefault();
    });
    leftZone.addEventListener("pointermove", (e) => {
      if (e.pointerId !== stickPid) return;
      updateStick(e.clientX, e.clientY);
      e.preventDefault();
    });
    const release = (e) => {
      if (e.pointerId !== stickPid) return;
      stickPid = null;
      hideStick();
      resetDirs();
    };
    leftZone.addEventListener("pointerup", release);
    leftZone.addEventListener("pointercancel", release);
  }

  /* ---------------------------- action button ---------------------------- */

  function setAction(v) {
    if (actionOn === v) return;
    actionOn = v;
    btnEl.classList.toggle("pressed", v);
    emit("a", v);
  }

  function wireRight() {
    rightZone.addEventListener("pointerdown", (e) => {
      if (btnPid !== null) return;
      btnPid = e.pointerId;
      e.currentTarget.setPointerCapture(btnPid);
      setAction(true);
      e.preventDefault();
    });
    const release = (e) => {
      if (e.pointerId !== btnPid) return;
      btnPid = null;
      setAction(false);
    };
    rightZone.addEventListener("pointerup", release);
    rightZone.addEventListener("pointercancel", release);
  }

  /* ------------------------------ visibility ------------------------------ */

  function reveal() {
    if (revealed || !layer) return;
    revealed = true;
    layer.classList.add("enabled");
  }

  function detect() {
    const coarse = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
    if (coarse) { reveal(); return; }
    const onFirstTouch = (e) => {
      if (e.pointerType === "touch") {
        reveal();
        window.removeEventListener("pointerdown", onFirstTouch);
      }
    };
    window.addEventListener("pointerdown", onFirstTouch, { passive: true });
  }

  function init(onKey) {
    onKeyCb = onKey;
    if (!build()) return;
    wireLeft();
    wireRight();
    detect();
  }

  function setActionLabel(text) {
    if (btnEl) btnEl.title = text || "";
  }

  return { init, setActionLabel };
})();
