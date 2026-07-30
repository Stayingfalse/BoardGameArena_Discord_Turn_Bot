from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class LinkedUser:
    discord_user_id: str
    bga_player_id: str
    bga_player_name: str


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
    is_initialized: bool
    game_name: str | None
    lifecycle_state: str
    tracked_message_id: int | None
    tracked_message_kind: str | None


@dataclass(slots=True)
class FollowedPlayer:
    follow_id: int
    guild_id: str
    discord_user_id: str
    channel_id: str
    created_by_discord_user_id: str


@dataclass(slots=True)
class BgaTableInfo:
    table_id: str
    table_url: str
    base_url: str
    gameserver: str
    game_name: str


@dataclass(slots=True)
class BgaTableSnapshot:
    table_id: str
    status: str
    game_name: str
    gameserver: str
    player_names: dict[str, str] = field(default_factory=dict)
    seats_taken: int = 0
    seats_total: int | None = None
    seats_remaining: int | None = None
    is_finished: bool = False
    can_watch_turns: bool = False
    table_url: str | None = None
    cover_image_url: str | None = None
    player_avatars: dict[str, str] = field(default_factory=dict)
    player_scores: dict[str, str] = field(default_factory=dict)
    player_ranks: dict[str, int] = field(default_factory=dict)
    winner_ids: list[str] = field(default_factory=list)
    winner_names: list[str] = field(default_factory=list)
    final_standings: list[str] = field(default_factory=list)
    finished_at: str | None = None
    finish_reason: str | None = None


@dataclass(slots=True)
class BgaNotificationState:
    highest_packet_id: int | None
    waiting_ids: list[str] | None
    player_names: dict[str, str] = field(default_factory=dict)
    source: str = "unchanged"
    details: dict[str, str] = field(default_factory=dict)
    is_game_finished: bool = False
