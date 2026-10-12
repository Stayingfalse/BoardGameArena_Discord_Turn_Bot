# BGA Discord self-host bot

Self-hosted Discord bot for Board Game Arena.

The bot's primary job is **automatic link detection**: whenever someone posts a BGA game URL in a Discord channel, the bot immediately starts watching that table and posts a live status message that it keeps up to date throughout the game.

No BGA account, no password, no cookie — the bot only reads what any anonymous spectator can see.

**How it works:**
1. A player posts a BGA table link in any Discord channel the bot can read.
2. The bot detects the link and begins watching the table.
3. One message is posted and updated live across three stages: **Recruiting → In Progress → Finished**.
4. When the game ends, the message is marked finished and the watch is removed automatically.

To get Discord mentions in turn notifications, server admins can link Discord members to their BGA accounts with `/bga link-member`. Players can also self-link by clicking the **Link your BGA & Discord** button that appears on the recruiting message.

**Codenames too.** Post a [codenames.game](https://codenames.game) room link and the bot joins the room as a spectator, posts a live "Game N" card, and pings the spymaster when a clue is needed and the operatives when it's their turn to guess. That makes the site work as a turn-based game. See [Codenames](#codenames-codenamesgame).

## Quick start

### Docker (recommended)

The easiest way to run the bot is with Docker Compose. The container starts automatically when Docker starts and restarts itself if it ever crashes.

```bash
cp .env.example .env
# Edit .env and set at least DISCORD_TOKEN
docker compose up -d
```

The SQLite database is stored in a named Docker volume (`bga_data`) so it survives container restarts and upgrades.

```bash
docker compose logs -f       # view logs
docker compose down          # stop
docker compose up -d --build # rebuild after a code change
```

### Windows PowerShell

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env, then:
python -m bga_turn
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env, then:
python -m bga_turn
```

## 1. Setup

### Requirements

- Docker and Docker Compose (recommended), **or** Python 3.11 or newer
- A Discord bot created in the Discord Developer Portal with the **Message Content Intent** enabled
- The bot invited to your Discord server
- At least one BGA table publicly accessible in spectator mode

---

### Step 1 — Create the Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**.
2. Give it a name and create it.
3. Open the **Bot** tab in the left sidebar.
4. Click **Reset Token** (or **Copy** if a token is already shown). This is your `DISCORD_TOKEN`. Keep it secret — if it is ever exposed, regenerate it immediately.
5. On the same **Bot** tab, scroll down to **Privileged Gateway Intents** and enable **Message Content Intent**. This is required for the bot to read posted links and auto-watch tables.

> **Why is the Message Content Intent needed?**
> Discord hides message text from bots by default. Enabling this intent lets the bot read the content of messages so it can detect BGA links automatically.

---

### Step 2 — Invite the bot to your server

The easiest method is to use the invite URL that the bot prints in its own logs on startup. Start the bot once with just `DISCORD_TOKEN` set, look for the line:

```
Bot invite URL: https://discord.com/oauth2/authorize?client_id=...
```

Open that URL in your browser, choose your server, and approve. The bot will have exactly the permissions it needs.

**Alternatively**, generate the URL manually in the Developer Portal:

1. Open your application in the Developer Portal.
2. Go to **Installation** (or **OAuth2 > URL Generator** on older portals).
3. Under **Guild Install**, select scopes: `bot` and `applications.commands`.
4. Under **Bot Permissions**, select:
   - `View Channels`
   - `Send Messages`
   - `Send Messages in Threads` *(Codenames rooms posted in threads, e.g. tournament match threads)*
   - `Embed Links`
   - `Read Message History`
   - `Manage Messages` *(needed to tidy up shared links)*
   - `Add Reactions` *(the bot reacts 🕵️ to Codenames room links it watches; optional)*
   - `Manage Webhooks` *(Codenames updates are posted as "Codenames" instead of under the bot's name; optional)*
5. Copy the generated URL, open it in a browser, and add the bot to your server.

---

### Step 3 — Get your server ID (`DISCORD_GUILD_ID`)

Setting `DISCORD_GUILD_ID` is optional but strongly recommended during initial setup: it makes slash commands appear in your server almost instantly instead of waiting for Discord's global sync (which can take up to an hour).

1. Open the Discord app.
2. Go to **User Settings → Advanced** and enable **Developer Mode**.
3. Right-click your server icon or name and click **Copy Server ID**.
4. Paste that value into `DISCORD_GUILD_ID=...` in your `.env`.

To register commands on multiple servers at once, set `DISCORD_GUILD_ID` to a comma-separated list of server IDs (e.g. `DISCORD_GUILD_ID=111222333,444555666`).

---

### Step 4 — Configure `.env`

Copy `.env.example` to `.env` and fill in at least `DISCORD_TOKEN`:

```env
DISCORD_TOKEN=paste_your_bot_token_here
DISCORD_GUILD_ID=paste_your_server_id_here
```

A complete example with all variables explained:

```env
# --- Required ---
DISCORD_TOKEN=paste_your_bot_token_here

# --- Recommended during setup ---
# Your Discord server ID. Speeds up slash command registration to this server only.
DISCORD_GUILD_ID=paste_your_server_id_here

# --- Bot behaviour ---
BGA_POLL_SECONDS=15             # How often the monitor checks watched tables (seconds)
BGA_DB_PATH=bga_bot.db          # SQLite database file path
BGA_WS_URL=wss://ws-x1.boardgamearena.com/connection/websocket
LOG_LEVEL=INFO                  # DEBUG, INFO, WARNING, ERROR
BOT_LANG=en                     # language file in src/bga_turn/locales/

# --- Optional features ---
# Set to 1 to only post the recruiting embed and auto-unwatch when the game starts.
BGA_RECRUITING_ONLY=0

# Set to 1 to delete the original message that contained the BGA invite link
# after the watch is registered. Requires the Manage Messages permission.
BGA_DELETE_INVITE_MESSAGE=

# Channel ID to route all bot notifications to a single fixed channel,
# regardless of which channel the original link was posted in.
BGA_FORCED_CHANNEL_ID=

# Set to 1 once to delete stale global slash commands before guild sync, then back to 0.
DISCORD_CLEAR_GLOBAL_COMMANDS=0

# Set to 1 to re-enable the legacy tableinfos HTTP fallback for end-of-game detection.
BGA_ENABLE_TABLEINFOS_FALLBACK=0

# --- Codenames (codenames.game) ---
CODENAMES_ENABLED=1             # Watch posted codenames.game room links
CODENAMES_BOT_NAMES=            # Optional "|"-separated names the bot uses in rooms
CODENAMES_IDLE_HOURS=168        # Stop watching a room with no moves for this long (7 days)

# --- Optional web dashboard ---
DASHBOARD_ENABLED=0
DASHBOARD_PORT=8080
DASHBOARD_BASE_URL=http://localhost:8080
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
DASHBOARD_SECRET_KEY=
```

#### `.env` variable reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | ✅ | — | Bot token from the Discord Developer Portal → Bot tab |
| `DISCORD_GUILD_ID` | — | — | Comma-separated server ID(s) for fast guild-scoped command sync (recommended during setup) |
| `DISCORD_CLEAR_GLOBAL_COMMANDS` | — | `0` | Set to `1` once to remove stale global slash commands, then back to `0` |
| `BGA_POLL_SECONDS` | — | `15` | Seconds between monitor scheduler ticks |
| `BGA_DB_PATH` | — | `bga_bot.db` | SQLite database file path |
| `BGA_WS_URL` | — | `wss://ws-x1.boardgamearena.com/connection/websocket` | Public BGA websocket URL |
| `BGA_ENABLE_TABLEINFOS_FALLBACK` | — | `0` | Re-enables the legacy HTTP fallback for end-of-game detection |
| `BGA_RECRUITING_ONLY` | — | `0` | Only post the recruiting embed; auto-unwatch when the game starts |
| `BGA_DELETE_INVITE_MESSAGE` | — | blank | Delete the original link message once the table card is showing. Blank: on for servers joined from now on, off for others; `1`/`0`: the default everywhere |
| `BGA_FORCED_CHANNEL_ID` | — | — | Force all bot notifications into one specific channel |
| `LOG_LEVEL` | — | `INFO` | Console log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `BOT_LANG` | — | `en` | Bot message language: the name of a file in `src/bga_turn/locales/` (only `en` ships today) |
| `CODENAMES_ENABLED` | — | `1` | Watch posted codenames.game room links and add the `/codenames` commands |
| `CODENAMES_BOT_NAMES` | — | built-in list | `\|`-separated names the bot picks from when it first joins a room (e.g. `🤖 Agent Turnkeeper\|🤖 Curious Cat`). Avoid brackets: the site strips them |
| `CODENAMES_IDLE_HOURS` | — | `168` | Stop watching a room where nothing has happened (no moves, nobody in it) for this many hours. Turn-based games can go days between moves, hence the 7-day default |
| `BRIDGE_TOKEN` | — | — | Shared secret with the [Codenames tournament bot](docs/tournament-bridge.md); setting it turns the bridge on |
| `BRIDGE_HOST` / `BRIDGE_PORT` | — | `0.0.0.0` / `8081` | Where the tournament bridge API listens |
| `BRIDGE_CALLBACK_URLS` | — | — | Comma-separated result URLs on the tournament bot, tried in order (internal first, public HTTPS fallback) |
| `DASHBOARD_ENABLED` | — | `0` | Enable the optional web dashboard on `DASHBOARD_PORT` |
| `DASHBOARD_HOST` | — | `0.0.0.0` | Interface the web dashboard listens on (use `127.0.0.1` behind a local reverse proxy) |
| `DASHBOARD_PORT` | — | `8080` | Port for the web dashboard |
| `DASHBOARD_BASE_URL` | — | `http://localhost:8080` | Public base URL for dashboard OAuth2 redirect (must be HTTPS in production) |
| `DISCORD_CLIENT_ID` | — | — | Discord OAuth2 client ID (required when dashboard is enabled) |
| `DISCORD_CLIENT_SECRET` | — | — | Discord OAuth2 client secret (required when dashboard is enabled) |
| `DASHBOARD_SECRET_KEY` | — | — | Random secret used to sign dashboard form tokens (required when dashboard is enabled) |
| `GLOBAL_ADMIN_DISCORD_IDS` | — | — | Comma-separated Discord user IDs that can view/manage all bot servers in the dashboard |

---

### Step 5 — Run the bot

#### Recommended

```bash
python -m bga_turn
```

or

```bash
bga-turn-bot
```

#### Optional development launcher

```bash
python bot.py
```

If `DISCORD_GUILD_ID` is set (single server ID or comma-separated list), slash commands are synced to each of those guilds. Otherwise, they are synced globally (may take up to an hour to appear).

If you previously used global slash commands and now see duplicates, set `DISCORD_CLEAR_GLOBAL_COMMANDS=1` for one startup, then set it back to `0`.

---

### Docker deployment details

The project ships with a `Dockerfile` and a `docker-compose.yml` for a single-container deployment:

- `restart: unless-stopped` — auto-starts with Docker and restarts on crash.
- `init: true` — correct signal forwarding and zombie-process reaping.
- The SQLite database is stored in a named volume `bga_data` (mounted at `/data`). You do **not** need to set `BGA_DB_PATH` in `.env`; the image defaults to `/data/bga_bot.db`.

---

### License

This repository is distributed under the MIT license. See `LICENSE`.

## 2. How the bot works

### Automatic link detection (primary feature)

The bot listens for messages in all channels it can see. Any Board Game Arena table link is recognised, including:

- game pages (`https://boardgamearena.com/1/azul?table=12345`), `/table?table=`, `/tableview?table=`, review and replay pages;
- short links (`https://bga.li/t/12345`) and language subdomains (`fr.boardgamearena.com`);
- links without `https://`, links wrapped in Discord formatting (`||spoiler||`, `**bold**`, `` `code` ``, `<no embed>`), and links in forwarded messages;
- links added later by editing a message. Only links that are new in the edit count, so fixing a typo or Discord adding a link preview changes nothing, and an edit never moves the card of a table the channel already watches.

As soon as a link is seen, the bot replies with a status message, **"Board Game Arena link detected – registering table…"**. Then:

1. It anonymously fetches the public table page to identify the game and opens a public websocket to follow it.
2. **If that works**, the status message turns into the table card (see below), so each table only ever has one message. If the server's *Tidy up shared links* setting is on, the original message with the link is deleted once the card is showing. Re-posting a link for a table that's already watched moves its card down to the new message.
3. **If something goes wrong**, the status message says why: the game has already finished, the table is private or no longer exists, Board Game Arena is down or rate-limiting, etc. The original message is kept.
4. If Board Game Arena takes more than ~20 seconds to respond, the status message says it is still waiting; it updates by itself when the details arrive.

To keep one person or message from flooding a channel, the bot tracks at most **5 table links per message** and **10 per person every 10 minutes**; extra links are ignored and a short note (removed after a minute) says so.

When the server uses a forced notification channel, the status message stays in the channel where the link was posted and points to the channel the card is in, then removes itself after two minutes.

This requires the **Message Content Intent** to be enabled in the Discord Developer Portal (see Step 1).

### One message per table, three lifecycle states

For each watched table the bot maintains exactly one Discord message and updates it in place as the game progresses:

- **State 1 — Recruiting**: shows game name, players who have joined, open seats, and a **Join** button. A **Link your BGA & Discord** button lets any server member self-link their account.
- **State 2 — In Progress**: shows whose turn it is. If that player is linked to a Discord account, they are @mentioned.
- **State 3 — Finished**: shows the final result (winner or standings when available). The watch is automatically removed.

### Self-service player linking

Players can link themselves without an admin by clicking the **Link your BGA & Discord** button on any recruiting message. A modal appears asking for their BGA username or numeric player ID. Clicking the button again when already linked offers **Change** and **Unlink**.

Each BGA account can be linked to only one Discord user. Linking a name or ID that someone else already holds is refused (two holders would leave the bot unable to tell whom to mention). A server admin can free it by unlinking the other person if they are in the same server; otherwise a global admin can reassign it from the dashboard.

### Player link enrichment

When the bot sees a player act on a watched table it automatically fills in any missing BGA name or ID for linked Discord members. You can link someone with just a name or just an ID, and the bot completes the other field over time.

### Codenames (codenames.game)

[codenames.game](https://codenames.game) works as a live game, but nothing tells you when it's your turn. The bot fills that gap so a game can run over hours or days.

1. **Post a room link** such as `https://codenames.game/r/juzaf-hitos`. The bot reacts 🕵️, joins the room as a **spectator** and posts a **"Game 1"** card. It never joins a team and never sees the key (card colours stay hidden from spectators until they're revealed).
2. **Say who's playing.** Each player opens the room, enters a name, then presses **🙋 I'm playing** on the card and picks that name from a private list. Or tag people with `/codenames players`: they get one invite ping and appear on the card until they claim a name. The bot remembers each person's last name and suggests it next time, and tagged players whose usual name is in the room are matched automatically.
3. **Play.** The card shows the teams, cards left, the current clue, the last guess and whose move it is (👉). When the turn changes the bot sends a short ping as a reply to the card: the team's **spymaster** when a clue is needed, then its **operatives** with the clue. The previous ping is deleted, guesses within a turn don't ping again, and a restart never pings twice.
4. **See the board.** When a game ends, the card gets a picture of the final board. It shows every card in its colour (guessed cards solid and numbered in the order they were picked, unguessed ones outlined), the teams, every clue with the guesses made on it, and how many agents each team found.
5. **Play again.** When the room starts a new game, the finished card keeps its result and picture and a new **"Game 2"** card is posted.

Things to know:

- The bot shows up in the room's player list with a fun name starting with 🤖 (one per room, kept for that room). Leave it off the teams.
- Only classic Codenames is supported for now; for a Duet room the bot posts a short note in the channel and doesn't watch it.
- The server disconnects everyone from a room that's been idle for a while; the bot just rejoins a minute later, so long gaps between turns are fine. It only stops watching, with a note on the card, when nothing has happened in the room for `CODENAMES_IDLE_HOURS` (7 days by default) or it can't reach the room any more. Post the link again to restart.
- Codenames links are never deleted, even where *Tidy up shared links* is on; the server's forced notification channel is used for the card.
- Seats on the site have no accounts, so a Discord link belongs to a seat in one room. It survives renames and rematches in that room.
- Turn pings are separate messages because Discord doesn't notify mentions in edited messages; the card itself is posted with mentions turned off.
- With the **Manage Webhooks** permission, cards and pings are posted by a "Codenames" webhook the bot creates in the channel (with its own avatar), so they don't appear under the bot's name. The buttons still work. Without it, the bot posts them itself and pings start with "🕵️ Codenames". Webhook messages can't be replies, so in that mode pings aren't attached to the card; they have their own **Open room** button.

### Codenames tournaments

The bot can feed results to the [Codenames Tournament bot](https://github.com/Stayingfalse/CodwellTornement). The tournament binds each match thread to its match. Players post their room link in the thread. The card shows each game's expected seats (✅ or ⚠️ as people sit down) and matches seats to players by name. Every finished game is reported back with its full clue history and scored automatically, with a confirm button when the seats don't match the line-up. Set `BRIDGE_TOKEN` and `BRIDGE_CALLBACK_URLS`. Setup and the API are in [docs/tournament-bridge.md](docs/tournament-bridge.md).

## 3. Discord commands

BGA commands are under the `/bga` group; Codenames commands are under [`/codenames`](#codenames).

### `/bga link-member`

Manually link a Discord member to a BGA player. Requires `Manage Server` or `Administrator`.

```text
/bga link-member @Member Haurrus 91713763
```

- `bga_player_name` and `bga_player_id` are both optional — provide at least one.
- The BGA player ID is the number in the BGA profile URL: `https://boardgamearena.com/player?id=91713763` → ID is `91713763`.
- If only the name is provided, the bot fills in the ID automatically the first time it sees that player act on a watched table.

### `/bga unlink-member`

Remove the BGA link for a Discord member. Requires `Manage Server` or `Administrator`.

```text
/bga unlink-member @Member
```

### `/bga status`

Show the last known state of all watched tables on the current server (ephemeral — only visible to you).

```text
/bga status
```

Displays for each watch: the table ID, game name, channel, whose turn it is (BGA player IDs), and the interpreted state.

### `/bga stats`

Post a compact multi-page stats card in the channel for everyone to see, for the current server or, with `global:true`, bot-wide totals. Nobody is pinged by the card. Only the person who ran the command turns its pages; anyone else who clicks a page button gets their own private copy of that page. The buttons are removed after five minutes. Global stats only show totals and game titles: player, recruiter and channel rankings mix every server's data, so they are only shown per server.

```text
/bga stats
/bga stats global:True
```

The stats view uses Discord component cards and page buttons to surface highlights such as:

- total tracked games, live tables, and recruiting tables
- linked accounts
- most played games and most active players
- most active recruiters and busiest channels
- recent activity and outcome breakdowns

### `/bga settings`

View or change server-wide bot settings. Requires `Manage Server` or `Administrator`.

Run with no arguments to see current settings:

```text
/bga settings
```

| Option | Type | Description |
|---|---|---|
| `recruiting_only` | bool | Only post the recruiting embed; auto-unwatch when the game moves to in-progress |
| `delete_invite_message` | bool | Delete the original message containing a BGA link after the watch is registered (requires Manage Messages) |
| `forced_channel` | channel | Route all bot notifications into this channel, regardless of where links are posted |
| `clear_forced_channel` | bool | Remove the forced channel setting |

Examples:

```text
/bga settings recruiting_only:True
/bga settings delete_invite_message:True forced_channel:#bga-notifications
/bga settings clear_forced_channel:True
```

### `/codenames`

| Command | Who | What it does |
|---|---|---|
| `/codenames players player1:@a [player2 … player5] [room]` | anyone | Asks up to five members to play the room watched in this channel. They get one ping with the room link and are listed on the card until they press **I'm playing**. |
| `/codenames status` | anyone | Privately lists the rooms watched in this server, which game each is on, and a summary of the server's games. |
| `/codenames unwatch [room]` | whoever posted the room, or Manage Messages | Stops watching a room in this channel. |

`room` takes a room link or ID and is only needed when a channel watches several rooms.

## 4. Full setup walkthrough

### Minimal — just auto-watch links

1. Create the Discord bot, enable Message Content Intent, invite it to your server (see [Setup](#1-setup)).
2. Configure `.env` with `DISCORD_TOKEN` and optionally `DISCORD_GUILD_ID`.
3. Start the bot with `python -m bga_turn` (or Docker).
4. Post a BGA game link in any channel the bot can see. The bot starts watching it automatically.

### With player mentions

1. Complete the steps above.
2. Link Discord members to their BGA accounts:
   ```text
   /bga link-member @MrHaurrus Haurrus 91713763
   ```
   Or let players self-link by clicking the **Link your BGA & Discord** button on the recruiting message.
3. From now on, when it is a linked player's turn, the bot @mentions them in the status message.

### With a dedicated notifications channel

If you want all game notifications in one place (e.g. `#bga-notifications`) regardless of where links are posted:

```text
/bga settings forced_channel:#bga-notifications
```

## 5. Optional web dashboard

The bot includes an optional web dashboard that lets server admins manage settings through a browser interface with Discord OAuth2 login.

### Enable the dashboard

Add the following to your `.env`:

```env
DASHBOARD_ENABLED=1
DASHBOARD_PORT=8080
DASHBOARD_BASE_URL=https://your-domain.com   # must be HTTPS in production
DISCORD_CLIENT_ID=your_discord_app_client_id
DISCORD_CLIENT_SECRET=your_discord_app_client_secret
DASHBOARD_SECRET_KEY=your_random_secret      # python -c "import secrets; print(secrets.token_hex(32))"
GLOBAL_ADMIN_DISCORD_IDS=123456789012345678  # optional comma-separated Discord user IDs
```

To get `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`:
1. Open your application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. The **Client ID** is shown on the **General Information** page.
3. Go to **OAuth2** and generate or copy the **Client Secret**.
4. Under **OAuth2 → Redirects**, add `https://your-domain.com/auth/callback`.

The dashboard is then accessible at `http://localhost:8080` (or your configured base URL).

### What the dashboard offers

- **My servers**: every server where you have **Manage Server**, plus one-click "Add bot" links for the ones without it.
- **Overview & settings**: recruiting-only mode, link clean-up, and a picker for the channel that receives all updates.
- **Tables**: recruiting, in-progress and recently finished tables, whose turn it is, and a "Stop watching" button; watched Codenames rooms and their finished games are listed too.
- **Member links**: link Discord members to BGA players (including players the bot has seen at your tables but can't match yet), or remove links.
- **Stats**: per-server charts and leaderboards, plus a Codenames section (games, wins per team, most played rooms). BGA stats never include Codenames games. The public global stats page (and the home page) show totals and game titles only; global admins also see the cross-server player leaderboard. Public global stats are cached for one minute.

### Security model

- Access to a server's pages is checked live with Discord (cached for one minute), so removing someone's **Manage Server** permission takes effect almost immediately.
- Links are global (one per Discord user), so the dashboard only lets an admin link or unlink people who are members of the server they're managing, and refuses a BGA account another Discord user already holds (only global admins can reassign one).
- The forced notification channel must be a text channel in the same server; the bot ignores any other value.
- Every form carries a CSRF token signed with `DASHBOARD_SECRET_KEY`, and pages are served with a strict Content-Security-Policy.
- The dashboard does **not** need the privileged Server Members Intent: member search and lookups use on-demand Discord queries.

## 6. Technical overview

### High-level architecture

The bot has three layers:
- **Discord** — slash commands, message events, and message publishing
- **SQLite** — persistence for player links, watches, and guild settings
- **BGA public API** — anonymous table page bootstrap and public websocket connection
- **codenames.game** — a Socket.IO connection per watched room, as a spectator

### BGA network flow

The bot does not use cookies, browser sessions, or a BGA account.

1. **Load the public table page** — downloads the table URL and extracts: anonymous spectator identity (`user_id`, `archivemask`), known player names from the HTML bootstrap, and the initial game state when available.

2. **Open the public websocket** (`wss://ws-x1.boardgamearena.com/connection/websocket`) — replays the BGA/Centrifugo handshake (`connect`, `subscribe bgamsg`, `subscribe /table/t<TABLE_ID>`, etc.).

3. **Interpret events** — reconstructs `waiting_ids` from websocket events in priority order: `gameStateMultipleActiveUpdate`, `gameStateChange.active_player`, `yourturnack`, then limited public heuristics. Detects game-over from `tableInfosChanged` status or `tableDestroy` signals.

### Codenames network flow

The site runs on [boardgame.io](https://boardgame.io) behind Socket.IO. The bot makes up its own credentials for each room (the site does the same in a browser), asks the lobby for the room, follows its redirect to the game server holding it, and then receives the full state followed by small JSON patches. Every reconnect goes through the lobby again, which also picks up "play again". The protocol, worked out from a recorded game, is described in [`docs/codenames-protocol.md`](docs/codenames-protocol.md).

### Project structure

| Path | Purpose |
|---|---|
| `Dockerfile` | Single-container image for Docker deployment |
| `docker-compose.yml` | Compose service with auto-restart and persistent data volume |
| `bot.py` | Optional development launcher from the repository root |
| `src/bga_turn/app.py` | Application entry point, environment loading, bot startup |
| `src/bga_turn/discord_io.py` | Rate-limit-aware Discord calls (per-channel queues, 429 retries) |
| `src/bga_turn/database.py` | SQLite persistence; `AsyncDatabase` runs it off the event loop |
| `src/bga_turn/dashboard.py` | Optional aiohttp web dashboard |
| `src/bga_turn/models.py` | Shared domain dataclasses |
| `src/bga_turn/utils.py` | JSON helpers, small utilities |
| `src/bga_turn/bga/commands.py` | `/bga` slash commands and the `on_message` link detector |
| `src/bga_turn/bga/client.py` | Public BGA networking, HTML parsing, websocket handling |
| `src/bga_turn/bga/monitor.py` | Watch loop, table workers and Discord message lifecycle |
| `src/bga_turn/bga/cards.py` | Renders the table cards and link status notes (no I/O) |
| `src/bga_turn/bga/views.py` | Persistent "Link your BGA & Discord" button and modal |
| `src/bga_turn/bga/links.py` | BGA table URL parsing |
| `src/bga_turn/bga/models.py` | BGA table and notification dataclasses |
| `src/bga_turn/codenames/commands.py` | `/codenames` commands and room link detection |
| `src/bga_turn/codenames/client.py` | Socket.IO connection to one room as a spectator |
| `src/bga_turn/codenames/state.py` | Follows a match from socket events; whose turn, scores, clue (no I/O) |
| `src/bga_turn/codenames/monitor.py` | One client per room; cards, turn pings, play-again, closing rooms |
| `src/bga_turn/codenames/cards.py` | Renders Codenames cards and turn pings (no I/O) |
| `src/bga_turn/codenames/views.py` | Persistent "I'm playing" button and seat picker |
| `src/bga_turn/codenames/posting.py` | Posts as the "Codenames" webhook, or as the bot without Manage Webhooks |
| `src/bga_turn/static/codenames-avatar.png` | Avatar of the "Codenames" webhook |
| `src/bga_turn/codenames/store.py` | Codenames tables and stats (mixed into `Database`) |
| `src/bga_turn/codenames/identity.py` | Per-room bot credentials and names |
| `src/bga_turn/codenames/jsonpatch.py`, `links.py`, `models.py` | JSON Patch, room URL parsing, dataclasses |
| `docs/codenames-protocol.md` | codenames.game socket protocol notes |
| `src/bga_turn/schema.sql` | Packaged SQLite schema |
| `src/bga_turn/locales/en.json` | Bot messages (see Translations) |
| `pyproject.toml` | Package metadata and console entry point |
| `.env.example` | Local configuration example |

### Important notes and limits

- The bot only works for BGA tables publicly accessible in spectator mode.
- The bot is self-hosted — it must keep running on your machine to keep watching tables.
- The **Message Content Intent** must be enabled in the Discord Developer Portal for automatic link detection to work.
- Without that intent, no links are detected and no tables are auto-watched (the bot starts but does nothing until a table is manually registered through the database).
- Displayed game names come from the BGA slug or public bootstrap and are not always perfectly formatted.
- Codenames support relies on codenames.game's unofficial socket protocol, which can change without notice.

### Translations

Everything the bot says in Discord and in its logs lives in `src/bga_turn/locales/en.json`, looked up with `tr("key", ...)`. English is the only language shipped, and the web dashboard is English-only. To add one, copy `en.json` to e.g. `de.json`, translate the values (keep each `{placeholder}`), and set `BOT_LANG=de`. Any missing key falls back to English, and `pytest` reports missing keys or mismatched placeholders.

### Running the tests

```bash
pip install -e '.[dev]'
pytest
```

The suite covers the dashboard's access controls (permission checks, CSRF, member scoping, channel validation) using fake Discord objects, so it needs no bot token or network access. Codenames tests replay an anonymised recording of a real game (`tests/fixtures/codenames_game.jsonl`) through the client, the monitor and the database.
