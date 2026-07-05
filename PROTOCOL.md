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
| host | `{"t":"host","a":action,...}` | Host only. Actions: `start`, `pause` (toggle; optional `"v":bool` for explicit on/off), `abort`, `lobby`, `set_game {"g":id}`, `set {"k":key,"v":val}`, `kick {"p":pid}`, `reset_scores`. |
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
| s | `{"t":"s","g":gameId,…}` | State snapshot at snapshot-rate (default 20 Hz), **coalesced per client** (slow clients skip frames, never lag behind). |
| fx | `{"t":"fx","ev":[…]}` | Reliable event list for juice (see below). |
| end | `{"t":"end","placements":[[pid,place,pts]…],"totals":[[pid,wins,pts]…],"winner":[pids],"auto":secs}` | Round over. Placements use competition ranking (ties share a place). Auto-returns to lobby after `auto` seconds. |
| pause | `{"t":"pause","on":bool}` | Host froze/unfroze the round. While paused the sim, countdown clock, and snapshots all stop; `round` msgs carry `"paused"` for late joiners. |
| toast | `{"t":"toast","msg"}` | Announcement banner. |
| kicked | `{"t":"kicked","msg"}` | Then the connection closes. |
| pong | `{"t":"pong","ts"}` | Echo of `ping`. |

### Snapshots per game

**Convention:** entity rows start `[pid, x, y, alive, charge, …]` — index 4
is the player's 0–1 action charge (dash/snowball/fire; 1 = ready). Games with
an action also send `"action":"LABEL"` in the arena payload; the client shows
the big side charge meter whenever that's present. (Light Cycles has no
action, hence no charge and no meter.)

**Sumo** — `{"t":"s","g":"sumo","R":ringRadius,"e":[[pid,x,y,alive,dash01,r]…],"wind"?:[dx,dy]}`
`r` = body radius.

**Light Cycles** — `{"t":"s","g":"cycles","heads":[[pid,gx,gy,alive,boost01,dx,dy,boosting]…],"cells":[[gx,gy,pid]…],"margin":m}`
`boost01` = 0–1 boost meter (1 = full charge; stays 0 if the `boost`
setting is off). `boosting` = 1 while a boost is actively being applied
that tick. `cells` are **deltas**: only cells claimed since the previous
snapshot (a `full` snapshot — countdown preview or late join — carries
every occupied cell). Clients accumulate them locally. `margin` =
closing-wall depth in cells.

**Avalanche Run** — `{"t":"s","g":"ski","cam":y,"spd":v,"e":[[pid,x,y,alive,ball01,tumble01]…],"obs":[[id,x,y,type]…],"balls":[[bid,x,y,vx,vy]…]}`
`cam` = world-y of the top of the screen (shared auto-scrolling camera —
clients offset all world coords by it, and scroll their parallax layers from
it). `obs` are **deltas** like cycles' cells: each obstacle (0 = tree,
1 = rock) is sent exactly once; clients cull anything behind the camera.
Entity `y` is absolute world-y. `balls` carry a per-round `bid` plus
`vx`/`vy` (world px/s, rounded ints) so clients dead-reckon smooth 60fps
flight from the last snapshot instead of snapping to a new raw position
every update.

**Aces High** — `{"t":"s","g":"planes","e":[[pid,x,y,alive,fire01,ang,hp,inv]…],"b":[[bid,x,y,vx,vy]…]}`
`ang` = heading in centiradians (divide by 100). `hp` = hearts left, `inv` =
1 during post-hit blink. `b` = live bullets, each `[bid,x,y,vx,vy]` (`vx`/`vy`
world px/s, rounded ints) — clients dead-reckon position from the last
snapshot instead of interpolating raw points, so bullets fly straight and
smooth between the ~15-20Hz updates. The world wraps at 960×540 — clients
interpolate wrap-aware (shortest path across edges). The arena payload
carries the static geometry: `"islands":[[x,y,r]…]` (solid — planes bounce,
bullets stop) and `"gusts":[[x,y,r,ang100]…]` (directional shove zones;
clients animate them and detect boost locally, zero extra wire cost).

**Bumper Ball** — `{"t":"s","g":"bumper","score":[r,b],"e":[[pid,x,y,alive,dash01]…],"ball":[x,y,vx,vy],"ko"?:secs}`
`score` = `[red goals, blue goals]`. `e` rows have no radius field — bodies
are a fixed size and nobody is ever eliminated, so `alive` is always 1;
`dash01` stays 0 whenever the `dash` setting is off. `ball` carries `vx`/`vy`
(world px/s, rounded ints) so clients dead-reckon a smooth 60fps ball exactly
like the ski/planes projectile registries, keyed on a single fixed id instead
of a per-projectile one. `ko`, when present, is the seconds remaining in the
post-goal kickoff freeze — a brief window (~1.4s) where the sim keeps ticking
and snapshots keep flowing, but movement/scoring are paused while everyone
re-spots. The arena payload carries `"teams":[[redPids…],[bluePids…]]` (roster
alternates onto the two teams so bots split evenly) and `"goalH"` (opening
height of both goal mouths, centered on the world's left/right edges).

### Fx events

| Event | Shape | Meaning |
|---|---|---|
| dash | `["dash",pid]` | Sumo dash (afterimage + shake) |
| hit | `["hit",x,y,intensity]` | Big sumo collision (sparks; intensity 0–1) |
| fall | `["fall",pid,x,y,vx,vy]` | Knocked off the ring (tumble animation) |
| die | `["die",pid,gx,gy]` | Cycle crashed (burst at grid cell) |
| clear | `["clear",pid]` | Dead player's trail vanishes (trails=vanish) |
| wall | `["wall",margin]` | Closing walls advanced one cell |
| boost | `["boost",pid]` | Light Cycle boost activated |
| throw | `["throw",pid]` | Snowball thrown |
| bonk | `["bonk",pid,x,screenY]` | Skier hit a tree/rock (y is screen-relative) |
| splat | `["splat",victim,thrower]` | Snowball connected |
| wipe | `["wipe",pid,x,screenY]` | The avalanche got them |
| shoot | `["shoot",pid]` | Plane fired (muzzle flash) |
| hitp | `["hitp",pid,x,y]` | Plane lost a heart |
| clash | `["clash",x,y]` | Mid-air plane collision (sparks) |
| down | `["down",pid,x,y]` | Plane destroyed (explosion + tumble) |
| thud | `["thud",pid,x,y]` | Plane bounced off a floating island |
| puff | `["puff",x,y]` | Bullet absorbed by an island |
| goal | `["goal",team,x,y]` | Ball fully crossed a goal line (`team` = 0 red / 1 blue is whoever SCORED); a kickoff reset follows unless that goal just won the round |

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
