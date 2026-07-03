# GAME NIGHT 🕹️

Browser-based, real-time multiplayer party games for you and your friends —
short competitive rounds, one winner per round, tweak the settings, run it
back. You host it on your own machine; friends just open a URL. **Zero
dependencies**: pure Python standard library server + vanilla JS client. No
pip, no accounts, no paid services.

**Games so far:** Sumo Ring (bump friends off a shrinking ring) and
Light Cycles (Tron — dodge walls and trails). Both run through the same
pluggable shell, so adding more is easy (see [Extending](#extending)).

---

## Quickstart (host)

Needs Python 3.11+ (Debian 12/13, Ubuntu 22.04+ are fine). No installs.

```bash
cd gamenight-minigame
python3 server/main.py
```

The terminal prints the join URL and the **host password**. Open the URL in
your own browser, type your name **and the host password** → you're the Host
with the start button and settings panel. Friends type just a name (plus the
join password, if you set one).

Change passwords in `config.toml`:

```toml
host_password = "change-me"   # whoever types this becomes Host
join_password = ""            # optional gate for everyone else
```

You can also just type commands in the server terminal (`start`, `help`, …) —
the terminal always has full host powers.

## How friends connect

**Same wifi / LAN:** share the printed `http://<your-ip>:8000` URL. Done.

**Over the internet** — pick one (a tunnel is the reliable choice; many home
ISPs use CGNAT, which silently breaks plain port-forwarding):

| Option | How | Notes |
|---|---|---|
| **Cloudflare Tunnel** (recommended) | `cloudflared tunnel --url http://localhost:8000` | Free, no account needed for quick tunnels. Share the printed `https://…` URL. |
| **ngrok** | `ngrok http 8000` | Free tier works; URL changes each run. |
| **Tailscale** | You + friends install Tailscale, share your tailnet IP | Most robust; friends install a small app once. |
| **Port forwarding** | Forward TCP 8000 on your router, share your public IP | **Breaks under CGNAT.** If it doesn't work, use a tunnel. |

The client picks `ws://` or `wss://` automatically, so https tunnel URLs just
work. If a friend gets a plain white page or "reconnecting…" forever, they're
usually behind an old bookmark of a dead tunnel URL — send the fresh one.

## Playing

1. Everyone joins → names pop into the **lobby**.
2. Host picks the game and tweaks **Game Settings** (every setting is a
   modifier: ice floor, reverse controls, giant bodies, closing walls, wind,
   wraparound, bot count/difficulty, …). Everyone sees the changes live.
3. Host hits **START ROUND** → 3-second intro → play.
4. Round ends → placements + points (**1st of N players gets N points, last
   gets 1; ties share**). Winner gets a ⭐ win on the session scoreboard.
5. **REMATCH** (same settings, instantly) or back to the **lobby** to retweak.
   Scoreboard accumulates until the host resets it. Nobody ever sits out —
   knocked-out players spectate the rest of the round, then they're back in.

**Controls:** WASD / arrows to move (turn, in Light Cycles) · SPACE to dash
(Sumo). If you disconnect or refresh, you get your seat, name, and points
back automatically.

### The games

- **Sumo Ring** — top-down knockback arena over the void. Bump people out;
  the ring shrinks so nobody can turtle. Dash for big plays; dashing bumps
  hit ~2× harder. Last one standing wins.
- **Light Cycles** — everyone rides constantly, leaving a solid trail. Walls,
  trails, other players: touch anything and you're out. Optional wraparound
  edges, vanishing dead trails, and closing walls.

### Bots

Set **Bots** (0–6) and **Bot skill** (easy / normal / mean) in Game Settings —
great for testing solo (`bots 3` in the terminal) or filling out small groups.
Bots play both games and show up on the scoreboard like anyone else.

## Host controls

**In the browser** (join with the host password): start/rematch/lobby buttons,
game picker, settings panel, scoreboard reset.

**In the server terminal — interactive dashboard.** Run in a normal terminal
and you get a live, color dashboard: state, join URL + passwords, players,
scoreboard, and log, plus an arrow-key menu:

- **↑ / ↓** move · **← / →** change a value (game, any setting, bots)
- **Enter** select (start round, kick submenu, say, reset, quit)
- Quick keys: **s** start · **a** abort · **q** quit

Settings lock while a round is live; if a round can't start, the dashboard
tells you why (e.g. nobody connected yet — add bots or share the URL).

**Plain console** (used automatically when piped/`nohup`'d, or force it with
`--no-tui`) takes typed commands:

```
start           begin a round            game sumo|cycles   switch game
lobby           back to the lobby        set <key> <value>  change a setting
abort           kill a stuck round       settings           show current settings
players         list everyone            scores             session scoreboard
bots <n>        quick bot count          skill easy|normal|mean
kick <name>     remove a player          resetscores        wipe the scoreboard
say <message>   toast to all players     quit               stop the server
```

## Config

`config.toml` (CLI flags override; `python3 server/main.py --help`):

| Key | Default | What |
|---|---|---|
| `port` | `8000` | The one port for everything |
| `host_password` | `"change-me"` | Grants Host in the browser. Empty → random per run, printed |
| `join_password` | `""` | Required from everyone if set |
| `max_players` | `12` | Human connection cap |
| `--tick-rate` | `30` | Simulation Hz (flag only) |
| `--snapshot-rate` | `15` | Broadcast Hz (flag only) |

## Extending

The shell (lobby, networking, scoring, settings UI, bots plumbing) never
changes. See `PROTOCOL.md` for the wire format.

**Add a mini-game:** create `server/minigames/yourgame.py` implementing the
contract in `base.py` — `settings_schema()` (your settings panel builds
itself), `setup()` (static arena payload), `on_input()`, `tick(dt, events)`,
`snapshot(full)`, `is_over()`, `placements()`, and optionally `bot_input()`.
Register it with one line in `minigames/__init__.py`, add a matching draw
branch in `client/js/render.js`, and it appears in the lobby dropdown.
`cycles.py` (~250 lines) is the model to copy.

**Add a "modifier":** it's just a settings-schema entry plus however it tweaks
your params. E.g. in `sumo.py`, add
`{"k": "sticky", "label": "Sticky floor", "type": "bool", "def": False}` to the
schema and `if s.get("sticky"): p["drag"] *= 3` in `__init__`. That's the
whole system — settings arrive validated, the panel renders itself, and the
intro screen announces anything non-default.

## Testing

```bash
python3 tools/smoke_test.py   # full game E2E over real websockets
python3 tools/tui_test.py     # drives the terminal dashboard through a pty
```

`smoke_test.py` boots the real server and plays both games end-to-end: lobby,
passwords, settings, bot rounds, scoring, reconnect, terminal commands, kick.
`tui_test.py` types real arrow keys into the dashboard. Both passing means
the game actually works headless.

## Troubleshooting

- **Port already in use** → `python3 server/main.py --port 8001` (tunnels
  don't care which port).
- **Friends can't reach your LAN IP** → firewall: `sudo ufw allow 8000/tcp`,
  or just use a tunnel.
- **"Wrong password"** → passwords are case-sensitive; the host password also
  gets you in even when a join password is set.
- **Laggy for someone far away** → normal internet physics (inputs take a
  round trip). Sumo plays great at 150–250 ms; Light Cycles rewards lower
  ping — try `speed: slow` for fairness, or a closer tunnel region.
- **Someone's frozen mid-round** → their body drifts (comedy included); they
  can refresh to reclaim it, or the round just resolves without them.

## Design notes (why it's built this way)

- **Server-authoritative:** clients only send key-states; all physics runs in
  one fixed-tick loop on the server, so everyone sees the same game and
  settings apply globally.
- **Zero dependencies:** the HTTP + WebSocket layer is ~250 lines of stdlib
  (`server/httpws.py`). Nothing to install on either end, nothing to break.
- **Slow clients can't stall the game:** each connection gets a queue where
  stale snapshots are dropped instead of piling up.
- **Light Cycles syncs trail *deltas***, not the whole trail, so bandwidth
  stays flat over long rounds even through a free tunnel.
