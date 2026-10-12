from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LinkedUser:
    discord_user_id: str
    bga_player_id: str
    bga_player_name: str


@dataclass(slots=True)
class GuildSettings:
    guild_id: str
    recruiting_only: bool
    delete_invite_message: bool
    forced_channel_id: str | None


@dataclass(slots=True)
class WatchSubscription:
    subscription_id: int
    table_id: str
    table_url: str | None
    base_url: str | None
    gameserver: str | None
    guild_id: str
    channel_id: str
    created_by_discord_user_id: str
    last_packet_id: int
    last_waiting_ids: list[str]
    player_names: dict[str, str]
    seated_player_names: dict[str, str]
    seats_total: int | None
    seats_remaining: int | None
    is_initialized: bool
    game_name: str | None
    lifecycle_state: str
    tracked_message_id: int | None
    tracked_message_kind: str | None
    recruiting_started_at: str | None = None


@dataclass(slots=True)
class GameHistoryEntry:
    history_id: int
    table_id: str
    game_name: str | None
    guild_id: str
    channel_id: str
    created_by_discord_user_id: str
    recruiting_started_at: str
    game_started_at: str | None
    finished_at: str
    outcome: str
    winner_names: list[str]
    final_standings: list[str]
    player_count: int | None
    provider: str = "bga"
