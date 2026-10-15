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
   - the four players are listed on the card as invited. Each one's seat is
     matched automatically by the name they used before, or by their Discord
     or tournament name (ignoring case, spaces, punctuation and emoji), as
     long as exactly one seat matches. Anyone left over presses
     **🙋 I'm playing**, or an admin runs `/codenames seats` in the thread and
     picks each player's name for them;
   - the card shows the **line-up for the current game**: each expected seat
     with ✅, "sitting as …" or "hasn't pressed I'm playing", plus anyone in a
     seat they shouldn't be in. In the lobby the players get one ping saying
     where to sit. Each time the line-up changes (after every game) there's a
     new ping, and it's deleted once the game starts. The tournament only
     sends a line-up after it has processed the last result from the thread,
     and this bot ignores a line-up older than its last report, so a stale one
     is never shown.
3. Every game that finishes in the room (one room for the whole match: use
   **Play next game** between games) is queued and POSTed to the tournament.
   The tournament checks the seats against the line-up for the match's
   current game:
   - **Seats match:** the result is logged, and the thread moves on to the
     next game.
   - **Seats don't match, or a seat isn't linked to a Discord user:** the
     result is posted in the thread with **Log as Game N** / **Dismiss**
     buttons for a player or admin.
4. While the match runs:
   - **Reminders:** a turn that has waited `CODENAMES_REMINDER_HOURS` (24 by
     default) is pinged again. Within 48 hours of the round's `deadline`,
     reminders come every 6 hours and say when the round ends.
   - **No idle timeout:** tournament threads are never dropped for being idle;
     the tournament ends the watch.
   - **Live status:** the tournament polls the binding every 2 minutes to show
     each match's live status on its dashboard and to nudge stalled matches.
5. When the match ends, the tournament removes the binding, and this bot stops
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
| `PUT /api/bridge/codenames/bindings/{channel_id}` | `{"guild_id", "external_ref", "players": [discord ids], "player_names": {id: name}, "lineup": {...} \| null}` | The binding. Idempotent: the tournament re-sends it whenever it changes. |
| `GET /api/bridge/codenames/bindings/{channel_id}` | — | The binding, each watched room's live status (below), and each result's delivery status. |
| `DELETE /api/bridge/codenames/bindings/{channel_id}[?keep_watching=1]` | — | `{"removed": bool}`. Also stops watching the thread's rooms unless `keep_watching`. |

`external_ref` is at most 100 characters; `players` is at most 10 IDs.
`player_names`, `lineup` and `deadline` (unix seconds when the round ends) are
optional:

```json
"player_names": {"333": "Carol"},
"lineup": {
  "game": 2,
  "last_event_id": "juzaf-hitos:Y1L4zY59RZO",
  "blue": {"spymaster": "444", "operative": "333"},
  "red":  {"spymaster": "222", "operative": "111"}
}
```

`last_event_id` is the last result the tournament processed from this thread
(`null` before the first). The line-up is only shown while it equals the last
result this bot reported from the thread.

Each entry in `rooms` from `GET` looks like this:

```json
{"room_id": "juzaf-hitos", "room_url": "https://codenames.game/r/juzaf-hitos", "connected": true,
 "game_number": 2, "phase": "in_progress", "active_team": "red", "active_role": "operatives",
 "waiting_on": [{"name": "Alice", "discord_user_id": "222"}],
 "turn_started_at": "2026-10-12T10:00:00+00:00", "clue": {"word": "OCEAN", "number": "2"},
 "remaining": {"red": 4, "blue": 6}, "players_in_room": 3}
```

`phase` is `lobby`, `in_progress` or `finished`. The turn fields are `null`
or empty unless a game is in progress.

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

The payload also has `starting_team`, `board` (25 × `{"word", "color",
"revealed"}`), and `turns`: every clue in order, with its giver and guesses.

```json
"turns": [
  {"team": "red", "word": "ANIMAL", "number": "3",
   "giver": {"player_id": "1", "name": "Bob", "discord_user_id": "111"},
   "guesses": [{"word": "DOG", "color": "red", "player_id": "0", "name": "Alice", "discord_user_id": "222"}]}
]
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
