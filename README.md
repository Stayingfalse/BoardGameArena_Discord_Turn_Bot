# BGA Discord self-host bot

[Version francaise](README.fr.md)

Self-hosted Discord bot for Board Game Arena.

The bot's primary job is **automatic link detection**: whenever someone posts a BGA game URL in a Discord channel, the bot immediately starts watching that table and posts a live status message that it keeps up to date throughout the game.

No BGA account, no password, no cookie — the bot only reads what any anonymous spectator can see.

**How it works:**
1. A player posts a BGA table link in any Discord channel the bot can read.
2. The bot detects the link and begins watching the table.
3. One message is posted and updated live across three stages: **Recruiting → In Progress → Finished**.
4. When the game ends, the message is marked finished and the watch is removed automatically.

To get Discord mentions in turn notifications, server admins can link Discord members to their BGA accounts with `/bga link-member`. Players can also self-link by clicking the **Link your BGA & Discord** button that appears on the recruiting message.

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
   - `Embed Links`
   - `Read Message History`
   - `Manage Messages` *(needed only if `BGA_DELETE_INVITE_MESSAGE=1`)*
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
BOT_LANG=EN                     # EN or FR

# --- Optional features ---
# Set to 1 to only post the recruiting embed and auto-unwatch when the game starts.
BGA_RECRUITING_ONLY=0

# Set to 1 to delete the original message that contained the BGA invite link
# after the watch is registered. Requires the Manage Messages permission.
BGA_DELETE_INVITE_MESSAGE=0

# Channel ID to route all bot notifications to a single fixed channel,
# regardless of which channel the original link was posted in.
BGA_FORCED_CHANNEL_ID=

# Set to 1 once to delete stale global slash commands before guild sync, then back to 0.
DISCORD_CLEAR_GLOBAL_COMMANDS=0

# Set to 1 to re-enable the legacy tableinfos HTTP fallback for end-of-game detection.
BGA_ENABLE_TABLEINFOS_FALLBACK=0

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
| `BGA_DELETE_INVITE_MESSAGE` | — | `0` | Delete the original link message after registering the watch |
| `BGA_FORCED_CHANNEL_ID` | — | — | Force all bot notifications into one specific channel |
| `LOG_LEVEL` | — | `INFO` | Console log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `BOT_LANG` | — | `EN` | Bot message language (`EN` or `FR`) |
| `DASHBOARD_ENABLED` | — | `0` | Enable the optional web dashboard on `DASHBOARD_PORT` |
| `DASHBOARD_PORT` | — | `8080` | Port for the web dashboard |
| `DASHBOARD_BASE_URL` | — | `http://localhost:8080` | Public base URL for dashboard OAuth2 redirect (must be HTTPS in production) |
| `DISCORD_CLIENT_ID` | — | — | Discord OAuth2 client ID (required when dashboard is enabled) |
| `DISCORD_CLIENT_SECRET` | — | — | Discord OAuth2 client secret (required when dashboard is enabled) |
| `DASHBOARD_SECRET_KEY` | — | — | Random secret for dashboard auth state validation (required when dashboard is enabled) |

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

The bot listens for messages in all channels it can see. When a message contains a BGA table URL (e.g. `https://boardgamearena.com/...?table=12345`), the bot:

1. Extracts the table ID from the URL.
2. Anonymously fetches the public table page to identify the game.
3. Opens a public websocket connection to the BGA table.
4. Posts a status message in the same channel and keeps it updated.

This requires the **Message Content Intent** to be enabled in the Discord Developer Portal (see Step 1).

### One message per table, three lifecycle states

For each watched table the bot maintains exactly one Discord message and updates it in place as the game progresses:

- **State 1 — Recruiting**: shows game name, players who have joined, open seats, and a **Join** button. A **Link your BGA & Discord** button lets any server member self-link their account.
- **State 2 — In Progress**: shows whose turn it is. If that player is linked to a Discord account, they are @mentioned.
- **State 3 — Finished**: shows the final result (winner or standings when available). The watch is automatically removed.

### Self-service player linking

Players can link themselves without an admin by clicking the **Link your BGA & Discord** button on any recruiting message. A modal appears asking for their BGA username or numeric player ID.

### Player link enrichment

When the bot sees a player act on a watched table it automatically fills in any missing BGA name or ID for linked Discord members. You can link someone with just a name or just an ID, and the bot completes the other field over time.

## 3. Discord commands

All commands are under the `/bga` group.

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
```

To get `DISCORD_CLIENT_ID` and `DISCORD_CLIENT_SECRET`:
1. Open your application in the [Discord Developer Portal](https://discord.com/developers/applications).
2. The **Client ID** is shown on the **General Information** page.
3. Go to **OAuth2** and generate or copy the **Client Secret**.
4. Under **OAuth2 → Redirects**, add `https://your-domain.com/auth/callback`.

The dashboard is then accessible at `http://localhost:8080` (or your configured base URL).

## 6. Technical overview

### High-level architecture

The bot has three layers:
- **Discord** — slash commands, message events, and message publishing
- **SQLite** — persistence for player links, watches, and guild settings
- **BGA public API** — anonymous table page bootstrap and public websocket connection

### BGA network flow

The bot does not use cookies, browser sessions, or a BGA account.

1. **Load the public table page** — downloads the table URL and extracts: anonymous spectator identity (`user_id`, `archivemask`), known player names from the HTML bootstrap, and the initial game state when available.

2. **Open the public websocket** (`wss://ws-x1.boardgamearena.com/connection/websocket`) — replays the BGA/Centrifugo handshake (`connect`, `subscribe bgamsg`, `subscribe /table/t<TABLE_ID>`, etc.).

3. **Interpret events** — reconstructs `waiting_ids` from websocket events in priority order: `gameStateMultipleActiveUpdate`, `gameStateChange.active_player`, `yourturnack`, then limited public heuristics. Detects game-over from `tableInfosChanged` status or `tableDestroy` signals.

### Project structure

| Path | Purpose |
|---|---|
| `Dockerfile` | Single-container image for Docker deployment |
| `docker-compose.yml` | Compose service with auto-restart and persistent data volume |
| `bot.py` | Optional development launcher from the repository root |
| `src/bga_turn/app.py` | Application entry point, environment loading, bot startup |
| `src/bga_turn/commands_bga.py` | `/bga` slash commands and the `on_message` link detector |
| `src/bga_turn/bga_client.py` | Public BGA networking, HTML parsing, websocket handling |
| `src/bga_turn/monitor.py` | Watch loop, Discord message lifecycle, followed-player sync |
| `src/bga_turn/database.py` | SQLite persistence |
| `src/bga_turn/dashboard.py` | Optional aiohttp web dashboard |
| `src/bga_turn/models.py` | Domain dataclasses |
| `src/bga_turn/utils.py` | URL parsing, JSON helpers, small utilities |
| `src/bga_turn/schema.sql` | Packaged SQLite schema |
| `pyproject.toml` | Package metadata and console entry point |
| `.env.example` | Local configuration example |

### Important notes and limits

- The bot only works for BGA tables publicly accessible in spectator mode.
- The bot is self-hosted — it must keep running on your machine to keep watching tables.
- The **Message Content Intent** must be enabled in the Discord Developer Portal for automatic link detection to work.
- Without that intent, no links are detected and no tables are auto-watched (the bot starts but does nothing until a table is manually registered through the database).
- Displayed game names come from the BGA slug or public bootstrap and are not always perfectly formatted.
- The project ships without a unit test suite; validation is kept lightweight through packaging and compilation checks.
