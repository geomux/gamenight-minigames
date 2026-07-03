/* net.js — websocket plumbing: connect, auto-reconnect, dispatch.
   The URL is derived from location so it works on LAN (ws://) and through
   https tunnels (wss://) without any config. */
"use strict";

const Net = (() => {
  const URL = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  const handlers = {};
  let ws = null;
  let tries = 0;
  let joinPayload = null;   // remembered so reconnects re-join automatically
  let statusCb = () => {};

  function on(type, fn) { handlers[type] = fn; }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function join(name, pw, sess) {
    joinPayload = { t: "join", name, pw, sess: sess || null };
    send(joinPayload);
  }

  function forgetJoin() { joinPayload = null; }

  function connect() {
    ws = new WebSocket(URL);
    ws.onopen = () => {
      tries = 0;
      statusCb(true);
      if (joinPayload) send(joinPayload);   // seamless rejoin after a blip
    };
    ws.onmessage = (e) => {
      let m;
      try { m = JSON.parse(e.data); } catch { return; }
      const h = handlers[m.t];
      if (h) h(m);
    };
    ws.onclose = () => {
      statusCb(false);
      tries++;
      setTimeout(connect, Math.min(5000, 400 * Math.pow(1.7, tries)));
    };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }

  return { on, send, join, forgetJoin, connect, onStatus: (cb) => (statusCb = cb) };
})();
