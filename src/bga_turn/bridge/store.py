"""SQLite persistence for tournament bindings and the result outbox.

Mixed into :class:`bga_turn.database.Database`, which provides ``_lock`` and
``_connection``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field

from ..utils import json_dumps, json_loads_list, utc_now_iso


@dataclass(slots=True)
class CodenamesBinding:
    channel_id: str
    guild_id: str
    external_ref: str
    players: list[str] = field(default_factory=list)
    # Discord user ID -> the name the tournament knows them by (helps match seats).
    player_names: dict[str, str] = field(default_factory=dict)
    # The current game's expected seats: {"game": 2, "last_event_id": ..., "blue": {"spymaster": id,
    # "operative": id}, "red": {...}}, or None.
    lineup: dict | None = None
    # Unix seconds when the tournament round ends, if it has a deadline.
    deadline: int | None = None
    # The newest outbox_id in this channel when the line-up last changed: a line-up
    # sent after a result was queued is newer than that result, even if the
    # tournament never received it (None for bindings stored before this was kept).
    lineup_after_outbox_id: int | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class OutboxEntry:
    outbox_id: int
    event_id: str
    channel_id: str
    external_ref: str
    payload: dict
    attempts: int
    next_attempt_at: float
    status: str
    last_error: str | None


class BridgeStore:
    _lock: threading.RLock
    _connection: sqlite3.Connection

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def _ensure_codenames_binding_columns(self) -> None:
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(codenames_bindings)")}
        if "details_json" not in columns:
            self._connection.execute("ALTER TABLE codenames_bindings ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")

    def upsert_codenames_binding(
        self,
        channel_id: str,
        *,
        guild_id: str,
        external_ref: str,
        players: list[str],
        player_names: dict[str, str] | None = None,
        lineup: dict | None = None,
        deadline: int | None = None,
    ) -> CodenamesBinding:
        now = utc_now_iso()
        with self._lock:
            previous = self.get_codenames_binding(channel_id)
            if previous is not None and previous.lineup == lineup:
                lineup_after = previous.lineup_after_outbox_id
            else:
                lineup_after = self._latest_outbox_id(channel_id)
            details = json_dumps(
                {
                    "player_names": player_names or {},
                    "lineup": lineup,
                    "deadline": deadline,
                    "lineup_after_outbox_id": lineup_after,
                }
            )
            self._connection.execute(
                """
                INSERT INTO codenames_bindings
                    (channel_id, guild_id, external_ref, players_json, details_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    guild_id = excluded.guild_id,
                    external_ref = excluded.external_ref,
                    players_json = excluded.players_json,
                    details_json = excluded.details_json,
                    updated_at = excluded.updated_at
                """,
                (channel_id, guild_id, external_ref, json_dumps(players), details, now, now),
            )
            self._connection.commit()
            binding = self.get_codenames_binding(channel_id)
        assert binding is not None
        return binding

    def get_codenames_binding(self, channel_id: str) -> CodenamesBinding | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM codenames_bindings WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        return _row_to_binding(row) if row is not None else None

    def remove_codenames_binding(self, channel_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM codenames_bindings WHERE channel_id = ?", (channel_id,))
            self._connection.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Result outbox
    # ------------------------------------------------------------------

    def enqueue_bridge_result(self, event_id: str, *, channel_id: str, external_ref: str, payload: dict) -> bool:
        """Queue a result for delivery; ``False`` if this event was already queued."""
        now = utc_now_iso()
        with self._lock:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO codenames_result_outbox
                    (event_id, channel_id, external_ref, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, channel_id, external_ref, json_dumps(payload), now, now),
            )
            self._connection.commit()
        return cursor.rowcount > 0

    def list_due_bridge_results(self, now: float, *, limit: int = 20) -> list[OutboxEntry]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM codenames_result_outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY outbox_id LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [_row_to_outbox(row) for row in rows]

    def next_bridge_result_due_at(self) -> float | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT MIN(next_attempt_at) AS due FROM codenames_result_outbox WHERE status = 'pending'"
            ).fetchone()
        return row["due"] if row is not None else None

    def update_bridge_result(
        self,
        outbox_id: int,
        *,
        status: str,
        attempts: int,
        next_attempt_at: float = 0,
        last_error: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                UPDATE codenames_result_outbox
                SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE outbox_id = ?
                """,
                (status, attempts, next_attempt_at, last_error, utc_now_iso(), outbox_id),
            )
            self._connection.commit()

    def requeue_bridge_results(self, channel_id: str) -> int:
        """Send every result reported from *channel_id* again (the tournament ignores
        ones it has; it can fill in what it was missing). Returns how many."""
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE codenames_result_outbox
                SET status = 'pending', attempts = 0, next_attempt_at = 0, last_error = NULL, updated_at = ?
                WHERE channel_id = ? AND status != 'pending'
                """,
                (utc_now_iso(), channel_id),
            )
            self._connection.commit()
        return cursor.rowcount

    def latest_bridge_event_id(self, channel_id: str) -> str | None:
        """The last finished game reported from *channel_id*, or ``None``."""
        with self._lock:
            row = self._connection.execute(
                "SELECT event_id FROM codenames_result_outbox WHERE channel_id = ? ORDER BY outbox_id DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
        return row["event_id"] if row is not None else None

    def _latest_outbox_id(self, channel_id: str) -> int:
        row = self._connection.execute(
            "SELECT MAX(outbox_id) AS latest FROM codenames_result_outbox WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["latest"] or 0

    def codenames_lineup_is_current(self, binding: CodenamesBinding) -> bool:
        """Whether *binding*'s line-up is for the game after the last result reported from its channel.

        True when the tournament says it processed that result, or when the line-up
        changed after the result was queued: the tournament moved on without it (a
        result logged by hand, or one it never received)."""
        if not binding.lineup:
            return False
        with self._lock:
            row = self._connection.execute(
                """
                SELECT outbox_id, event_id, created_at FROM codenames_result_outbox
                WHERE channel_id = ? ORDER BY outbox_id DESC LIMIT 1
                """,
                (binding.channel_id,),
            ).fetchone()
        if row is None:
            return True
        if binding.lineup.get("last_event_id") == row["event_id"]:
            return True
        if binding.lineup_after_outbox_id is not None:
            return binding.lineup_after_outbox_id >= row["outbox_id"]
        return binding.updated_at > row["created_at"]

    def list_bridge_results(self, channel_id: str) -> list[OutboxEntry]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM codenames_result_outbox WHERE channel_id = ? ORDER BY outbox_id", (channel_id,)
            ).fetchall()
        return [_row_to_outbox(row) for row in rows]


def _row_to_binding(row: sqlite3.Row) -> CodenamesBinding:
    try:
        details = json.loads(row["details_json"] or "{}")
    except json.JSONDecodeError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    names = details.get("player_names")
    lineup = details.get("lineup")
    lineup_after = details.get("lineup_after_outbox_id")
    return CodenamesBinding(
        channel_id=row["channel_id"],
        guild_id=row["guild_id"],
        external_ref=row["external_ref"],
        players=json_loads_list(row["players_json"]),
        player_names={str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {},
        lineup=lineup if isinstance(lineup, dict) else None,
        deadline=details.get("deadline") if isinstance(details.get("deadline"), int) else None,
        lineup_after_outbox_id=lineup_after if isinstance(lineup_after, int) else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_outbox(row: sqlite3.Row) -> OutboxEntry:
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        payload = {}
    return OutboxEntry(
        outbox_id=row["outbox_id"],
        event_id=row["event_id"],
        channel_id=row["channel_id"],
        external_ref=row["external_ref"],
        payload=payload if isinstance(payload, dict) else {},
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        status=row["status"],
        last_error=row["last_error"],
    )
