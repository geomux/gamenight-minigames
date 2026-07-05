#!/usr/bin/env python3
"""
GAME NIGHT server — zero dependencies, one process, one port.

    python3 server/main.py            # uses config.toml next to the repo root
    python3 server/main.py --port 9999

Serves the browser client (static files) and the realtime WebSocket from the
same port. Type 'help' in this terminal for host commands.
"""

import argparse
import asyncio
import re
import secrets
import socket
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpws
from room import Room

IS_WINDOWS = sys.platform == "win32"

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"

DEFAULTS = {
    "host": "0.0.0.0",
    "port": 8000,
    "host_password": "",     # "" -> random one is generated and printed
    "join_password": "",     # "" -> anyone with the URL can join
    "max_players": 12,
    "tick_rate": 60,
    "snapshot_rate": 20,     # tick_rate/snapshot_rate must divide cleanly
}


def load_config(argv=None):
    ap = argparse.ArgumentParser(description="Game Night party-game server")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host-password")
    ap.add_argument("--join-password")
    ap.add_argument("--max-players", type=int)
    ap.add_argument("--tick-rate", type=int)
    ap.add_argument("--snapshot-rate", type=int)
    ap.add_argument("--no-tui", action="store_true",
                    help="plain line-based console instead of the dashboard")
    ap.add_argument("--tunnel", action="store_true",
                    help="auto-launch a Cloudflare quick tunnel so remote "
                         "friends get one shareable https URL (needs "
                         "`cloudflared` on PATH)")
    args = ap.parse_args(argv)

    cfg = dict(DEFAULTS)
    path = Path(args.config)
    file_cfg = {}
    if path.is_file():
        with open(path, "rb") as f:
            file_cfg = tomllib.load(f)
        for k in DEFAULTS:
            if k in file_cfg:
                cfg[k] = file_cfg[k]
    for k in DEFAULTS:  # CLI flags win
        v = getattr(args, k.replace("-", "_"), None)
        if v is not None:
            cfg[k] = v
    cfg["port"] = int(cfg["port"])
    cfg["tick_rate"] = max(10, min(60, int(cfg["tick_rate"])))
    cfg["snapshot_rate"] = max(5, min(cfg["tick_rate"], int(cfg["snapshot_rate"])))
    cfg["generated_pw"] = False
    if not cfg["host_password"]:
        cfg["host_password"] = secrets.token_hex(3)
        cfg["generated_pw"] = True
    cfg["no_tui"] = bool(args.no_tui)
    cfg["tunnel"] = bool(args.tunnel) or bool(file_cfg.get("tunnel", False))
    return cfg


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packets are actually sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


async def start_tunnel(port, on_update):
    """Launch a Cloudflare quick tunnel in the background (needs `cloudflared`
    on PATH; nothing to sign up for). Reads its output for the https URL it
    prints and reports back through on_update(url, err) — exactly one of the
    two is set, called once. Returns the subprocess so the caller can
    terminate it on shutdown, or None if cloudflared isn't installed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel", "--url", f"http://localhost:{port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        on_update(None, "cloudflared not found on PATH — install it "
                        "(github.com/cloudflare/cloudflared) or drop --tunnel "
                        "and share the LAN URL over your own VPN/tunnel.")
        return None

    async def pump():
        found = False
        while True:
            line = await proc.stdout.readline()
            if not line:
                if not found:
                    on_update(None, "cloudflared exited before printing a URL "
                                    "— run it by hand to see why.")
                return
            if not found:
                m = TUNNEL_URL_RE.search(line.decode(errors="replace"))
                if m:
                    found = True
                    on_update(m.group(0), None)
            # keep draining stdout even after finding the URL, or the pipe
            # backs up and eventually stalls the cloudflared process

    asyncio.create_task(pump())
    return proc


def banner(cfg):
    tty = sys.stdout.isatty()
    B = "\033[1m" if tty else ""
    C = "\033[36m" if tty else ""
    Y = "\033[33m" if tty else ""
    R = "\033[0m" if tty else ""
    ip = lan_ip()
    port = cfg["port"]
    lines = [
        "",
        f"{B}  ██████   GAME NIGHT   ██████{R}",
        "",
        f"  Friends join:   {C}http://{ip}:{port}{R}   (same wifi)",
        f"  You (host) too — enter the host password at the name screen.",
        "",
        f"  Host password:  {Y}{cfg['host_password']}{R}"
        + ("   (generated this run — set one in config.toml to keep it)" if cfg["generated_pw"] else ""),
        f"  Join password:  {Y}{cfg['join_password'] or '(none — anyone with the URL can join)'}{R}",
        "",
    ]
    if not cfg["tunnel"]:
        lines += [
            "  Over the internet? Easiest is a free tunnel in a second terminal:",
            f"      cloudflared tunnel --url http://localhost:{port}",
            "  then share the https URL it prints — or just run with --tunnel to do "
            "this automatically. (ngrok/tailscale also work — see README.)",
            "",
        ]
    lines += [
        "  Terminal commands: start · settings · bots 3 · kick <name> · help",
        "",
    ]
    print("\n".join(lines))


async def amain(cfg):
    room = Room(cfg)
    server = httpws.Server(CLIENT_DIR, "/ws",
                           on_ws_message=room.on_ws_message,
                           on_ws_close=room.on_ws_close,
                           on_ws_open=room.on_ws_open)
    try:
        await server.start(cfg["host"], cfg["port"])
    except OSError as e:
        print(f"error: can't bind {cfg['host']}:{cfg['port']} ({e}). "
              f"Try --port {cfg['port'] + 1}.")
        return 1

    tunnel_proc = None
    try:
        # interactive dashboard when we have a real terminal, else plain console
        if not cfg["no_tui"] and sys.stdin.isatty() and sys.stdout.isatty():
            from tui import Tui
            ui = Tui(room, cfg, lan_ip())
            room.log = ui.log
            if cfg["tunnel"]:
                ui.set_tunnel_status("tunnel starting…")
                tunnel_proc = await start_tunnel(cfg["port"], ui.on_tunnel_update)
            await asyncio.gather(room.run(), ui.run())
            return 0

        if cfg["tunnel"]:
            print("starting Cloudflare tunnel…")
            done = asyncio.Event()

            def _on_tunnel(url, err):
                if url:
                    print(f"  Remote friends: {url}   (anywhere — share this link)")
                elif err:
                    print(f"  {err}")
                done.set()

            tunnel_proc = await start_tunnel(cfg["port"], _on_tunnel)
            if tunnel_proc:
                try:
                    await asyncio.wait_for(done.wait(), timeout=12.0)
                except asyncio.TimeoutError:
                    print("  still starting — its URL will print above once ready")

        banner(cfg)
        loop = asyncio.get_event_loop()

        def handle_line(line):
            """Feed one console line to the room; returns False to stop reading
            (EOF), True to keep going."""
            if not line:                       # EOF (piped stdin closed)
                return False
            try:
                reply = room.console(line)
            except SystemExit:
                print("bye!")
                for task in asyncio.all_tasks(loop):
                    task.cancel()
                return False
            if reply:
                print(reply)
            return True

        console_task = None
        if IS_WINDOWS:
            # Windows asyncio has no add_reader for stdin. Read lines on a
            # daemon thread (blocking readline gives native console line-
            # editing) and hand them to the event loop via a queue, so typed
            # commands still work in plain no-tui mode. The thread is a daemon
            # so a blocked readline never keeps the process from exiting.
            import threading

            line_q = asyncio.Queue()

            def _reader():
                while True:
                    try:
                        line = sys.stdin.readline()
                    except (ValueError, OSError):
                        line = ""
                    loop.call_soon_threadsafe(line_q.put_nowait, line or None)
                    if not line:
                        return

            threading.Thread(target=_reader, daemon=True).start()

            async def _consume():
                while True:
                    line = await line_q.get()
                    if line is None or not handle_line(line):
                        return

            console_task = asyncio.create_task(_consume())
        else:
            def on_stdin():
                line = sys.stdin.readline()
                if not handle_line(line):
                    try:
                        loop.remove_reader(sys.stdin.fileno())
                    except (ValueError, OSError):
                        pass

            try:
                loop.add_reader(sys.stdin.fileno(), on_stdin)
            except (ValueError, OSError, NotImplementedError):
                print("(no interactive terminal — use the host password web panel)")

        try:
            await room.run()
        finally:
            if console_task:
                console_task.cancel()
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()


def main():
    try:  # prompt logs to appear even when stdout is piped (nohup, tests)
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    cfg = load_config()
    try:
        code = asyncio.run(amain(cfg))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nserver stopped.")
        code = 0
    sys.exit(code or 0)


if __name__ == "__main__":
    main()
