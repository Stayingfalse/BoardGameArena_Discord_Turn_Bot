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
    player_count INTEGER
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
