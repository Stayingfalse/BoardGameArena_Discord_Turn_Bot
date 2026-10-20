PRAGMA foreign_keys = ON;

-- Discord/BGA bindings are global: one link per Discord user across all guilds.
CREATE TABLE IF NOT EXISTS users (
    discord_user_id TEXT NOT NULL,
    bga_player_id TEXT NOT NULL,
    bga_player_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (discord_user_id)
);

-- Per-guild settings that override the global env-var defaults.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT NOT NULL PRIMARY KEY,
    recruiting_only INTEGER NOT NULL DEFAULT 0,
    delete_invite_message INTEGER NOT NULL DEFAULT 0,
    forced_channel_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_subscriptions (
    subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id TEXT NOT NULL,
    table_url TEXT,
    base_url TEXT,
    gameserver TEXT,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    created_by_discord_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(table_id, guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS watch_states (
    subscription_id INTEGER PRIMARY KEY,
    last_packet_id INTEGER NOT NULL DEFAULT 1,
    last_waiting_ids TEXT NOT NULL DEFAULT '[]',
    last_player_names TEXT NOT NULL DEFAULT '{}',
    seated_player_names TEXT NOT NULL DEFAULT '{}',
    seats_total INTEGER,
    seats_remaining INTEGER,
    is_initialized INTEGER NOT NULL DEFAULT 0,
    game_name TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'recruiting',
    game_started_at TEXT,
    winner_names TEXT NOT NULL DEFAULT '[]',
    final_standings TEXT NOT NULL DEFAULT '[]',
    player_count INTEGER,
    tracked_message_id TEXT,
    tracked_message_kind TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (subscription_id) REFERENCES watch_subscriptions(subscription_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS game_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id TEXT NOT NULL,
    game_name TEXT,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    created_by_discord_user_id TEXT NOT NULL,
    recruiting_started_at TEXT NOT NULL,
    game_started_at TEXT,
    finished_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('finished', 'cancelled', 'unwatched')),
    winner_names TEXT NOT NULL DEFAULT '[]',
    final_standings TEXT NOT NULL DEFAULT '[]',
    player_count INTEGER,
    provider TEXT NOT NULL DEFAULT 'bga'
);

CREATE TABLE IF NOT EXISTS dashboard_sessions (
    session_id TEXT NOT NULL PRIMARY KEY,
    user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    avatar TEXT,
    guilds_json TEXT NOT NULL DEFAULT '[]',
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_watch_subscriptions_table_id ON watch_subscriptions(table_id);

-- Codenames (codenames.game). Rooms have no accounts, so the bot joins each
-- room with its own credentials and a display name, kept per room so it gets
-- the same seat back after a restart. A room runs one match at a time; "play
-- again" starts a new match in the same room, which is the next "Game N".
CREATE TABLE IF NOT EXISTS codenames_rooms (
    room_id TEXT NOT NULL PRIMARY KEY,
    bot_credentials TEXT NOT NULL,
    bot_name TEXT NOT NULL,
    bot_player_id TEXT,
    current_match_id TEXT,
    game_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codenames_watches (
    watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    created_by_discord_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Thread the turn pings go to when the card is in an ordinary channel (NULL: the channel itself).
    ping_thread_id TEXT,
    UNIQUE(room_id, guild_id, channel_id),
    FOREIGN KEY (room_id) REFERENCES codenames_rooms(room_id) ON DELETE CASCADE
);

-- One Discord message per match per watch.
CREATE TABLE IF NOT EXISTS codenames_games (
    game_id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER NOT NULL,
    match_id TEXT NOT NULL,
    game_number INTEGER NOT NULL,
    message_id TEXT,
    lifecycle_state TEXT NOT NULL DEFAULT 'lobby' CHECK (lifecycle_state IN ('lobby', 'in_progress', 'finished')),
    -- "<team>:<role>" of the last turn we pinged, so a restart never pings twice.
    turn_key TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    winner_team TEXT,
    finish_reason TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(watch_id, match_id),
    FOREIGN KEY (watch_id) REFERENCES codenames_watches(watch_id) ON DELETE CASCADE
);

-- In-room seats. Seat IDs are stable for the room (they survive renames and
-- "play again"), so a Discord link is kept on the seat, not the name.
CREATE TABLE IF NOT EXISTS codenames_players (
    room_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    display_name TEXT,
    discord_user_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, player_id),
    FOREIGN KEY (room_id) REFERENCES codenames_rooms(room_id) ON DELETE CASCADE
);

-- Seats a player said are another device of theirs ("That's also me"), on top of
-- the one seat they claim in codenames_players.
CREATE TABLE IF NOT EXISTS codenames_devices (
    room_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    discord_user_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, player_id),
    FOREIGN KEY (room_id) REFERENCES codenames_rooms(room_id) ON DELETE CASCADE
);

-- Discord members tagged as expected players for a watched room.
CREATE TABLE IF NOT EXISTS codenames_watch_members (
    watch_id INTEGER NOT NULL,
    discord_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (watch_id, discord_user_id),
    FOREIGN KEY (watch_id) REFERENCES codenames_watches(watch_id) ON DELETE CASCADE
);

-- The display name each Discord user last claimed, offered as a suggestion.
CREATE TABLE IF NOT EXISTS codenames_user_names (
    discord_user_id TEXT NOT NULL PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codenames_watches_guild ON codenames_watches(guild_id);
CREATE INDEX IF NOT EXISTS idx_codenames_games_message ON codenames_games(message_id);
CREATE INDEX IF NOT EXISTS idx_codenames_players_discord ON codenames_players(discord_user_id);

-- Tournament bridge: a Discord channel (a tournament match thread) that an
-- external tournament bot has bound to one of its matches. Codenames rooms
-- posted in that channel report each finished game back to the tournament.
CREATE TABLE IF NOT EXISTS codenames_bindings (
    channel_id TEXT NOT NULL PRIMARY KEY,
    guild_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    -- JSON list of the Discord user IDs expected to play.
    players_json TEXT NOT NULL DEFAULT '[]',
    -- JSON: {"player_names": {id: name}, "lineup": {...} or null}.
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Finished-game results waiting to be delivered to the tournament bot. Kept
-- until delivered (or given up on) so a restart or an outage loses nothing.
CREATE TABLE IF NOT EXISTS codenames_result_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'rejected', 'abandoned')),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codenames_result_outbox_due ON codenames_result_outbox(status, next_attempt_at);
