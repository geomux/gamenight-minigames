# Wire protocol

One WebSocket endpoint at `/ws`, JSON text messages, each with a `"t"` type
field. The client derives `ws://` vs `wss://` from the page protocol.
Static files and the websocket share the single server port.

Entity rows are arrays (not objects) to keep snapshots small. Positions are
in a fixed 960×540 world for Sumo and 96×54 grid cells for Light Cycles.

## Client → Server

| Msg | Shape | Notes |
|---|---|---|
| join | `{"t":"join","name":str,"pw":str,"sess":str\|null}` | `pw` = host password ⇒ host; else must match join password if one is set. `sess` reclaims a previous seat (score survives reconnects). |
| input | `{"t":"input","k":{"u":b,"d":b,"l":b,"r":b,"a":b}}` | Full key-state, sent on change + 1 s resend (self-healing). `a` = action/dash. |
| host | `{"t":"host","a":action,...}` | Host only. Actions: `start`, `lobby`, `set_game {"g":id}`, `set {"k":key,"v":val}`, `kick {"p":pid}`, `reset_scores`. |
| ping | `{"t":"ping","ts":n}` | Echoed back as `pong` for the HUD latency dot. |

## Server → Client

| Msg | Shape | Notes |
|---|---|---|
| hello | `{"t":"hello","needPw":bool,"title":str}` | On connect, before join. |
| joined | `{"t":"joined","you":{"id","name","host"},"sess":str}` | Store `sess` in localStorage. |
| err | `{"t":"err","msg":str}` | Join rejected (wrong password, server full). Connection stays open for a retry. |
| room | `{"t":"room","state","round","gameId","games":[{id,name,tag}],"schema":[…],"settings":{…},"players":[{id,name,color,host,bot,conn,wins,pts,inRound}],"maxP"}` | Full lobby state, broadcast on every change. `schema` drives the settings panel (see below). |
| round | `{"t":"round","phase":"countdown"\|"playing","round",game:{id,name,tag,controls},"settings":{…},"arena":{…},"roster":[pids],"secs"?,"preview"?,"spectate"?}` | Round start (or context for a late joiner). `preview` is a full snapshot so spawns render during the countdown. |
| go | `{"t":"go"}` | Countdown over, inputs live. |
| s | `{"t":"s","g":gameId,…}` | State snapshot at snapshot-rate (default 15 Hz), **coalesced per client** (slow clients skip frames, never lag behind). |
| fx | `{"t":"fx","ev":[…]}` | Reliable event list for juice (see below). |
| end | `{"t":"end","placements":[[pid,place,pts]…],"totals":[[pid,wins,pts]…],"winner":[pids],"auto":secs}` | Round over. Placements use competition ranking (ties share a place). Auto-returns to lobby after `auto` seconds. |
| toast | `{"t":"toast","msg"}` | Announcement banner. |
| kicked | `{"t":"kicked","msg"}` | Then the connection closes. |
| pong | `{"t":"pong","ts"}` | Echo of `ping`. |

### Snapshots per game

**Sumo** — `{"t":"s","g":"sumo","R":ringRadius,"e":[[pid,x,y,alive,dash01,r]…],"wind"?:[dx,dy]}`
`dash01` = dash charge 0–1 (1 = ready). `r` = body radius.

**Light Cycles** — `{"t":"s","g":"cycles","heads":[[pid,gx,gy,alive,dx,dy]…],"cells":[[gx,gy,pid]…],"margin":m}`
`cells` are **deltas**: only cells claimed since the previous snapshot (a
`full` snapshot — countdown preview or late join — carries every occupied
cell). Clients accumulate them locally. `margin` = closing-wall depth in
cells.

### Fx events

| Event | Shape | Meaning |
|---|---|---|
| dash | `["dash",pid]` | Sumo dash (afterimage + shake) |
| hit | `["hit",x,y,intensity]` | Big sumo collision (sparks; intensity 0–1) |
| fall | `["fall",pid,x,y,vx,vy]` | Knocked off the ring (tumble animation) |
| die | `["die",pid,gx,gy]` | Cycle crashed (burst at grid cell) |
| clear | `["clear",pid]` | Dead player's trail vanishes (trails=vanish) |
| wall | `["wall",margin]` | Closing walls advanced one cell |

### Settings schema

Each entry renders one control in the lobby panel, host-editable, visible to
all: `{"k":key,"label":str,"type":"bool"|"int"|"choice","def":…}` plus
`min`/`max` for `int` or `choices` for `choice`. The server validates every
`set` against this schema (clamped ints, membership-checked choices), and
settings persist per game between rounds.

### Lifecycle

```
lobby ──host start──▶ countdown(3s) ──▶ playing ──game over──▶ results(14s) ──▶ lobby
                          ▲                                        │
                          └────────────── host rematch ────────────┘
```

Disconnects mid-round leave the body in the sim (it drifts / rides straight);
the session token reclaims seat, score, and — if the round is still going —
control of the body.
