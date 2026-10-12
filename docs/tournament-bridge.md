# Tournament bridge (Codenames)

Lets the [Codenames Tournament bot](https://github.com/Stayingfalse/CodwellTornement)
score tournament games from codenames.game automatically, using this bot's
Codenames watcher.

## How it works

1. When the tournament starts a match, it creates a Discord thread for it and
   **binds** that thread here: "thread `600…` is match `R3M2`, played by these
   four Discord users".
2. Players post their codenames.game room link in the thread, as they would
   anywhere else. Because the thread is bound:
   - the room card stays in the thread, even if the server has a forced
     notification channel;
   - the four players are listed on the card as invited, so they can press
     **🙋 I'm playing**. Anyone who claimed a name before gets their seat
     matched automatically.
3. Every game that finishes in the room (one room for the whole match: use
   **Play next game** between games) is queued and POSTed to the tournament.
   The tournament checks the seats against the line-up for the match's
   current game:
   - **Seats match:** the result is logged, and the thread moves on to the
     next game.
   - **Seats don't match, or a seat isn't linked to a Discord user:** the
     result is posted in the thread with **Log as Game N** / **Dismiss**
     buttons for a player or admin.
4. When the match ends, the tournament removes the binding, and this bot stops
   watching the thread's room.

Results are kept in a database outbox until the tournament accepts them, so a
restart or an outage on either side loses nothing. Each game has a stable
`event_id`, so redelivery never scores a game twice.

## Configuration

| This bot (`.env`) | Description |
|---|---|
| `BRIDGE_TOKEN` | Shared secret. Setting it turns the bridge on (Codenames must be enabled). Use the same value on both bots. |
| `BRIDGE_HOST` / `BRIDGE_PORT` | Where the bridge API listens. Default `0.0.0.0:8081`. Not published on the host by default. |
| `BRIDGE_CALLBACK_URLS` | Comma-separated result URLs, tried in order. Put the internal one first and the public HTTPS one as fallback. |

| Tournament bot (`.env`) | Description |
|---|---|
| `BRIDGE_TOKEN` | The same shared secret. |
| `BRIDGE_URLS` | Comma-separated base URLs of this bot, tried in order, e.g. `http://bga-bot:8081,https://bga.example.com`. |

### Same host, separate containers

Put both containers on a shared Docker network so they reach each other by
service name:

```bash
docker network create codenames-bridge
# in this repo
docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d
# in the tournament repo
docker compose -f docker-compose.yml -f docker-compose.bridge.yml up -d
```

```env
# this bot
BRIDGE_TOKEN=<long random string>
BRIDGE_CALLBACK_URLS=http://codenames-bot:3000/api/bridge/codenames/result,https://tournament.example.com/api/bridge/codenames/result

# tournament bot (WEB_PORT=3000)
BRIDGE_TOKEN=<the same string>
BRIDGE_URLS=http://bga-bot:8081,https://bga.example.com
```

### Public HTTPS fallback

The second URL in each list is only used when the first can't be reached.
Both sides check the bearer token on every request.

- **The tournament's endpoint** is on its normal web server, so its public
  `WEB_URL` already works.
- **This bot's bridge API** is also served by the dashboard when
  `DASHBOARD_ENABLED=1`, so the dashboard's public HTTPS address works as
  `https://bga.example.com/api/bridge/...` with no extra proxy rules. Without
  the dashboard, proxy `/api/bridge/` to `BRIDGE_PORT`.

## API

All requests carry `Authorization: Bearer <BRIDGE_TOKEN>`. All bodies are JSON.

### This bot

| Method & path | Body | Result |
|---|---|---|
| `GET /api/bridge/health` | — | `{"ok": true}` |
| `PUT /api/bridge/codenames/bindings/{channel_id}` | `{"guild_id", "external_ref", "players": [discord ids]}` | The binding. Idempotent: the tournament re-sends it whenever it changes. |
| `GET /api/bridge/codenames/bindings/{channel_id}` | — | The binding, the rooms watched in the thread, and each result's delivery status. |
| `DELETE /api/bridge/codenames/bindings/{channel_id}[?keep_watching=1]` | — | `{"removed": bool}`. Also stops watching the thread's rooms unless `keep_watching`. |

`external_ref` is at most 100 characters; `players` is at most 10 IDs.

### Tournament bot

`POST /api/bridge/codenames/result`, with an `Idempotency-Key: <event_id>`
header:

```json
{
  "event": "codenames.game_finished",
  "event_id": "juzaf-hitos:QuVMIPSqNpU",
  "external_ref": "R3M2",
  "guild_id": "111", "channel_id": "600",
  "room_id": "juzaf-hitos", "room_url": "https://codenames.game/r/juzaf-hitos",
  "match_id": "QuVMIPSqNpU", "game_number": 2,
  "winner": "blue", "finish_reason": "assassin", "assassin": true,
  "remaining": {"red": 3, "blue": 0}, "loser_remaining": 3,
  "seats": {
    "blue": {"spymasters": [{"player_id": "3", "name": "Carol", "discord_user_id": "333"}], "operatives": [...]},
    "red":  {"spymasters": [...], "operatives": [...]}
  },
  "finished_at": "2026-10-12T20:15:00+00:00"
}
```

`discord_user_id` is `null` for a seat nobody has claimed. `game_number` counts
games in the room as this bot sees them; the tournament goes by seats, not by
this number.

How this bot treats the response:

| Response | Meaning | What happens to the result |
|---|---|---|
| 2xx | Handled (logged, waiting for confirmation, duplicate, or ignored) | Marked `delivered` |
| 408, 425, 429, 5xx, timeout, connection error | Try again later | The next URL is tried; if all fail, it is retried with backoff (15 s doubling, up to 1 h) |
| Any other 4xx | Refused | Marked `rejected` and not retried |

After 200 failed attempts (about 8 days), a result is marked `abandoned`. You
can inspect the outbox in the `codenames_result_outbox` table.
