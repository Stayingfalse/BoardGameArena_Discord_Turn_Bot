"""Codenames (codenames.game) data models."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CodenamesRoom:
    room_id: str
    bot_credentials: str
    bot_name: str
    bot_player_id: str | None
    current_match_id: str | None
    game_count: int


@dataclass(slots=True)
class CodenamesWatch:
    watch_id: int
    room_id: str
    guild_id: str
    channel_id: str
    created_by_discord_user_id: str
    created_at: str


@dataclass(slots=True)
class CodenamesGame:
    game_id: int
    watch_id: int
    match_id: str
    game_number: int
    message_id: int | None
    lifecycle_state: str
    turn_key: str | None
    state: dict = field(default_factory=dict)
    winner_team: str | None = None
    finish_reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str = ""


@dataclass(slots=True)
class CodenamesPlayer:
    room_id: str
    player_id: str
    display_name: str | None
    discord_user_id: str | None
