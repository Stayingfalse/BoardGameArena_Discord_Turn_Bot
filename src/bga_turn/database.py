from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import FollowedPlayer, GameHistoryEntry, GuildSettings, LinkedUser, WatchSubscription
from .utils import json_dumps, json_loads_dict, json_loads_list, utc_now_iso


class Database:
    def __init__(self, db_path: Path, schema_sql: str) -> None:
        self.db_path = db_path
        self.schema_sql = schema_sql
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(self.schema_sql)
            self._ensure_watch_subscription_columns()
            self._ensure_watch_state_columns()
            self._migrate_legacy_watches_if_needed()
            self._drop_legacy_tables_if_safe()
            self._migrate_users_to_guild_scope()
            self._migrate_users_to_global()
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------
    # Users (global — one link per Discord user across all guilds)
    # ------------------------------------------------------------------

    def upsert_linked_user(
        self,
        discord_user_id: str,
        bga_player_id: str | None,
        bga_player_name: str | None,
    ) -> None:
        normalized_player_id = (bga_player_id or "").strip()
        normalized_player_name = (bga_player_name or "").strip()
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO users (
                    discord_user_id,
                    bga_player_id,
                    bga_player_name,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET
                    bga_player_id = CASE
                        WHEN excluded.bga_player_id <> '' THEN excluded.bga_player_id
                        ELSE users.bga_player_id
                    END,
                    bga_player_name = CASE
                        WHEN excluded.bga_player_name <> '' THEN excluded.bga_player_name
                        ELSE users.bga_player_name
                    END,
                    updated_at = excluded.updated_at
                """,
                (discord_user_id, normalized_player_id, normalized_player_name, now, now),
            )
            self._connection.commit()

    def get_linked_user(self, discord_user_id: str) -> LinkedUser | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT discord_user_id, bga_player_id, bga_player_name
                FROM users
                WHERE discord_user_id = ?
                """,
                (discord_user_id,),
            ).fetchone()
        if row is None:
            return None
        return LinkedUser(
            discord_user_id=row["discord_user_id"],
            bga_player_id=row["bga_player_id"],
            bga_player_name=row["bga_player_name"],
        )

    def remove_linked_user(self, discord_user_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM users WHERE discord_user_id = ?",
                (discord_user_id,),
            )
            # Remove auto-follows for this user across all guilds/channels.
            self._connection.execute(
                "DELETE FROM followed_players WHERE discord_user_id = ?",
                (discord_user_id,),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def list_linked_users(self) -> list[LinkedUser]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT discord_user_id, bga_player_id, bga_player_name
                FROM users
                ORDER BY bga_player_name COLLATE NOCASE, discord_user_id
                """
            ).fetchall()
        return [
            LinkedUser(
                discord_user_id=row["discord_user_id"],
                bga_player_id=row["bga_player_id"],
                bga_player_name=row["bga_player_name"],
            )
            for row in rows
        ]

    def get_linked_users_by_bga_ids(self, bga_player_ids: list[str]) -> list[LinkedUser]:
        filtered_ids = [item for item in bga_player_ids if item]
        if not filtered_ids:
            return []
        placeholders = ",".join("?" for _ in filtered_ids)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT discord_user_id, bga_player_id, bga_player_name
                FROM users
                WHERE bga_player_id IN ({placeholders})
                ORDER BY bga_player_name COLLATE NOCASE, discord_user_id
                """,
                tuple(filtered_ids),
            ).fetchall()
        return [
            LinkedUser(
                discord_user_id=row["discord_user_id"],
                bga_player_id=row["bga_player_id"],
                bga_player_name=row["bga_player_name"],
            )
            for row in rows
        ]

    def get_linked_users_for_players(
        self, player_names: dict[str, str]
    ) -> list[LinkedUser]:
        if not player_names:
            return []
        linked_users = self.list_linked_users()
        matches: dict[str, LinkedUser] = {}
        for player_id, player_name in player_names.items():
            match = self._find_matching_linked_user(linked_users, player_id, player_name)
            if match is not None:
                matches[match.discord_user_id] = match
        return sorted(matches.values(), key=lambda item: ((item.bga_player_name or "~").casefold(), item.discord_user_id))

    def enrich_linked_users_from_players(
        self, player_names: dict[str, str]
    ) -> int:
        if not player_names:
            return 0

        updated_count = 0
        with self._lock:
            linked_users = self.list_linked_users()
            now = utc_now_iso()
            for player_id, player_name in player_names.items():
                if not player_id and not player_name:
                    continue
                match = self._find_matching_linked_user(linked_users, player_id, player_name)
                if match is None:
                    continue

                new_player_id = match.bga_player_id or player_id
                new_player_name = match.bga_player_name or player_name
                if new_player_id == match.bga_player_id and new_player_name == match.bga_player_name:
                    continue

                self._connection.execute(
                    """
                    UPDATE users
                    SET bga_player_id = ?, bga_player_name = ?, updated_at = ?
                    WHERE discord_user_id = ?
                    """,
                    (new_player_id, new_player_name, now, match.discord_user_id),
                )
                updated_count += 1

                linked_users = [
                    LinkedUser(
                        discord_user_id=item.discord_user_id,
                        bga_player_id=new_player_id if item.discord_user_id == match.discord_user_id else item.bga_player_id,
                        bga_player_name=new_player_name if item.discord_user_id == match.discord_user_id else item.bga_player_name,
                    )
                    for item in linked_users
                ]

            if updated_count:
                self._connection.commit()
        return updated_count

    # ------------------------------------------------------------------
    # Dashboard sessions
    # ------------------------------------------------------------------

    def create_dashboard_session(
        self,
        *,
        session_id: str,
        user_id: str,
        username: str,
        avatar: str | None,
        guilds: list[dict[str, str]],
        ttl_seconds: int,
    ) -> None:
        now = utc_now_iso()
        expires_at = int(time.time()) + max(1, ttl_seconds)
        guilds_json = json_dumps(guilds)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO dashboard_sessions (
                    session_id,
                    user_id,
                    username,
                    avatar,
                    guilds_json,
                    expires_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    username = excluded.username,
                    avatar = excluded.avatar,
                    guilds_json = excluded.guilds_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (session_id, user_id, username, avatar, guilds_json, expires_at, now, now),
            )
            self._connection.execute(
                "DELETE FROM dashboard_sessions WHERE expires_at <= ?",
                (int(time.time()),),
            )
            self._connection.commit()

    def get_dashboard_session(self, session_id: str) -> dict[str, object] | None:
        now_ts = int(time.time())
        with self._lock:
            row = self._connection.execute(
                """
                SELECT session_id, user_id, username, avatar, guilds_json, expires_at
                FROM dashboard_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if int(row["expires_at"]) <= now_ts:
                self._connection.execute(
                    "DELETE FROM dashboard_sessions WHERE session_id = ?",
                    (session_id,),
                )
                self._connection.commit()
                return None
            self._connection.execute(
                "UPDATE dashboard_sessions SET updated_at = ? WHERE session_id = ?",
                (utc_now_iso(), session_id),
            )
            self._connection.commit()

        return {
            "session_id": str(row["session_id"]),
            "user_id": str(row["user_id"]),
            "username": str(row["username"]),
            "avatar": (str(row["avatar"]) if row["avatar"] is not None else None),
            "guilds": self._parse_dashboard_session_guilds(row["guilds_json"]),
            "expires_at": int(row["expires_at"]),
        }

    def delete_dashboard_session(self, session_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM dashboard_sessions WHERE session_id = ?",
                (session_id,),
            )
            self._connection.commit()

    def cleanup_expired_dashboard_sessions(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM dashboard_sessions WHERE expires_at <= ?",
                (int(time.time()),),
            )
            self._connection.commit()
            return cursor.rowcount

    def _parse_dashboard_session_guilds(self, raw: str | None) -> list[dict[str, str]]:
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(value, list):
            return []
        result: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            guild_id = str(item.get("id", "")).strip()
            guild_name = str(item.get("name", "")).strip()
            guild_icon = item.get("icon")
            guild_permissions = str(item.get("permissions", "0")).strip() or "0"
            if not guild_id:
                continue
            result.append(
                {
                    "id": guild_id,
                    "name": guild_name,
                    "icon": str(guild_icon) if guild_icon is not None else "",
                    "permissions": guild_permissions,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Per-guild settings
    # ------------------------------------------------------------------

    def get_guild_settings(
        self,
        guild_id: str,
        *,
        default_recruiting_only: bool = False,
        default_delete_invite_message: bool = False,
        default_forced_channel_id: str | None = None,
    ) -> GuildSettings:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT recruiting_only, delete_invite_message, forced_channel_id
                FROM guild_settings
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        if row is None:
            return GuildSettings(
                guild_id=guild_id,
                recruiting_only=default_recruiting_only,
                delete_invite_message=default_delete_invite_message,
                forced_channel_id=default_forced_channel_id,
            )
        return GuildSettings(
            guild_id=guild_id,
            recruiting_only=bool(row["recruiting_only"]),
            delete_invite_message=bool(row["delete_invite_message"]),
            forced_channel_id=row["forced_channel_id"],
        )

    def upsert_guild_settings(
        self,
        guild_id: str,
        *,
        recruiting_only: bool,
        delete_invite_message: bool,
        forced_channel_id: str | None,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO guild_settings (
                    guild_id,
                    recruiting_only,
                    delete_invite_message,
                    forced_channel_id,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    recruiting_only = excluded.recruiting_only,
                    delete_invite_message = excluded.delete_invite_message,
                    forced_channel_id = excluded.forced_channel_id,
                    updated_at = excluded.updated_at
                """,
                (guild_id, 1 if recruiting_only else 0, 1 if delete_invite_message else 0, forced_channel_id, now),
            )
            self._connection.commit()

    def remove_guild_settings(self, guild_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def remove_followed_players_for_guild(self, guild_id: str) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM followed_players WHERE guild_id = ?",
                (guild_id,),
            )
            self._connection.commit()
            return int(cursor.rowcount)

    def remove_guild_scoped_users_for_guild(self, guild_id: str) -> int:
        # Current schema stores one global users row per Discord user. This cleanup
        # only applies to legacy guild-scoped deployments that still have guild_id.
        with self._lock:
            if "guild_id" not in self._column_names("users"):
                return 0
            cursor = self._connection.execute(
                "DELETE FROM users WHERE guild_id = ?",
                (guild_id,),
            )
            self._connection.commit()
            return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_global_stats(self) -> dict[str, int]:
        """Return total and currently-recruiting subscription counts across all guilds."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN COALESCE(st.lifecycle_state, 'recruiting') = 'recruiting' THEN 1 ELSE 0 END) AS recruiting
                FROM watch_subscriptions ws
                LEFT JOIN watch_states st ON st.subscription_id = ws.subscription_id
                """
            ).fetchone()
        if row is None:
            return {"total": 0, "recruiting": 0}
        return {"total": int(row["total"] or 0), "recruiting": int(row["recruiting"] or 0)}

    def get_guild_stats(self, guild_id: str) -> dict[str, int]:
        """Return subscription counts and followed-player count for one guild."""
        with self._lock:
            sub_row = self._connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN COALESCE(st.lifecycle_state, 'recruiting') = 'recruiting' THEN 1 ELSE 0 END) AS recruiting
                FROM watch_subscriptions ws
                LEFT JOIN watch_states st ON st.subscription_id = ws.subscription_id
                WHERE ws.guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
            follow_row = self._connection.execute(
                """
                SELECT COUNT(DISTINCT discord_user_id) AS followed
                FROM followed_players
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        return {
            "total": int((sub_row["total"] if sub_row else 0) or 0),
            "recruiting": int((sub_row["recruiting"] if sub_row else 0) or 0),
            "followed": int((follow_row["followed"] if follow_row else 0) or 0),
        }

    # ------------------------------------------------------------------
    # Followed players
    # ------------------------------------------------------------------

    def toggle_followed_player(
        self,
        *,
        guild_id: str,
        discord_user_id: str,
        channel_id: str,
        created_by_discord_user_id: str,
    ) -> bool:
        """Flip auto-follow for a member in a channel and return the resulting state.

        ``True`` means the member is now followed. The delete/insert pair runs under
        the connection lock so two concurrent slash commands cannot both observe the
        same "off" state and end up inserting twice.
        """
        now = utc_now_iso()
        with self._lock:
            cursor = self._connection.execute(
                """
                DELETE FROM followed_players
                WHERE guild_id = ? AND discord_user_id = ? AND channel_id = ?
                """,
                (guild_id, discord_user_id, channel_id),
            )
            if cursor.rowcount > 0:
                self._connection.commit()
                return False

            self._connection.execute(
                """
                INSERT INTO followed_players (
                    guild_id,
                    discord_user_id,
                    channel_id,
                    created_by_discord_user_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, discord_user_id, channel_id, created_by_discord_user_id, now),
            )
            self._connection.commit()
            return True

    def is_player_followed(self, *, guild_id: str, discord_user_id: str, channel_id: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT 1 FROM followed_players
                WHERE guild_id = ? AND discord_user_id = ? AND channel_id = ?
                """,
                (guild_id, discord_user_id, channel_id),
            ).fetchone()
        return row is not None

    def list_followed_players(self) -> list[FollowedPlayer]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT follow_id, guild_id, discord_user_id, channel_id, created_by_discord_user_id
                FROM followed_players
                ORDER BY guild_id, channel_id, discord_user_id
                """
            ).fetchall()
        return [
            FollowedPlayer(
                follow_id=int(row["follow_id"]),
                guild_id=row["guild_id"],
                discord_user_id=row["discord_user_id"],
                channel_id=row["channel_id"],
                created_by_discord_user_id=row["created_by_discord_user_id"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Watch subscriptions / states
    # ------------------------------------------------------------------

    def upsert_watch_subscription(
        self,
        *,
        table_id: str,
        table_url: str,
        base_url: str,
        gameserver: str,
        guild_id: str,
        channel_id: str,
        created_by_discord_user_id: str,
        game_name: str | None,
    ) -> WatchSubscription:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO watch_subscriptions (
                    table_id,
                    table_url,
                    base_url,
                    gameserver,
                    guild_id,
                    channel_id,
                    created_by_discord_user_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(table_id, guild_id, channel_id) DO UPDATE SET
                    table_url = excluded.table_url,
                    base_url = excluded.base_url,
                    gameserver = excluded.gameserver,
                    created_by_discord_user_id = excluded.created_by_discord_user_id,
                    updated_at = excluded.updated_at
                """,
                (
                    table_id,
                    table_url,
                    base_url,
                    gameserver,
                    guild_id,
                    channel_id,
                    created_by_discord_user_id,
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                """
                SELECT subscription_id
                FROM watch_subscriptions
                WHERE table_id = ? AND guild_id = ? AND channel_id = ?
                """,
                (table_id, guild_id, channel_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Failed to fetch watch subscription after upsert.")
            subscription_id = int(row["subscription_id"])
            self._connection.execute(
                """
                INSERT INTO watch_states (
                    subscription_id,
                    game_name,
                    lifecycle_state,
                    updated_at
                ) VALUES (?, ?, 'recruiting', ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    game_name = COALESCE(excluded.game_name, watch_states.game_name),
                    updated_at = excluded.updated_at
                """,
                (subscription_id, game_name, now),
            )
            self._connection.commit()
        subscription = self.get_watch_subscription(subscription_id)
        if subscription is None:
            raise RuntimeError("Failed to reload watch subscription after upsert.")
        return subscription

    def get_watch_subscription(self, subscription_id: int) -> WatchSubscription | None:
        with self._lock:
            row = self._connection.execute(self._watch_subscription_query() + " WHERE ws.subscription_id = ?", (subscription_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_watch_subscription(row)

    def get_watch_subscription_by_scope(
        self,
        *,
        table_id: str,
        guild_id: str,
        channel_id: str,
    ) -> WatchSubscription | None:
        with self._lock:
            row = self._connection.execute(
                self._watch_subscription_query()
                + " WHERE ws.table_id = ? AND ws.guild_id = ? AND ws.channel_id = ?",
                (table_id, guild_id, channel_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_watch_subscription(row)

    def _archive_watch_subscription_locked(
        self,
        *,
        subscription_id: int,
        outcome: str,
        finished_at: str,
    ) -> bool:
        normalized_outcome = outcome.strip().lower()
        if normalized_outcome not in {"finished", "cancelled", "unwatched"}:
            raise ValueError(f"Invalid watch archive outcome: {outcome}")

        row = self._connection.execute(
            """
            SELECT
                ws.subscription_id,
                ws.table_id,
                ws.guild_id,
                ws.channel_id,
                ws.created_by_discord_user_id,
                ws.created_at AS recruiting_started_at,
                st.game_name,
                st.game_started_at,
                COALESCE(st.winner_names, '[]') AS winner_names,
                COALESCE(st.final_standings, '[]') AS final_standings,
                st.player_count,
                st.seats_total,
                st.seats_remaining,
                COALESCE(st.seated_player_names, '{}') AS seated_player_names
            FROM watch_subscriptions ws
            LEFT JOIN watch_states st ON st.subscription_id = ws.subscription_id
            WHERE ws.subscription_id = ?
            """,
            (subscription_id,),
        ).fetchone()
        if row is None:
            return False

        winner_names = json_loads_list(row["winner_names"])
        final_standings = json_loads_list(row["final_standings"])
        player_count = int(row["player_count"]) if row["player_count"] is not None else None

        seated_player_names = json_loads_dict(row["seated_player_names"])
        if player_count is None:
            if row["seats_total"] is not None and row["seats_remaining"] is not None:
                player_count = max(int(row["seats_total"]) - int(row["seats_remaining"]), 0)
            elif seated_player_names:
                player_count = len(seated_player_names)
            elif final_standings:
                player_count = len(final_standings)

        self._connection.execute(
            """
            INSERT INTO game_history (
                table_id,
                game_name,
                guild_id,
                channel_id,
                created_by_discord_user_id,
                recruiting_started_at,
                game_started_at,
                finished_at,
                outcome,
                winner_names,
                final_standings,
                player_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["table_id"],
                row["game_name"],
                row["guild_id"],
                row["channel_id"],
                row["created_by_discord_user_id"],
                row["recruiting_started_at"],
                row["game_started_at"],
                finished_at,
                normalized_outcome,
                json_dumps(winner_names),
                json_dumps(final_standings),
                player_count,
            ),
        )
        self._connection.execute(
            "DELETE FROM watch_subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        )
        return True

    def _archive_watch_subscription(self, *, subscription_id: int, outcome: str) -> bool:
        with self._lock:
            archived = self._archive_watch_subscription_locked(
                subscription_id=subscription_id,
                outcome=outcome,
                finished_at=utc_now_iso(),
            )
            self._connection.commit()
            return archived

    def remove_watch_subscription(
        self,
        *,
        table_id: str,
        guild_id: str,
        channel_id: str,
        outcome: str = "unwatched",
    ) -> bool:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT subscription_id
                FROM watch_subscriptions
                WHERE table_id = ? AND guild_id = ? AND channel_id = ?
                """,
                (table_id, guild_id, channel_id),
            ).fetchone()
            if row is None:
                return False
            archived = self._archive_watch_subscription_locked(
                subscription_id=int(row["subscription_id"]),
                outcome=outcome,
                finished_at=utc_now_iso(),
            )
            self._connection.commit()
            return archived

    def remove_all_watch_subscriptions_for_guild(self, guild_id: str, *, outcome: str = "cancelled") -> int:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT subscription_id
                FROM watch_subscriptions
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchall()
            removed_count = 0
            finished_at = utc_now_iso()
            for row in rows:
                if self._archive_watch_subscription_locked(
                    subscription_id=int(row["subscription_id"]),
                    outcome=outcome,
                    finished_at=finished_at,
                ):
                    removed_count += 1
            self._connection.commit()
            return removed_count

    def list_watch_subscriptions(self) -> list[WatchSubscription]:
        with self._lock:
            rows = self._connection.execute(
                self._watch_subscription_query() + " ORDER BY ws.table_id, ws.guild_id, ws.channel_id"
            ).fetchall()
        return [self._row_to_watch_subscription(row) for row in rows]

    def list_watch_subscriptions_for_guild(self, guild_id: str) -> list[WatchSubscription]:
        with self._lock:
            rows = self._connection.execute(
                self._watch_subscription_query() + " WHERE ws.guild_id = ? ORDER BY ws.table_id, ws.channel_id",
                (guild_id,),
            ).fetchall()
        return [self._row_to_watch_subscription(row) for row in rows]

    def list_recent_game_history_for_guild(self, guild_id: str, *, limit: int = 10) -> list[GameHistoryEntry]:
        effective_limit = max(1, min(100, int(limit)))
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT
                    history_id,
                    table_id,
                    game_name,
                    guild_id,
                    channel_id,
                    created_by_discord_user_id,
                    recruiting_started_at,
                    game_started_at,
                    finished_at,
                    outcome,
                    winner_names,
                    final_standings,
                    player_count
                FROM game_history
                WHERE guild_id = ? AND outcome = 'finished'
                ORDER BY finished_at DESC, history_id DESC
                LIMIT ?
                """,
                (guild_id, effective_limit),
            ).fetchall()
        return [
            GameHistoryEntry(
                history_id=int(row["history_id"]),
                table_id=row["table_id"],
                game_name=row["game_name"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                created_by_discord_user_id=row["created_by_discord_user_id"],
                recruiting_started_at=row["recruiting_started_at"],
                game_started_at=row["game_started_at"],
                finished_at=row["finished_at"],
                outcome=row["outcome"],
                winner_names=json_loads_list(row["winner_names"]),
                final_standings=json_loads_list(row["final_standings"]),
                player_count=int(row["player_count"]) if row["player_count"] is not None else None,
            )
            for row in rows
        ]

    def list_game_history_for_guild(self, guild_id: str, *, limit: int | None = None) -> list[GameHistoryEntry]:
        limit_clause = ""
        params: tuple[object, ...]
        if limit is None:
            params = (guild_id,)
        else:
            effective_limit = max(1, min(5000, int(limit)))
            limit_clause = " LIMIT ?"
            params = (guild_id, effective_limit)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    history_id,
                    table_id,
                    game_name,
                    guild_id,
                    channel_id,
                    created_by_discord_user_id,
                    recruiting_started_at,
                    game_started_at,
                    finished_at,
                    outcome,
                    winner_names,
                    final_standings,
                    player_count
                FROM game_history
                WHERE guild_id = ?
                ORDER BY finished_at DESC, history_id DESC{limit_clause}
                """,
                params,
            ).fetchall()
        return [
            GameHistoryEntry(
                history_id=int(row["history_id"]),
                table_id=row["table_id"],
                game_name=row["game_name"],
                guild_id=row["guild_id"],
                channel_id=row["channel_id"],
                created_by_discord_user_id=row["created_by_discord_user_id"],
                recruiting_started_at=row["recruiting_started_at"],
                game_started_at=row["game_started_at"],
                finished_at=row["finished_at"],
                outcome=row["outcome"],
                winner_names=json_loads_list(row["winner_names"]),
                final_standings=json_loads_list(row["final_standings"]),
                player_count=int(row["player_count"]) if row["player_count"] is not None else None,
            )
            for row in rows
        ]

    def update_watch_state(
        self,
        *,
        subscription_id: int,
        last_packet_id: int,
        waiting_ids: list[str],
        player_names: dict[str, str],
        seated_player_names: dict[str, str] | None = None,
        seats_total: int | None = None,
        seats_remaining: int | None = None,
        is_initialized: bool,
        game_name: str | None,
        lifecycle_state: str | None = None,
        game_started_at: str | None = None,
        winner_names: list[str] | None = None,
        final_standings: list[str] | None = None,
        player_count: int | None = None,
        tracked_message_id: int | None = None,
        tracked_message_kind: str | None = None,
    ) -> None:
        now = utc_now_iso()
        effective_game_started_at = game_started_at
        if effective_game_started_at is None and lifecycle_state == "in_progress":
            effective_game_started_at = now
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO watch_states (
                    subscription_id,
                    last_packet_id,
                    last_waiting_ids,
                    last_player_names,
                    seated_player_names,
                    seats_total,
                    seats_remaining,
                    is_initialized,
                    game_name,
                    lifecycle_state,
                    game_started_at,
                    winner_names,
                    final_standings,
                    player_count,
                    tracked_message_id,
                    tracked_message_kind,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'recruiting'), ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    last_packet_id = excluded.last_packet_id,
                    last_waiting_ids = excluded.last_waiting_ids,
                    last_player_names = excluded.last_player_names,
                    seated_player_names = COALESCE(excluded.seated_player_names, watch_states.seated_player_names),
                    seats_total = COALESCE(excluded.seats_total, watch_states.seats_total),
                    seats_remaining = COALESCE(excluded.seats_remaining, watch_states.seats_remaining),
                    is_initialized = excluded.is_initialized,
                    game_name = excluded.game_name,
                    lifecycle_state = COALESCE(excluded.lifecycle_state, watch_states.lifecycle_state),
                    game_started_at = COALESCE(watch_states.game_started_at, excluded.game_started_at),
                    winner_names = COALESCE(excluded.winner_names, watch_states.winner_names),
                    final_standings = COALESCE(excluded.final_standings, watch_states.final_standings),
                    player_count = COALESCE(excluded.player_count, watch_states.player_count),
                    tracked_message_id = COALESCE(excluded.tracked_message_id, watch_states.tracked_message_id),
                    tracked_message_kind = COALESCE(excluded.tracked_message_kind, watch_states.tracked_message_kind),
                    updated_at = excluded.updated_at
                """,
                (
                    subscription_id,
                    last_packet_id,
                    json_dumps(waiting_ids),
                    json_dumps(player_names),
                    json_dumps(seated_player_names) if seated_player_names is not None else None,
                    seats_total,
                    seats_remaining,
                    1 if is_initialized else 0,
                    game_name,
                    lifecycle_state,
                    effective_game_started_at,
                    json_dumps(winner_names) if winner_names is not None else None,
                    json_dumps(final_standings) if final_standings is not None else None,
                    player_count,
                    str(tracked_message_id) if tracked_message_id is not None else None,
                    tracked_message_kind,
                    now,
                ),
            )
            self._connection.commit()

    def update_watch_message_tracking(
        self,
        *,
        subscription_id: int,
        lifecycle_state: str,
        tracked_message_id: int | None,
        tracked_message_kind: str | None,
    ) -> None:
        now = utc_now_iso()
        with self._lock:
            self._connection.execute(
                """
                UPDATE watch_states
                SET lifecycle_state = ?,
                    tracked_message_id = ?,
                    tracked_message_kind = ?,
                    updated_at = ?
                WHERE subscription_id = ?
                """,
                (
                    lifecycle_state,
                    str(tracked_message_id) if tracked_message_id is not None else None,
                    tracked_message_kind,
                    now,
                    subscription_id,
                ),
            )
            self._connection.commit()

    # ------------------------------------------------------------------
    # Schema migrations
    # ------------------------------------------------------------------

    def _ensure_watch_subscription_columns(self) -> None:
        existing_columns = self._column_names("watch_subscriptions")
        if "table_url" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_subscriptions ADD COLUMN table_url TEXT")
        if "base_url" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_subscriptions ADD COLUMN base_url TEXT")
        if "gameserver" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_subscriptions ADD COLUMN gameserver TEXT")

    def _ensure_watch_state_columns(self) -> None:
        existing_columns = self._column_names("watch_states")
        if "last_player_names" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN last_player_names TEXT NOT NULL DEFAULT '{}'"
            )
        if "seated_player_names" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN seated_player_names TEXT NOT NULL DEFAULT '{}'"
            )
        if "seats_total" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_states ADD COLUMN seats_total INTEGER")
        if "seats_remaining" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_states ADD COLUMN seats_remaining INTEGER")
        if "lifecycle_state" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'recruiting'"
            )
        if "game_started_at" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_states ADD COLUMN game_started_at TEXT")
        if "winner_names" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN winner_names TEXT NOT NULL DEFAULT '[]'"
            )
        if "final_standings" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN final_standings TEXT NOT NULL DEFAULT '[]'"
            )
        if "player_count" not in existing_columns:
            self._connection.execute("ALTER TABLE watch_states ADD COLUMN player_count INTEGER")
        if "tracked_message_id" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN tracked_message_id TEXT"
            )
        if "tracked_message_kind" not in existing_columns:
            self._connection.execute(
                "ALTER TABLE watch_states ADD COLUMN tracked_message_kind TEXT"
            )
        self._connection.execute(
            """
            UPDATE watch_states
            SET lifecycle_state = CASE
                WHEN COALESCE(lifecycle_state, '') = '' AND COALESCE(is_initialized, 0) = 1 THEN 'in_progress'
                WHEN COALESCE(lifecycle_state, '') = '' THEN 'recruiting'
                ELSE lifecycle_state
            END
            """
        )

    def _migrate_legacy_watches_if_needed(self) -> None:
        if not self._table_exists("watches"):
            return
        if self._count_rows("watch_subscriptions") > 0:
            return

        now = utc_now_iso()
        self._connection.execute(
            """
            INSERT OR IGNORE INTO watch_subscriptions (
                table_id,
                guild_id,
                channel_id,
                created_by_discord_user_id,
                created_at,
                updated_at
            )
            SELECT
                table_id,
                guild_id,
                channel_id,
                MIN(discord_user_id) AS created_by_discord_user_id,
                MIN(created_at) AS created_at,
                MAX(updated_at) AS updated_at
            FROM watches
            GROUP BY table_id, guild_id, channel_id
            """
        )
        self._connection.execute(
            """
            INSERT OR IGNORE INTO watch_states (
                subscription_id,
                last_packet_id,
                last_waiting_ids,
                is_initialized,
                game_name,
                updated_at
            )
            SELECT
                ws.subscription_id,
                COALESCE(
                    (
                        SELECT MAX(COALESCE(w2.last_packet_id, 1))
                        FROM watches w2
                        WHERE w2.table_id = ws.table_id
                          AND w2.guild_id = ws.guild_id
                          AND w2.channel_id = ws.channel_id
                    ),
                    1
                ) AS last_packet_id,
                COALESCE(
                    (
                        SELECT w3.last_waiting_ids
                        FROM watches w3
                        WHERE w3.table_id = ws.table_id
                          AND w3.guild_id = ws.guild_id
                          AND w3.channel_id = ws.channel_id
                        ORDER BY COALESCE(w3.is_initialized, 0) DESC, COALESCE(w3.last_packet_id, 1) DESC
                        LIMIT 1
                    ),
                    '[]'
                ) AS last_waiting_ids,
                COALESCE(
                    (
                        SELECT MAX(COALESCE(w4.is_initialized, 0))
                        FROM watches w4
                        WHERE w4.table_id = ws.table_id
                          AND w4.guild_id = ws.guild_id
                          AND w4.channel_id = ws.channel_id
                    ),
                    0
                ) AS is_initialized,
                (
                    SELECT w5.game_name
                    FROM watches w5
                    WHERE w5.table_id = ws.table_id
                      AND w5.guild_id = ws.guild_id
                      AND w5.channel_id = ws.channel_id
                      AND w5.game_name IS NOT NULL
                    ORDER BY COALESCE(w5.updated_at, w5.created_at) DESC
                    LIMIT 1
                ) AS game_name,
                ? AS updated_at
            FROM watch_subscriptions ws
            """,
            (now,),
        )

    def _migrate_users_to_guild_scope(self) -> None:
        # This migration is superseded by _migrate_users_to_global.
        # Users that are still on the very old global schema (no guild_id column)
        # are already compatible with the new global schema, so nothing to do.
        # Users on the guild-scoped schema (with guild_id) will be handled by
        # _migrate_users_to_global immediately after.
        pass

    def _migrate_users_to_global(self) -> None:
        """Collapse the guild-scoped users table back to a single global row per Discord user.

        Strategy: for each discord_user_id keep the most complete row (both
        bga_player_id and bga_player_name present), using updated_at as the
        tiebreaker. Any deployment that was never on the guild-scoped schema
        (``guild_id`` not in columns) is already compatible and skipped.
        """
        columns = self._column_names("users")
        if not columns or "guild_id" not in columns:
            return

        self._connection.execute(
            """
            CREATE TABLE users_new (
                discord_user_id TEXT NOT NULL,
                bga_player_id TEXT NOT NULL,
                bga_player_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (discord_user_id)
            )
            """
        )
        # ROW_NUMBER window function requires SQLite >= 3.25 (Python 3.11+ ships 3.39+).
        self._connection.execute(
            """
            WITH ranked AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY discord_user_id
                        ORDER BY
                            CASE WHEN bga_player_id != '' AND bga_player_name != '' THEN 0 ELSE 1 END ASC,
                            updated_at DESC
                    ) AS rn
                FROM users
            )
            INSERT INTO users_new (discord_user_id, bga_player_id, bga_player_name, created_at, updated_at)
            SELECT discord_user_id, bga_player_id, bga_player_name, created_at, updated_at
            FROM ranked WHERE rn = 1
            """
        )
        self._connection.execute("DROP TABLE users")
        self._connection.execute("ALTER TABLE users_new RENAME TO users")

    def _drop_legacy_tables_if_safe(self) -> None:
        if not self._table_exists("watches"):
            return
        if self._count_rows("watch_subscriptions") == 0:
            return
        self._connection.execute("DROP TABLE IF EXISTS watches")
        self._connection.execute("DROP INDEX IF EXISTS idx_watches_table_id")

    def _table_exists(self, table_name: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _column_names(self, table_name: str) -> set[str]:
        rows = self._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row[1]) for row in rows}

    def _count_rows(self, table_name: str) -> int:
        row = self._connection.execute(f"SELECT COUNT(*) AS count_value FROM {table_name}").fetchone()
        if row is None:
            return 0
        return int(row["count_value"])

    @staticmethod
    def _normalize_player_name(value: str) -> str:
        return value.strip().casefold()

    @classmethod
    def _find_matching_linked_user(
        cls,
        linked_users: list[LinkedUser],
        player_id: str,
        player_name: str,
    ) -> LinkedUser | None:
        normalized_name = cls._normalize_player_name(player_name) if player_name else ""
        matches: dict[str, LinkedUser] = {}
        for item in linked_users:
            id_match = bool(player_id) and bool(item.bga_player_id) and item.bga_player_id == player_id
            name_match = (
                bool(normalized_name)
                and bool(item.bga_player_name)
                and cls._normalize_player_name(item.bga_player_name) == normalized_name
            )
            if id_match or name_match:
                matches[item.discord_user_id] = item
        if len(matches) != 1:
            return None
        return next(iter(matches.values()))

    @staticmethod
    def _watch_subscription_query() -> str:
        return """
            SELECT
                ws.subscription_id,
                ws.table_id,
                ws.table_url,
                ws.base_url,
                ws.gameserver,
                ws.guild_id,
                ws.channel_id,
                ws.created_by_discord_user_id,
                COALESCE(st.last_packet_id, 1) AS last_packet_id,
                COALESCE(st.last_waiting_ids, '[]') AS last_waiting_ids,
                COALESCE(st.last_player_names, '{}') AS last_player_names,
                COALESCE(st.seated_player_names, '{}') AS seated_player_names,
                st.seats_total,
                st.seats_remaining,
                COALESCE(st.is_initialized, 0) AS is_initialized,
                st.game_name,
                COALESCE(st.lifecycle_state, 'recruiting') AS lifecycle_state,
                st.tracked_message_id,
                st.tracked_message_kind
            FROM watch_subscriptions ws
            LEFT JOIN watch_states st ON st.subscription_id = ws.subscription_id
        """

    @staticmethod
    def _row_to_watch_subscription(row: sqlite3.Row) -> WatchSubscription:
        return WatchSubscription(
            subscription_id=int(row["subscription_id"]),
            table_id=row["table_id"],
            table_url=row["table_url"],
            base_url=row["base_url"],
            gameserver=row["gameserver"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            created_by_discord_user_id=row["created_by_discord_user_id"],
            last_packet_id=int(row["last_packet_id"]),
            last_waiting_ids=json_loads_list(row["last_waiting_ids"]),
            player_names=json_loads_dict(row["last_player_names"]),
            seated_player_names=json_loads_dict(row["seated_player_names"]),
            seats_total=int(row["seats_total"]) if row["seats_total"] is not None else None,
            seats_remaining=int(row["seats_remaining"]) if row["seats_remaining"] is not None else None,
            is_initialized=bool(row["is_initialized"]),
            game_name=row["game_name"],
            lifecycle_state=row["lifecycle_state"] or "recruiting",
            tracked_message_id=int(row["tracked_message_id"]) if row["tracked_message_id"] else None,
            tracked_message_kind=row["tracked_message_kind"],
        )
