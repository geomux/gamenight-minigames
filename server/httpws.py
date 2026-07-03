"""
httpws.py — zero-dependency HTTP static file server + WebSocket (RFC 6455).

One asyncio server on one port:
  * GET /...              -> static files served from a root directory
  * GET /ws  (+ Upgrade)  -> WebSocket session handed to the application

Deliberately minimal: no TLS (put cloudflared/ngrok in front for internet
play), no ws extensions (permessage-deflate is not negotiated), text frames
only for app data. ~250 lines, all stdlib, so the game needs zero pip installs.
"""

import asyncio
import base64
import hashlib
from pathlib import Path
from urllib.parse import unquote, urlsplit

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_HEAD = 16 * 1024      # max HTTP request head
MAX_MSG = 16 * 1024       # max inbound ws message payload (bytes)
MAX_QUEUE = 128           # reliable outbox depth before we drop the client

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


class WSConn:
    """One websocket client.

    All outbound traffic funnels through a single writer task so frames never
    interleave. State snapshots are *coalesced*: only the newest unsent
    snapshot is kept, so one slow client (hotel wifi) never stalls the tick
    loop or builds an unbounded backlog — it just skips frames.
    """

    _next_id = 0

    def __init__(self, reader, writer, on_message, on_close):
        WSConn._next_id += 1
        self.id = WSConn._next_id
        self._r = reader
        self._w = writer
        self._on_message = on_message
        self._on_close = on_close
        self._reliable = []        # queued text payloads (join acks, lobby, events)
        self._control = []         # queued raw control frames (pong/ping)
        self._state = None         # newest unsent state snapshot (text) — coalesced
        self._close_frame = None   # pending close, sent after queues drain
        self._wake = asyncio.Event()
        self.closing = False
        self.closed = False
        self.last_rx = asyncio.get_event_loop().time()
        self.player = None         # set by the room
        try:
            peer = writer.get_extra_info("peername")
            self.remote = f"{peer[0]}:{peer[1]}" if peer else "?"
        except Exception:
            self.remote = "?"

    # ---- public send API (sync enqueue; a writer task does the I/O) ----

    def send_text(self, text: str):
        if self.closing or self.closed:
            return
        self._reliable.append(text)
        if len(self._reliable) > MAX_QUEUE:  # hopeless client
            self.close()
            return
        self._wake.set()

    def send_state(self, text: str):
        if self.closing or self.closed:
            return
        self._state = text  # overwrite any unsent snapshot
        self._wake.set()

    def ping(self):
        if self.closing or self.closed:
            return
        self._control.append(self._frame(0x9, b""))
        self._wake.set()

    def close(self, code: int = 1000):
        if self.closing or self.closed:
            return
        self.closing = True
        self._close_frame = self._frame(0x8, code.to_bytes(2, "big"))
        self._wake.set()

    # ---- frame building ----

    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        head = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head.append(n)
        elif n < 65536:
            head.append(126)
            head += n.to_bytes(2, "big")
        else:
            head.append(127)
            head += n.to_bytes(8, "big")
        return bytes(head) + payload

    # ---- writer task ----

    async def _writer_loop(self):
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while self._control:
                    self._w.write(self._control.pop(0))
                while self._reliable:
                    self._w.write(self._frame(0x1, self._reliable.pop(0).encode()))
                if self._state is not None:
                    s, self._state = self._state, None
                    self._w.write(self._frame(0x1, s.encode()))
                if self._close_frame is not None:   # everything flushed -> close
                    self._w.write(self._close_frame)
                    await self._w.drain()
                    return
                await self._w.drain()
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass

    # ---- reader loop ----

    async def _read_frame(self):
        h = await self._r.readexactly(2)
        fin = h[0] & 0x80
        if h[0] & 0x70:  # RSV bits set but no extension negotiated
            raise ConnectionError("bad rsv")
        opcode = h[0] & 0x0F
        masked = h[1] & 0x80
        n = h[1] & 0x7F
        if n == 126:
            n = int.from_bytes(await self._r.readexactly(2), "big")
        elif n == 127:
            n = int.from_bytes(await self._r.readexactly(8), "big")
        if n > MAX_MSG:
            raise ConnectionError("message too large")
        if not masked:  # clients MUST mask (RFC 6455 §5.1)
            raise ConnectionError("unmasked client frame")
        key = await self._r.readexactly(4)
        raw = await self._r.readexactly(n) if n else b""
        payload = bytes(b ^ key[i & 3] for i, b in enumerate(raw))
        return fin, opcode, payload

    async def run(self):
        """Read until the connection dies; writer runs alongside."""
        writer_task = asyncio.create_task(self._writer_loop())
        buf = bytearray()
        try:
            while True:
                fin, op, payload = await self._read_frame()
                self.last_rx = asyncio.get_event_loop().time()
                if op == 0x9:                      # ping -> pong
                    self._control.append(self._frame(0xA, payload))
                    self._wake.set()
                elif op == 0xA:                    # pong -> keepalive only
                    pass
                elif op == 0x8:                    # close
                    self.close()
                    break
                elif op in (0x1, 0x0):             # text / continuation
                    buf += payload
                    if len(buf) > MAX_MSG:
                        raise ConnectionError("message too large")
                    if fin:
                        try:
                            text = buf.decode()
                        except UnicodeDecodeError:
                            raise ConnectionError("bad utf8")
                        buf = bytearray()
                        self._on_message(self, text)
                elif op == 0x2:                    # binary unused -> ignore
                    pass
                else:
                    raise ConnectionError("bad opcode")
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            self.closed = True
            self._wake.set()
            writer_task.cancel()
            try:
                self._w.close()
            except Exception:
                pass
            cb, self._on_close = self._on_close, None
            if cb:
                cb(self)


# ---------------------------------------------------------------------------


def _http_response(status: str, body: bytes, ctype: str = "text/plain") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


class Server:
    """Static files + websocket upgrade on a single port."""

    def __init__(self, root: Path, ws_path: str, on_ws_message, on_ws_close, on_ws_open=None):
        self.root = Path(root).resolve()
        self.ws_path = ws_path
        self.on_ws_message = on_ws_message
        self.on_ws_close = on_ws_close
        self.on_ws_open = on_ws_open
        self._server = None

    async def start(self, host: str, port: int):
        self._server = await asyncio.start_server(self._handle, host, port)
        return self._server

    async def _handle(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except Exception:
            writer.close()
            return
        if len(head) > MAX_HEAD:
            writer.close()
            return
        try:
            lines = head.decode("latin1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
        except Exception:
            writer.close()
            return

        path = unquote(urlsplit(target).path)

        # --- websocket upgrade ---
        if (
            path == self.ws_path
            and "upgrade" in headers.get("connection", "").lower()
            and headers.get("upgrade", "").lower() == "websocket"
            and "sec-websocket-key" in headers
        ):
            accept = base64.b64encode(
                hashlib.sha1((headers["sec-websocket-key"] + WS_GUID).encode()).digest()
            ).decode()
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            conn = WSConn(reader, writer, self.on_ws_message, self.on_ws_close)
            if self.on_ws_open:
                self.on_ws_open(conn)
            await conn.run()
            return

        # --- plain http ---
        if method not in ("GET", "HEAD"):
            writer.write(_http_response("405 Method Not Allowed", b"method not allowed"))
        elif path == "/health":
            writer.write(_http_response("200 OK", b"ok"))
        else:
            if path.endswith("/"):
                path += "index.html"
            f = (self.root / path.lstrip("/")).resolve()
            ok = f.is_relative_to(self.root) and f.is_file()
            if ok:
                body = f.read_bytes()
                ctype = MIME.get(f.suffix.lower(), "application/octet-stream")
                if method == "HEAD":
                    writer.write(_http_response("200 OK", b"", ctype))
                else:
                    writer.write(_http_response("200 OK", body, ctype))
            else:
                writer.write(_http_response("404 Not Found", b"not found"))
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
