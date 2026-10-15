"""SQLite persistence for Codenames rooms, watches, games and players.

Mixed into :class:`bga_turn.database.Database`, which provides ``_lock`` and
``_connection``; :class:`bga_turn.database.AsyncDatabase` wraps these methods
like any other.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from ..utils import json_dumps, utc_now_iso
from .models import CodenamesGame, CodenamesPlayer, CodenamesRoom, CodenamesWatch

PROVIDER = "codenames"
GAME_NAME = "Codenames"

_LIFECYCLE_STATES = {"lobby", "in_progress", "finished"}
_UNSET = object()


class CodenamesSeatTakenError(Exception):
    """The in-room seat is already claimed by another Discord user."""

    def __init__(self, holder_discord_user_id: str) -> None:
        super().__init__(holder_discord_user_id)
        self.holder_discord_user_id = holder_discord_user_id


class CodenamesStore:
    _lock: threading.RLock
    _connection: sqlite3.Connection

    # ------------------------------------------------------------------
    # Rooms
    # ------------------------------------------------------------------

    def get_codenames_room(self, room_id: str) -> CodenamesRoom | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM codenames_rooms WHERE room_id = ?", (room_id,)).fetchone()
        return _row_to_room(row) if row is not None else None

    def get_or_create_codenames_room(self, room_id: str, *, bot_credentials: str, bot_name: str) -> CodenamesRoom:
        """Return the room, creating it with this bot identity if it is new.

        An existing room keeps its stored identity, so the bot reclaims the
        same seat and name after a restart.
        """
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO codenames_rooms (room_id, bot_credentials, bot_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (room_id, bot_credentials, bot_name, now, now),
            )
            self._connection.commit()
            room = self.get_codenames_room(room_id)
        assert room is not None
        return room

    def record_codenames_match(self, room_id: str, match_id: str, bot_player_id: str | None) -> tuple[int, bool]:
        """Note the room's current match; return ``(game_number, is_new_match)``.

        A match ID not seen before in this room (the first game, or "play
        again") becomes the next game number.
        """
        with self._lock:
            room = self.get_codenames_room(room_id)
            if room is None:
                raise KeyError(room_id)
            is_new = room.current_match_id != match_id
            game_count = room.game_count + 1 if is_new else room.game_count
            self._connection.execute(
                """
                UPDATE codenames_rooms
                SET current_match_id = ?, game_count = ?, bot_player_id = COALESCE(?, bot_player_id), updated_at = ?
                WHERE room_id = ?
                """,
                (match_id, game_count, bot_player_id, utc_now_iso(), room_id),
            )
            self._connection.commit()
        return game_count, is_new

    # ------------------------------------------------------------------
    # Watches
    # ------------------------------------------------------------------

    def add_codenames_watch(
        self, room_id: str, *, guild_id: str, channel_id: str, created_by_discord_user_id: str
    ) -> tuple[CodenamesWatch, bool]:
        """Watch *room_id* in a channel; return ``(watch, created)``. The room must exist."""
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO codenames_watches (room_id, guild_id, channel_id, created_by_discord_user_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (room_id, guild_id, channel_id, created_by_discord_user_id, utc_now_iso()),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM codenames_watches WHERE room_id = ? AND guild_id = ? AND channel_id = ?",
                (room_id, guild_id, channel_id),
            ).fetchone()
        return _row_to_watch(row), cursor.rowcount > 0

    def get_codenames_watch(self, watch_id: int) -> CodenamesWatch | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM codenames_watches WHERE watch_id = ?", (watch_id,)).fetchone()
        return _row_to_watch(row) if row is not None else None

    def _ensure_codenames_watch_columns(self) -> None:
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(codenames_watches)")}
        if "ping_thread_id" not in columns:
            self._connection.execute("ALTER TABLE codenames_watches ADD COLUMN ping_thread_id TEXT")

    def set_codenames_ping_thread(self, watch_id: int, thread_id: str | None) -> None:
        """Remember (or forget, with ``None``) the thread a watch's turn pings go to."""
        with self._lock:
            self._connection.execute(
                "UPDATE codenames_watches SET ping_thread_id = ? WHERE watch_id = ?", (thread_id, watch_id)
            )
            self._connection.commit()

    def list_codenames_watches(self, room_id: str | None = None) -> list[CodenamesWatch]:
        with self._lock:
            if room_id is None:
                rows = self._connection.execute("SELECT * FROM codenames_watches ORDER BY watch_id").fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT * FROM codenames_watches WHERE room_id = ? ORDER BY watch_id", (room_id,)
                ).fetchall()
        return [_row_to_watch(row) for row in rows]

    def list_codenames_watches_for_guild(self, guild_id: str) -> list[CodenamesWatch]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM codenames_watches WHERE guild_id = ? ORDER BY watch_id", (guild_id,)
            ).fetchall()
        return [_row_to_watch(row) for row in rows]

    def remove_codenames_watch(self, watch_id: int, *, outcome: str = "unwatched") -> bool:
        """Stop watching. A game still in progress is archived with *outcome*;
        lobby-only games are dropped, as no game was played."""
        with self._lock:
            watch = self.get_codenames_watch(watch_id)
            if watch is None:
                return False
            game = self.get_latest_codenames_game(watch_id)
            if game is not None and game.lifecycle_state == "in_progress":
                self._archive_codenames_game_locked(watch, game, outcome=outcome, finished_at=utc_now_iso())
            self._connection.execute("DELETE FROM codenames_watches WHERE watch_id = ?", (watch_id,))
            self._connection.commit()
        return True

    def remove_all_codenames_watches_for_guild(self, guild_id: str, *, outcome: str = "cancelled") -> int:
        with self._lock:
            watches = self.list_codenames_watches_for_guild(guild_id)
            for watch in watches:
                self.remove_codenames_watch(watch.watch_id, outcome=outcome)
        return len(watches)

    # ------------------------------------------------------------------
    # Tagged members ("/codenames players @a @b")
    # ------------------------------------------------------------------

    def add_codenames_watch_members(self, watch_id: int, discord_user_ids: list[str]) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.executemany(
                "INSERT OR IGNORE INTO codenames_watch_members (watch_id, discord_user_id, created_at) VALUES (?, ?, ?)",
                [(watch_id, user_id, now) for user_id in discord_user_ids],
            )
            self._connection.commit()

    def list_codenames_watch_members(self, watch_id: int) -> list[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT discord_user_id FROM codenames_watch_members WHERE watch_id = ? ORDER BY created_at, discord_user_id",
                (watch_id,),
            ).fetchall()
        return [row["discord_user_id"] for row in rows]

    def remove_codenames_watch_member(self, watch_id: int, discord_user_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM codenames_watch_members WHERE watch_id = ? AND discord_user_id = ?",
                (watch_id, discord_user_id),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Games (one Discord message per match per watch)
    # ------------------------------------------------------------------

    def get_or_create_codenames_game(self, watch_id: int, match_id: str, game_number: int) -> tuple[CodenamesGame, bool]:
        now = utc_now_iso()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO codenames_games (watch_id, match_id, game_number, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (watch_id, match_id, game_number, now, now),
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT * FROM codenames_games WHERE watch_id = ? AND match_id = ?", (watch_id, match_id)
            ).fetchone()
        return _row_to_game(row), cursor.rowcount > 0

    def get_codenames_game(self, game_id: int) -> CodenamesGame | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM codenames_games WHERE game_id = ?", (game_id,)).fetchone()
        return _row_to_game(row) if row is not None else None

    def get_codenames_game_by_message(self, message_id: int) -> CodenamesGame | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM codenames_games WHERE message_id = ?", (str(message_id),)
            ).fetchone()
        return _row_to_game(row) if row is not None else None

    def get_latest_codenames_game(self, watch_id: int) -> CodenamesGame | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM codenames_games WHERE watch_id = ? ORDER BY game_number DESC, game_id DESC LIMIT 1",
                (watch_id,),
            ).fetchone()
        return _row_to_game(row) if row is not None else None

    def update_codenames_game(
        self,
        game_id: int,
        *,
        message_id: int | None | object = _UNSET,
        lifecycle_state: str | object = _UNSET,
        turn_key: str | None | object = _UNSET,
        state: dict | object = _UNSET,
        started_at: str | None | object = _UNSET,
    ) -> None:
        """Update only the fields passed."""
        updates: dict[str, object] = {}
        if message_id is not _UNSET:
            updates["message_id"] = str(message_id) if message_id is not None else None
        if lifecycle_state is not _UNSET:
            if lifecycle_state not in _LIFECYCLE_STATES - {"finished"}:
                raise ValueError(f"Use finish_codenames_game to finish; invalid state: {lifecycle_state}")
            updates["lifecycle_state"] = lifecycle_state
        if turn_key is not _UNSET:
            updates["turn_key"] = turn_key
        if state is not _UNSET:
            updates["state_json"] = json_dumps(state)
        if started_at is not _UNSET:
            updates["started_at"] = started_at
        if not updates:
            return
        updates["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{column} = ?" for column in updates)
        with self._lock:
            self._connection.execute(
                f"UPDATE codenames_games SET {assignments} WHERE game_id = ?", (*updates.values(), game_id)
            )
            self._connection.commit()

    def finish_codenames_game(
        self,
        game_id: int,
        *,
        winner_team: str | None,
        finish_reason: str | None,
        player_count: int | None = None,
        finished_at: str | None = None,
    ) -> bool:
        """Mark the game finished and record it in ``game_history``.

        Returns ``False`` if it was already finished, so a restart replaying
        the final state never records the game twice.
        """
        finished_at = finished_at or utc_now_iso()
        with self._lock:
            game = self.get_codenames_game(game_id)
            if game is None or game.lifecycle_state == "finished":
                return False
            watch = self.get_codenames_watch(game.watch_id)
            assert watch is not None
            self._connection.execute(
                """
                UPDATE codenames_games
                SET lifecycle_state = 'finished', winner_team = ?, finish_reason = ?, finished_at = ?, updated_at = ?
                WHERE game_id = ?
                """,
                (winner_team, finish_reason, finished_at, finished_at, game_id),
            )
            game.winner_team = winner_team
            self._archive_codenames_game_locked(
                watch, game, outcome="finished", finished_at=finished_at, player_count=player_count
            )
            self._connection.commit()
        return True

    def _archive_codenames_game_locked(
        self,
        watch: CodenamesWatch,
        game: CodenamesGame,
        *,
        outcome: str,
        finished_at: str,
        player_count: int | None = None,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO game_history (
                table_id, game_name, guild_id, channel_id, created_by_discord_user_id,
                recruiting_started_at, game_started_at, finished_at, outcome,
                winner_names, final_standings, player_count, provider
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                watch.room_id,
                GAME_NAME,
                watch.guild_id,
                watch.channel_id,
                watch.created_by_discord_user_id,
                game.created_at,
                game.started_at,
                finished_at,
                outcome,
                json_dumps([game.winner_team] if outcome == "finished" and game.winner_team else []),
                json_dumps([]),
                player_count,
                PROVIDER,
            ),
        )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_codenames_stats(self, guild_id: str | None = None) -> dict[str, object]:
        """Codenames totals for one server, or every server when *guild_id* is ``None``.

        Only the per-server view lists rooms: a room link lets anyone join it.
        """
        guild_clause, params = ("AND guild_id = ?", (guild_id,)) if guild_id is not None else ("", ())
        with self._lock:
            history = self._connection.execute(
                f"""
                SELECT
                    COUNT(*) AS finished_games,
                    SUM(CASE WHEN winner_names = '["red"]' THEN 1 ELSE 0 END) AS red_wins,
                    SUM(CASE WHEN winner_names = '["blue"]' THEN 1 ELSE 0 END) AS blue_wins,
                    AVG(
                        CASE WHEN game_started_at IS NOT NULL
                        THEN (julianday(finished_at) - julianday(game_started_at)) * 1440 END
                    ) AS avg_game_minutes
                FROM game_history
                WHERE provider = ? AND outcome = 'finished' {guild_clause}
                """,
                (PROVIDER, *params),
            ).fetchone()
            watch_clause = "WHERE w.guild_id = ?" if guild_id is not None else ""
            live = self._connection.execute(
                f"""
                SELECT
                    COUNT(DISTINCT w.room_id) AS watching_rooms,
                    SUM(CASE WHEN g.lifecycle_state = 'in_progress' THEN 1 ELSE 0 END) AS games_in_progress
                FROM codenames_watches w
                LEFT JOIN codenames_games g ON g.game_id = (
                    SELECT game_id FROM codenames_games WHERE watch_id = w.watch_id
                    ORDER BY game_number DESC, game_id DESC LIMIT 1
                )
                {watch_clause}
                """,
                params,
            ).fetchone()
            room_rows = (
                self._connection.execute(
                    """
                    SELECT table_id AS room_id, COUNT(*) AS games
                    FROM game_history
                    WHERE provider = ? AND outcome = 'finished' AND guild_id = ?
                    GROUP BY table_id
                    ORDER BY games DESC, table_id
                    LIMIT 10
                    """,
                    (PROVIDER, guild_id),
                ).fetchall()
                if guild_id is not None
                else []
            )
        avg_minutes = history["avg_game_minutes"]
        return {
            "finished_games": int(history["finished_games"] or 0),
            "red_wins": int(history["red_wins"] or 0),
            "blue_wins": int(history["blue_wins"] or 0),
            "avg_game_minutes": round(float(avg_minutes), 1) if avg_minutes is not None else None,
            "watching_rooms": int(live["watching_rooms"] or 0),
            "games_in_progress": int(live["games_in_progress"] or 0),
            "top_rooms": [{"room_id": row["room_id"], "games": int(row["games"])} for row in room_rows],
        }

    # ------------------------------------------------------------------
    # In-room players
    # ------------------------------------------------------------------

    def update_codenames_player_names(self, room_id: str, names: dict[str, str]) -> None:
        """Store the current display name of each seat. Discord links are kept:
        a seat stays the same person when they rename."""
        now = utc_now_iso()
        with self._lock:
            self._connection.executemany(
                """
                INSERT INTO codenames_players (room_id, player_id, display_name, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(room_id, player_id) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at
                """,
                [(room_id, player_id, name, now) for player_id, name in names.items()],
            )
            self._connection.commit()

    def list_codenames_players(self, room_id: str) -> list[CodenamesPlayer]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM codenames_players WHERE room_id = ? ORDER BY CAST(player_id AS INTEGER), player_id",
                (room_id,),
            ).fetchall()
        return [_row_to_player(row) for row in rows]

    def claim_codenames_player(
        self, room_id: str, player_id: str, discord_user_id: str, *, override: bool = False
    ) -> CodenamesPlayer:
        """Link a seat to a Discord user, releasing any other seat they held in the room.

        Raises :class:`CodenamesSeatTakenError` if someone else holds the seat,
        unless *override* is set. The seat's display name becomes the user's
        remembered name.
        """
        now = utc_now_iso()
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM codenames_players WHERE room_id = ? AND player_id = ?", (room_id, player_id)
            ).fetchone()
            if row is not None and row["discord_user_id"] not in (None, discord_user_id) and not override:
                raise CodenamesSeatTakenError(row["discord_user_id"])
            self._connection.execute(
                "UPDATE codenames_players SET discord_user_id = NULL, updated_at = ? WHERE room_id = ? AND discord_user_id = ?",
                (now, room_id, discord_user_id),
            )
            self._connection.execute(
                """
                INSERT INTO codenames_players (room_id, player_id, discord_user_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(room_id, player_id) DO UPDATE SET discord_user_id = excluded.discord_user_id, updated_at = excluded.updated_at
                """,
                (room_id, player_id, discord_user_id, now),
            )
            player = _row_to_player(
                self._connection.execute(
                    "SELECT * FROM codenames_players WHERE room_id = ? AND player_id = ?", (room_id, player_id)
                ).fetchone()
            )
            if player.display_name:
                self._connection.execute(
                    """
                    INSERT INTO codenames_user_names (discord_user_id, display_name, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(discord_user_id) DO UPDATE SET display_name = excluded.display_name, updated_at = excluded.updated_at
                    """,
                    (discord_user_id, player.display_name, now),
                )
            self._connection.commit()
        return player

    def release_codenames_player(self, room_id: str, discord_user_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE codenames_players SET discord_user_id = NULL, updated_at = ? WHERE room_id = ? AND discord_user_id = ?",
                (utc_now_iso(), room_id, discord_user_id),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def get_codenames_remembered_name(self, discord_user_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT display_name FROM codenames_user_names WHERE discord_user_id = ?", (discord_user_id,)
            ).fetchone()
        return row["display_name"] if row is not None else None


def _row_to_room(row: sqlite3.Row) -> CodenamesRoom:
    return CodenamesRoom(
        room_id=row["room_id"],
        bot_credentials=row["bot_credentials"],
        bot_name=row["bot_name"],
        bot_player_id=row["bot_player_id"],
        current_match_id=row["current_match_id"],
        game_count=int(row["game_count"]),
    )


def _row_to_watch(row: sqlite3.Row) -> CodenamesWatch:
    return CodenamesWatch(
        watch_id=int(row["watch_id"]),
        room_id=row["room_id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        created_by_discord_user_id=row["created_by_discord_user_id"],
        created_at=row["created_at"],
        ping_thread_id=row["ping_thread_id"] if "ping_thread_id" in row.keys() else None,
    )


def _row_to_game(row: sqlite3.Row) -> CodenamesGame:
    try:
        state = json.loads(row["state_json"] or "{}")
    except json.JSONDecodeError:
        state = {}
    return CodenamesGame(
        game_id=int(row["game_id"]),
        watch_id=int(row["watch_id"]),
        match_id=row["match_id"],
        game_number=int(row["game_number"]),
        message_id=int(row["message_id"]) if row["message_id"] else None,
        lifecycle_state=row["lifecycle_state"],
        turn_key=row["turn_key"],
        state=state if isinstance(state, dict) else {},
        winner_team=row["winner_team"],
        finish_reason=row["finish_reason"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
    )


def _row_to_player(row: sqlite3.Row) -> CodenamesPlayer:
    return CodenamesPlayer(
        room_id=row["room_id"],
        player_id=row["player_id"],
        display_name=row["display_name"],
        discord_user_id=row["discord_user_id"],
    )
