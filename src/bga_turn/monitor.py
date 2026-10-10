from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass

import discord
from discord.ext import tasks

from . import cards
from .bga_client import BgaClient, BgaClientError, BgaNotPublicError, BgaRateLimitError, BgaServerError, BgaTableUnavailableError
from .database import AsyncDatabase, Database
from .discord_io import DiscordRateLimiter
from .i18n import tr
from .models import BgaTableSnapshot, LinkedUser, WatchSubscription
from .utils import BASE_URL, build_table_url

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveTableMessage:
    message: discord.Message
    kind: str
    waiting_ids: list[str]
    payload_signature: str | None = None


class BgaMonitor:
    LIFECYCLE_RECRUITING = cards.LIFECYCLE_RECRUITING
    LIFECYCLE_IN_PROGRESS = cards.LIFECYCLE_IN_PROGRESS
    LIFECYCLE_FINISHED = cards.LIFECYCLE_FINISHED
    # Kind of a tracked message that is still the "link detected" status note,
    # waiting to be turned into the table's lifecycle card.
    LINK_STATUS_KIND = "link_status"
    # How long the status note may stay unchanged before it says it is waiting on BGA.
    LINK_STATUS_WAITING_AFTER_SECONDS = 20.0
    _LIFECYCLE_ORDER = {
        LIFECYCLE_RECRUITING: 0,
        LIFECYCLE_IN_PROGRESS: 1,
        LIFECYCLE_FINISHED: 2,
    }
    # Cross-table 500 error tracking: how many seconds a server error is counted
    # as "recent" when deciding whether the outage is site-wide or table-specific.
    _SERVER_ERROR_WINDOW_SECONDS = 120.0
    # Number of consecutive server errors on a single table before we add an error
    # banner to the Discord message for that table.
    _SERVER_ERROR_DISCORD_THRESHOLD = 3
    # Fraction of active tables that must report a server error within the window
    # before we conclude BGA itself is down (rather than a per-table problem).
    _SITE_DOWN_TABLE_FRACTION = 0.5
    # Minimum number of active tables required before we apply the site-down
    # heuristic (with only 1–2 tables we always treat it as table-specific).
    _SITE_DOWN_MIN_TABLES = 3

    def __init__(
        self,
        bot: discord.Client,
        database: Database,
        bga_client: BgaClient,
        poll_seconds: int,
        *,
        default_recruiting_only: bool = False,
        default_forced_channel_id: str | None = None,
    ) -> None:
        self.bot = bot
        self.database = database
        self.db = AsyncDatabase(database)
        self.bga_client = bga_client
        self._poll_seconds = max(5, poll_seconds)
        self._default_recruiting_only = default_recruiting_only
        self._default_forced_channel_id = default_forced_channel_id
        self._table_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_messages: dict[int, ActiveTableMessage] = {}
        # Serialises everything that swaps a subscription's tracked message, so a
        # link status note can be handed over without racing the table worker.
        self._message_locks: dict[int, asyncio.Lock] = {}
        self._last_player_name_refresh_at: dict[str, float] = {}
        self._cover_image_urls: dict[str, str] = {}
        self.discord = DiscordRateLimiter()
        # Per-table 500 error tracking for site-wide vs table-specific detection.
        # Monotonic time of the most recent server error per table.
        self._table_server_error_at: dict[str, float] = {}
        # Number of consecutive server errors per table (reset on success).
        self._table_consecutive_server_errors: dict[str, int] = {}
        # Optional error note displayed in the Discord message for a table when it
        # has seen enough consecutive server errors to warrant user-visible feedback.
        self._table_error_note: dict[str, str | None] = {}
        self.sync_tables.change_interval(seconds=self._poll_seconds)

    def start(self) -> None:
        if not self.sync_tables.is_running():
            self.sync_tables.start()

    def stop(self) -> None:
        if self.sync_tables.is_running():
            self.sync_tables.cancel()
        for task in self._table_tasks.values():
            task.cancel()
        self._table_tasks.clear()
        self._active_messages.clear()
        self._message_locks.clear()
        self._last_player_name_refresh_at.clear()
        self._cover_image_urls.clear()
        self.discord.reset()
        self._table_server_error_at.clear()
        self._table_consecutive_server_errors.clear()
        self._table_error_note.clear()

    def _is_site_wide_outage(self) -> bool:
        """Return True if enough active tables are seeing recent 500 errors to suggest BGA is down.

        Requires at least ``_SITE_DOWN_MIN_TABLES`` active tables, and that at
        least ``_SITE_DOWN_TABLE_FRACTION`` of them reported a server error within
        the last ``_SERVER_ERROR_WINDOW_SECONDS``.  With fewer than the minimum
        number of tables we conservatively treat any 500 as table-specific.
        """
        active_tables = set(self._table_tasks.keys())
        total = len(active_tables)
        if total < self._SITE_DOWN_MIN_TABLES:
            return False
        now = time.monotonic()
        recent_errors = sum(
            1 for tid in active_tables
            if (now - self._table_server_error_at.get(tid, 0.0)) < self._SERVER_ERROR_WINDOW_SECONDS
        )
        threshold = max(self._SITE_DOWN_MIN_TABLES, int(total * self._SITE_DOWN_TABLE_FRACTION))
        return recent_errors >= threshold

    def _clear_server_error_state(self, table_id: str) -> None:
        """Clear per-table server error counters on a successful BGA response.

        If a Discord error note was previously shown for this table, the note is
        removed here. The card signature includes the note, so the next lifecycle
        update sees a changed signature and re-renders without the banner.
        """
        self._table_server_error_at.pop(table_id, None)
        self._table_consecutive_server_errors.pop(table_id, None)
        if self._table_error_note.pop(table_id, None) is not None:
            LOGGER.info(tr("bga_server_error_cleared", table_id=table_id))

    async def _push_error_note_to_discord(self, table_id: str) -> None:
        """Force an immediate update of the tracked Discord message for *table_id*.

        Called once the error-note threshold is reached so that the error banner
        appears in Discord without waiting for the next natural state change.
        """
        subscriptions = await self._subscriptions_for_table(table_id)
        for subscription in subscriptions:
            lifecycle_state = subscription.lifecycle_state or self.LIFECYCLE_IN_PROGRESS
            waiting_ids = list(subscription.last_waiting_ids)
            player_names = dict(subscription.player_names)
            await self._publish_or_update_lifecycle_message(
                subscription=subscription,
                table_id=table_id,
                lifecycle_state=lifecycle_state,
                waiting_ids=waiting_ids,
                player_names=player_names,
                snapshot=None,
            )

    @tasks.loop(seconds=30)
    async def sync_tables(self) -> None:
        await self._sync_tables_once()

    async def refresh_now(self) -> None:
        await self._sync_tables_once()

    async def _sync_tables_once(self) -> None:
        subscriptions = await self.db.list_watch_subscriptions()
        active_table_ids = {subscription.table_id for subscription in subscriptions}
        active_subscription_ids = {subscription.subscription_id for subscription in subscriptions}
        active_channel_ids = {subscription.channel_id for subscription in subscriptions}
        self.discord.sweep(active_channel_ids)

        for subscription_id in list(self._active_messages):
            if subscription_id not in active_subscription_ids:
                active_message = self._active_messages.pop(subscription_id, None)
                if active_message is not None:
                    try:
                        result = await self.discord.call(
                            "delete_orphan",
                            "?",
                            lambda msg=active_message.message: msg.delete(),
                            channel_id=str(active_message.message.channel.id),
                            operation_kind="delete",
                        )
                        if result is None:
                            LOGGER.error(
                                tr(
                                    "orphan_watch_message_delete_failed",
                                    subscription_id=subscription_id,
                                    message_kind=active_message.kind,
                                    error="rate-limit retries exhausted",
                                )
                            )
                        else:
                            LOGGER.info(
                                tr(
                                    "orphan_watch_message_deleted",
                                    subscription_id=subscription_id,
                                    message_kind=active_message.kind,
                                )
                            )
                    except discord.NotFound:
                        pass
                    except discord.DiscordException as exc:
                        LOGGER.error(
                            tr(
                                "orphan_watch_message_delete_failed",
                                subscription_id=subscription_id,
                                message_kind=active_message.kind,
                                error=exc,
                            )
                        )

        for table_id in list(self._table_tasks):
            if table_id not in active_table_ids:
                task = self._table_tasks.pop(table_id)
                task.cancel()
                self._last_player_name_refresh_at.pop(table_id, None)
                LOGGER.info(tr("worker_stopped", table_id=table_id))

        for table_id in sorted(active_table_ids):
            task = self._table_tasks.get(table_id)
            if task is None or task.done():
                self._table_tasks[table_id] = asyncio.create_task(self._run_table_worker(table_id))
                LOGGER.info(tr("worker_started", table_id=table_id))

    @sync_tables.before_loop
    async def before_sync_tables(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_table_worker(self, table_id: str) -> None:
        backoff_seconds = 5.0
        while True:
            try:
                subscriptions = await self._subscriptions_for_table(table_id)
                if not subscriptions:
                    return

                reference = subscriptions[0]
                base_url = reference.base_url or BASE_URL
                snapshot = await asyncio.to_thread(
                    self.bga_client.fetch_public_table_snapshot,
                    table_id,
                    base_url,
                )
                subscriptions = await self._sync_subscriptions_from_snapshot(subscriptions, snapshot)
                reference = subscriptions[0]
                if snapshot.cover_image_url:
                    self._cover_image_urls[table_id] = snapshot.cover_image_url
                if snapshot.is_finished:
                    await self._finalize_finished_table(subscriptions, table_id, snapshot=snapshot)
                    return
                if not snapshot.can_watch_turns:
                    await self._apply_invite_state(table_id, subscriptions, snapshot)
                    # Decay backoff toward the configured poll interval on success.
                    backoff_seconds = max(self._poll_seconds, backoff_seconds / 2)
                    self._clear_server_error_state(table_id)
                    await asyncio.sleep(self._poll_seconds)
                    continue

                table_info = self.bga_client.build_public_table_info(
                    table_id=reference.table_id,
                    table_url=snapshot.table_url or reference.table_url or build_table_url(table_id),
                    base_url=reference.base_url or base_url,
                    gameserver=snapshot.gameserver or reference.gameserver or "",
                    game_name=snapshot.game_name or reference.game_name or "unknown",
                )
                current_waiting_ids = self._select_previous_waiting_ids(subscriptions)
                known_player_names = self._merge_player_names(subscriptions)
                known_player_names.update(snapshot.player_names)

                # Successful snapshot fetch — decay backoff toward configured floor.
                backoff_seconds = max(self._poll_seconds, backoff_seconds / 2)
                self._clear_server_error_state(table_id)

                async for state in self.bga_client.watch_table(
                    table_info,
                    current_waiting_ids=current_waiting_ids,
                    known_player_names=known_player_names,
                ):
                    current_waiting_ids = state.waiting_ids or current_waiting_ids
                    known_player_names.update(state.player_names)
                    await self._apply_table_state(table_id, reference.game_name or "unknown", state)

            except asyncio.CancelledError:
                raise
            except BgaTableUnavailableError as exc:
                LOGGER.warning(tr("table_unavailable_autounwatch", table_id=table_id, error=exc))
                subscriptions = await self._subscriptions_for_table(table_id)
                if subscriptions:
                    await self._finalize_finished_table(subscriptions, table_id)
                else:
                    self._table_tasks.pop(table_id, None)
                return
            except BgaRateLimitError as exc:
                # BGA asked us to back off for a specific duration; honour it
                # exactly and do not penalise the backoff multiplier.
                wait = min(exc.retry_after, 120)
                LOGGER.warning(
                    tr("bga_rate_limited", table_id=table_id, retry_after=wait)
                )
                await asyncio.sleep(wait)
            except BgaServerError as exc:
                # BGA returned a 5xx error.  Record it for site-wide detection.
                self._table_server_error_at[table_id] = time.monotonic()
                consecutive = self._table_consecutive_server_errors.get(table_id, 0) + 1
                self._table_consecutive_server_errors[table_id] = consecutive
                if self._is_site_wide_outage():
                    # Many tables are failing — likely BGA itself is down.  Wait
                    # quietly without marking individual tables as errored in Discord.
                    LOGGER.warning(
                        tr(
                            "bga_site_down",
                            table_id=table_id,
                            error=exc,
                            error_count=sum(
                                1 for tid in self._table_tasks
                                if (time.monotonic() - self._table_server_error_at.get(tid, 0.0))
                                < self._SERVER_ERROR_WINDOW_SECONDS
                            ),
                            total_count=len(self._table_tasks),
                        )
                    )
                    # Use a longer wait for site-wide outages to reduce hammering.
                    wait = min(backoff_seconds * 2, 120)
                    await asyncio.sleep(wait)
                    backoff_seconds = min(backoff_seconds * 2, 120)
                else:
                    # Isolated table error — update Discord message after threshold.
                    LOGGER.warning(
                        tr(
                            "bga_table_server_error",
                            table_id=table_id,
                            consecutive=consecutive,
                            error=exc,
                        )
                    )
                    if consecutive >= self._SERVER_ERROR_DISCORD_THRESHOLD:
                        note = tr("table_server_error_note")
                        if self._table_error_note.get(table_id) != note:
                            self._table_error_note[table_id] = note
                            await self._push_error_note_to_discord(table_id)
                    await asyncio.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, 120)
            except BgaNotPublicError as exc:
                LOGGER.warning(tr("table_not_public", table_id=table_id, error=exc))
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)
            except BgaClientError as exc:
                LOGGER.error(tr("websocket_error", table_id=table_id, error=exc))
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)
            except Exception:
                LOGGER.exception(tr("unexpected_worker_error", table_id=table_id))
                consecutive = self._table_consecutive_server_errors.get(table_id, 0) + 1
                self._table_consecutive_server_errors[table_id] = consecutive
                if consecutive >= self._SERVER_ERROR_DISCORD_THRESHOLD:
                    note = tr("table_unexpected_error_note")
                    if self._table_error_note.get(table_id) != note:
                        self._table_error_note[table_id] = note
                        await self._push_error_note_to_discord(table_id)
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)

    async def _apply_table_state(self, table_id: str, fallback_game_name: str, state) -> None:
        subscriptions = await self._subscriptions_for_table(table_id)
        if not subscriptions:
            return

        merged_player_names = self._merge_player_names(subscriptions)
        merged_player_names.update(state.player_names)
        waiting_ids = state.waiting_ids if state.waiting_ids is not None else self._select_previous_waiting_ids(subscriptions)
        merged_player_names = await self._refresh_missing_player_names(
            subscriptions=subscriptions,
            table_id=table_id,
            fallback_game_name=fallback_game_name,
            waiting_ids=waiting_ids,
            player_names=merged_player_names,
        )
        # Links are global, so one pass covers every server watching this table.
        await self.db.enrich_linked_users_from_players(merged_player_names)

        if state.is_game_finished:
            LOGGER.info(tr("table_finished_public", table_id=table_id))
            await self._finalize_finished_table(subscriptions, table_id)
            return

        table_packet_id = state.highest_packet_id or max(item.last_packet_id for item in subscriptions)
        LOGGER.info(
            tr(
                "table_state",
                table_id=table_id,
                packet_id=table_packet_id,
                waiting_ids=state.waiting_ids,
                source=state.source,
                details=state.details,
            )
        )

        for subscription in subscriptions:
            waiting_ids = state.waiting_ids if state.waiting_ids is not None else subscription.last_waiting_ids
            current_player_names = dict(subscription.player_names)
            current_player_names.update(state.player_names)
            game_name = subscription.game_name or fallback_game_name

            if not self._can_transition_lifecycle(
                subscription.lifecycle_state, self.LIFECYCLE_IN_PROGRESS
            ):
                continue

            # In recruiting-only mode: the game has started — delete the recruiting
            # message and remove the watch subscription without posting in-progress messages.
            guild_settings = await self.db.get_guild_settings(
                subscription.guild_id,
                default_recruiting_only=self._default_recruiting_only,
            )
            if guild_settings.recruiting_only:
                active_message = self._active_messages.get(subscription.subscription_id)
                if active_message is not None:
                    await self._delete_tracked_message(
                        subscription=subscription,
                        active_message=active_message,
                        table_id=table_id,
                    )
                    self._active_messages.pop(subscription.subscription_id, None)
                await self.db.update_watch_state(
                    subscription_id=subscription.subscription_id,
                    last_packet_id=table_packet_id,
                    waiting_ids=waiting_ids,
                    player_names=current_player_names,
                    seated_player_names=current_player_names,
                    seats_total=subscription.seats_total,
                    seats_remaining=subscription.seats_remaining,
                    is_initialized=True,
                    game_name=game_name,
                    lifecycle_state=self.LIFECYCLE_IN_PROGRESS,
                    player_count=len(current_player_names),
                )
                await self.db.remove_watch_subscription(
                    table_id=subscription.table_id,
                    guild_id=subscription.guild_id,
                    channel_id=subscription.channel_id,
                    outcome="unwatched",
                )
                LOGGER.info(
                    tr(
                        "recruiting_only_unwatch",
                        table_id=table_id,
                        subscription_id=subscription.subscription_id,
                    )
                )
                continue

            await self._publish_or_update_lifecycle_message(
                subscription=subscription,
                table_id=table_id,
                lifecycle_state=self.LIFECYCLE_IN_PROGRESS,
                waiting_ids=waiting_ids,
                player_names=current_player_names,
                snapshot=None,
            )

            await self.db.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=table_packet_id,
                waiting_ids=waiting_ids,
                player_names=current_player_names,
                seated_player_names=current_player_names,
                seats_total=subscription.seats_total,
                seats_remaining=subscription.seats_remaining,
                is_initialized=True,
                game_name=game_name,
                lifecycle_state=self.LIFECYCLE_IN_PROGRESS,
                player_count=len(current_player_names),
            )

    async def _finalize_finished_table(
        self,
        subscriptions: list[WatchSubscription],
        table_id: str,
        *,
        snapshot: BgaTableSnapshot | None = None,
    ) -> None:
        if snapshot is None and subscriptions:
            reference = subscriptions[0]
            try:
                snapshot = await asyncio.to_thread(
                    self.bga_client.fetch_public_table_snapshot,
                    table_id,
                    reference.base_url or BASE_URL,
                )
            except BgaClientError:
                snapshot = None
        for subscription in subscriptions:
            if not self._can_transition_lifecycle(
                subscription.lifecycle_state, self.LIFECYCLE_FINISHED
            ):
                continue

            await self._publish_or_update_lifecycle_message(
                subscription=subscription,
                table_id=table_id,
                lifecycle_state=self.LIFECYCLE_FINISHED,
                waiting_ids=[],
                player_names=dict(subscription.player_names),
                snapshot=snapshot,
            )
            await self.db.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=subscription.last_packet_id,
                waiting_ids=[],
                player_names=dict(subscription.player_names),
                seated_player_names=(
                    dict(snapshot.player_names)
                    if snapshot is not None and snapshot.player_names
                    else dict(subscription.seated_player_names or subscription.player_names)
                ),
                seats_total=(
                    snapshot.seats_total
                    if snapshot is not None and snapshot.seats_total is not None
                    else subscription.seats_total
                ),
                seats_remaining=(
                    snapshot.seats_remaining
                    if snapshot is not None and snapshot.seats_remaining is not None
                    else subscription.seats_remaining
                ),
                is_initialized=True,
                game_name=subscription.game_name,
                lifecycle_state=self.LIFECYCLE_FINISHED,
                winner_names=snapshot.winner_names if snapshot is not None else None,
                final_standings=snapshot.final_standings if snapshot is not None else None,
                player_count=(
                    len(snapshot.player_names)
                    if snapshot is not None and snapshot.player_names
                    else len(subscription.seated_player_names or subscription.player_names)
                ),
            )

            await self.db.remove_watch_subscription(
                table_id=subscription.table_id,
                guild_id=subscription.guild_id,
                channel_id=subscription.channel_id,
                outcome="finished",
            )
            self._active_messages.pop(subscription.subscription_id, None)

        self._table_tasks.pop(table_id, None)
        LOGGER.info(tr("table_finished_cleanup", table_id=table_id))

    async def _delete_tracked_message(
        self,
        *,
        subscription: WatchSubscription,
        active_message: ActiveTableMessage,
        table_id: str,
    ) -> bool:
        if not self.discord.io_allowed():
            return False
        channel = await self._resolve_channel(subscription, table_id)
        if not self._is_supported_message_channel(channel):
            return False

        message = active_message.message
        try:
            result = await self.discord.call(
                "delete_message",
                table_id,
                lambda: message.delete(),
                channel_id=str(channel.id),
                operation_kind="delete",
            )
            if result is None:
                # Retries exhausted — treat as a failed delete.
                LOGGER.error(
                    tr(
                        "watch_message_delete_failed",
                        table_id=table_id,
                        message_kind=active_message.kind,
                        error="rate-limit retries exhausted",
                    )
                )
                return False
            LOGGER.info(
                tr(
                    "watch_message_deleted",
                    table_id=table_id,
                    message_kind=active_message.kind,
                )
            )
            return True
        except discord.NotFound:
            LOGGER.info(
                tr(
                    "watch_message_missing_delete",
                    table_id=table_id,
                    message_kind=active_message.kind,
                )
            )
            return True
        except discord.DiscordException as exc:
            LOGGER.error(
                tr(
                    "watch_message_delete_failed",
                    table_id=table_id,
                    message_kind=active_message.kind,
                    error=exc,
                )
            )
            return False

    async def _apply_invite_state(
        self,
        table_id: str,
        subscriptions: list[WatchSubscription],
        snapshot: BgaTableSnapshot,
    ) -> None:
        for subscription in subscriptions:
            if not self._can_transition_lifecycle(
                subscription.lifecycle_state, self.LIFECYCLE_RECRUITING
            ):
                continue
            player_names = dict(subscription.player_names)
            player_names.update(snapshot.player_names)
            await self.db.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=subscription.last_packet_id,
                waiting_ids=[],
                player_names=player_names,
                seated_player_names=dict(snapshot.player_names),
                seats_total=snapshot.seats_total,
                seats_remaining=snapshot.seats_remaining,
                is_initialized=False,
                game_name=snapshot.game_name or subscription.game_name,
                lifecycle_state=self.LIFECYCLE_RECRUITING,
                player_count=len(snapshot.player_names),
            )
            await self._publish_or_update_lifecycle_message(
                subscription=subscription,
                table_id=table_id,
                lifecycle_state=self.LIFECYCLE_RECRUITING,
                waiting_ids=[],
                player_names=player_names,
                snapshot=snapshot,
            )

    def _message_lock(self, subscription_id: int) -> asyncio.Lock:
        lock = self._message_locks.get(subscription_id)
        if lock is None:
            lock = self._message_locks[subscription_id] = asyncio.Lock()
        return lock

    async def _publish_or_update_lifecycle_message(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        lifecycle_state: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        snapshot: BgaTableSnapshot | None,
    ) -> None:
        async with self._message_lock(subscription.subscription_id):
            await self._publish_or_update_lifecycle_message_locked(
                subscription=subscription,
                table_id=table_id,
                lifecycle_state=lifecycle_state,
                waiting_ids=waiting_ids,
                player_names=player_names,
                snapshot=snapshot,
            )

    async def _publish_or_update_lifecycle_message_locked(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        lifecycle_state: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        snapshot: BgaTableSnapshot | None,
    ) -> None:
        if not self.discord.io_allowed():
            return

        channel = await self._resolve_channel(subscription, table_id)
        if not self._is_supported_message_channel(channel):
            return

        card = self._card_data(
            subscription=subscription,
            table_id=table_id,
            lifecycle_state=lifecycle_state,
            waiting_ids=waiting_ids,
            player_names=player_names,
            snapshot=snapshot,
        )
        payload_signature = cards.card_signature(card)
        active_message = self._active_messages.get(subscription.subscription_id)
        if (
            active_message is not None
            and active_message.kind == lifecycle_state
            and active_message.payload_signature == payload_signature
        ):
            return

        message = await self._resolve_tracked_message(subscription, channel)
        layout_view = cards.build_card(card, await self._linked_users_for_card(card))

        if message is None:
            sent = await self.discord.call(
                "send_lifecycle",
                table_id,
                lambda: channel.send(view=layout_view),
                channel_id=str(channel.id),
                operation_kind="send",
            )
            if sent is None:
                LOGGER.error(
                    tr(
                        "notification_send_failed",
                        table_id=table_id,
                        channel_id=subscription.channel_id,
                        error="rate-limit retries exhausted",
                    )
                )
                return
            message = sent
        else:
            try:
                edited = await self.discord.call(
                    "edit_lifecycle",
                    table_id,
                    lambda: message.edit(view=layout_view),
                    channel_id=str(channel.id),
                    operation_kind="edit",
                    message_created_at=message.created_at,
                )
                if edited is None:
                    return
            except discord.NotFound:
                await self.db.update_watch_message_tracking(
                    subscription_id=subscription.subscription_id,
                    lifecycle_state=self._normalize_lifecycle_state(subscription.lifecycle_state),
                    tracked_message_id=None,
                    tracked_message_kind=None,
                )
                self._active_messages.pop(subscription.subscription_id, None)
                return await self._publish_or_update_lifecycle_message_locked(
                    subscription=subscription,
                    table_id=table_id,
                    lifecycle_state=lifecycle_state,
                    waiting_ids=waiting_ids,
                    player_names=player_names,
                    snapshot=snapshot,
                )
            except discord.HTTPException as exc:
                # Discord rejects editing a legacy embed message with V2 components
                # (HTTP 400). Delete the stale message and send a fresh V2 one.
                if exc.status != 400:
                    LOGGER.error(tr("turn_message_update_failed", table_id=table_id, error=exc))
                    return
                try:
                    await self.discord.call(
                        "delete_legacy_lifecycle",
                        table_id,
                        lambda: message.delete(),
                        channel_id=str(channel.id),
                        operation_kind="delete",
                    )
                except discord.DiscordException:
                    pass
                await self.db.update_watch_message_tracking(
                    subscription_id=subscription.subscription_id,
                    lifecycle_state=self._normalize_lifecycle_state(subscription.lifecycle_state),
                    tracked_message_id=None,
                    tracked_message_kind=None,
                )
                self._active_messages.pop(subscription.subscription_id, None)
                return await self._publish_or_update_lifecycle_message_locked(
                    subscription=subscription,
                    table_id=table_id,
                    lifecycle_state=lifecycle_state,
                    waiting_ids=waiting_ids,
                    player_names=player_names,
                    snapshot=snapshot,
                )
            except discord.DiscordException as exc:
                LOGGER.error(tr("turn_message_update_failed", table_id=table_id, error=exc))
                return

        self._active_messages[subscription.subscription_id] = ActiveTableMessage(
            message=message,
            kind=lifecycle_state,
            waiting_ids=list(waiting_ids),
            payload_signature=payload_signature,
        )
        await self.db.update_watch_message_tracking(
            subscription_id=subscription.subscription_id,
            lifecycle_state=lifecycle_state,
            tracked_message_id=message.id,
            tracked_message_kind=lifecycle_state,
        )

    def _card_data(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        lifecycle_state: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        snapshot: BgaTableSnapshot | None,
    ) -> cards.CardData:
        return cards.CardData(
            lifecycle_state=lifecycle_state,
            table_id=table_id,
            subscription=subscription,
            waiting_ids=list(waiting_ids),
            player_names=dict(player_names),
            snapshot=snapshot,
            cover_image_url=self._cover_image_urls.get(table_id),
            error_note=self._table_error_note.get(table_id),
        )

    async def _linked_users_for_card(self, card: cards.CardData) -> list[LinkedUser]:
        try:
            return await self.db.get_linked_users_for_players(card.players_to_link())
        except sqlite3.Error as exc:
            LOGGER.warning("Linked-user lookup failed while building table message: %s", exc)
            return []

    async def _resolve_tracked_message(
        self,
        subscription: WatchSubscription,
        channel: discord.TextChannel | discord.Thread,
    ) -> discord.Message | None:
        active = self._active_messages.get(subscription.subscription_id)
        if active is not None:
            return active.message
        if subscription.tracked_message_id is None:
            return None
        try:
            message = await self.discord.call(
                "fetch_tracked_message",
                "?",
                lambda mid=subscription.tracked_message_id: channel.fetch_message(mid),
            )
        except discord.NotFound:
            await self.db.update_watch_message_tracking(
                subscription_id=subscription.subscription_id,
                lifecycle_state=self._normalize_lifecycle_state(subscription.lifecycle_state),
                tracked_message_id=None,
                tracked_message_kind=None,
            )
            return None
        except discord.DiscordException:
            return None
        if message is None:
            # Rate-limit retries exhausted; caller will re-attempt next tick.
            return None
        self._active_messages[subscription.subscription_id] = ActiveTableMessage(
            message=message,
            kind=subscription.tracked_message_kind or subscription.lifecycle_state,
            waiting_ids=list(subscription.last_waiting_ids),
        )
        return message

    async def _refresh_missing_player_names(
        self,
        *,
        subscriptions: list[WatchSubscription],
        table_id: str,
        fallback_game_name: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
    ) -> dict[str, str]:
        missing_player_ids = [player_id for player_id in waiting_ids if not player_names.get(player_id, "").strip()]
        if not missing_player_ids:
            return player_names

        last_refresh_at = self._last_player_name_refresh_at.get(table_id, 0.0)
        now = time.monotonic()
        if now - last_refresh_at < 60:
            return player_names

        reference = subscriptions[0]
        if not reference.table_url or not reference.base_url:
            return player_names

        self._last_player_name_refresh_at[table_id] = now
        table_info = self.bga_client.build_public_table_info(
            table_id=reference.table_id,
            table_url=reference.table_url,
            base_url=reference.base_url,
            gameserver=reference.gameserver or "",
            game_name=reference.game_name or fallback_game_name,
        )

        try:
            refreshed_names = await asyncio.to_thread(self.bga_client.fetch_public_player_names, table_info)
        except BgaClientError as exc:
            LOGGER.debug(tr("player_name_refresh_failed", table_id=table_id, error=exc))
            return player_names

        if not refreshed_names:
            return player_names

        merged_names = dict(player_names)
        merged_names.update(refreshed_names)
        resolved_missing_ids = [
            player_id
            for player_id in missing_player_ids
            if merged_names.get(player_id, "").strip()
        ]
        if resolved_missing_ids:
            LOGGER.info(
                tr(
                    "player_name_refresh_success",
                    table_id=table_id,
                    count=len(resolved_missing_ids),
                )
            )
        return merged_names

    async def _resolve_channel(self, subscription: WatchSubscription, table_id: str) -> discord.abc.Messageable | None:
        guild_settings = await self.db.get_guild_settings(
            subscription.guild_id,
            default_forced_channel_id=self._default_forced_channel_id,
        )
        channel_id = subscription.channel_id
        channel = None
        forced_channel_id = guild_settings.forced_channel_id or ""
        if forced_channel_id:
            # Only honour a forced channel that belongs to this subscription's own
            # server, so a bad setting can never route posts into another server.
            guild = self.bot.get_guild(int(subscription.guild_id))
            forced_channel = (
                guild.get_channel_or_thread(int(forced_channel_id))
                if guild is not None and forced_channel_id.isascii() and forced_channel_id.isdecimal()
                else None
            )
            if forced_channel is not None:
                channel_id = forced_channel_id
                channel = forced_channel
            else:
                LOGGER.debug(
                    "Ignoring forced channel %s: not a channel of guild %s (table %s).",
                    forced_channel_id,
                    subscription.guild_id,
                    table_id,
                )
        if channel is None:
            channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            if not self.discord.io_allowed():
                return None
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.DiscordException as exc:
                LOGGER.error(
                    tr(
                        "channel_fetch_failed",
                        channel_id=channel_id,
                        table_id=table_id,
                        error=exc,
                    )
                )
                return None

        if not isinstance(channel, discord.abc.Messageable):
            LOGGER.error(
                tr(
                    "channel_not_messageable",
                    channel_id=channel_id,
                    table_id=table_id,
                )
            )
            return None
        return channel

    async def wait_for_active_messages(
        self,
        subscription_ids: list[int],
        *,
        timeout_seconds: float = 20.0,
        poll_seconds: float = 0.5,
    ) -> bool:
        """Wait until every subscription shows its table card (not a link status note)."""
        pending = set(subscription_ids)
        if not pending:
            return False

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            if all(
                (active := self._active_messages.get(subscription_id)) is not None
                and active.kind != self.LINK_STATUS_KIND
                for subscription_id in pending
            ):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(max(0.1, poll_seconds))

    # ------------------------------------------------------------------
    # Link status notes ("link detected" → table card, or → error)
    # ------------------------------------------------------------------

    async def send_link_status(
        self,
        channel: discord.abc.Messageable,
        *,
        table_id: str,
        text: str,
    ) -> discord.Message | None:
        """Post a status note; returns ``None`` if it could not be sent."""
        try:
            return await self.discord.call(
                "send_link_status",
                table_id,
                lambda: channel.send(view=cards.build_link_status_view(text, color=discord.Color.light_grey())),
                channel_id=str(getattr(channel, "id", "")) or None,
                operation_kind="send",
            )
        except discord.DiscordException as exc:
            LOGGER.warning("Could not post link status for table %s: %s", table_id, exc)
            return None

    async def edit_link_status(
        self,
        message: discord.Message,
        *,
        table_id: str,
        text: str,
        color: discord.Color,
        table_url: str | None = None,
        delete_after: float | None = None,
    ) -> bool:
        view = cards.build_link_status_view(text, color=color, table_url=table_url)
        try:
            edited = await self.discord.call(
                "edit_link_status",
                table_id,
                lambda: message.edit(view=view, delete_after=delete_after),
                channel_id=str(message.channel.id),
                operation_kind="edit",
                message_created_at=message.created_at,
            )
        except discord.DiscordException as exc:
            LOGGER.warning("Could not update link status for table %s: %s", table_id, exc)
            return False
        return edited is not None

    async def adopt_link_status_message(
        self,
        subscription: WatchSubscription,
        message: discord.Message,
    ) -> str:
        """Make the status note *message* the tracked message for *subscription*.

        The table worker then edits the note into the table card instead of
        posting a new message. Returns:

        - ``"adopted"``: the note is now the card's message; an older card for
          the same table was deleted (re-posting a link moves the card down);
        - ``"card_posted"``: the worker posted a card after the note while the
          link was being registered, so the note was deleted instead;
        - ``"other_channel"``: the table posts to a forced channel elsewhere,
          so the note stays a standalone note.
        """
        channel = await self._resolve_channel(subscription, subscription.table_id)
        if channel is None or getattr(channel, "id", None) != message.channel.id:
            return "other_channel"

        async with self._message_lock(subscription.subscription_id):
            previous = self._active_messages.get(subscription.subscription_id)
            previous_message: discord.Message | None = previous.message if previous is not None else None
            if previous_message is None and subscription.tracked_message_id not in (None, message.id):
                previous_message = await self._resolve_tracked_message(subscription, channel)

            if previous_message is not None and previous_message.id > message.id:
                # Snowflakes grow over time: this card is newer than the note.
                await self._delete_quietly(message, subscription.table_id, operation="delete_link_status")
                return "card_posted"

            self._active_messages[subscription.subscription_id] = ActiveTableMessage(
                message=message,
                kind=self.LINK_STATUS_KIND,
                waiting_ids=[],
            )
            await self.db.update_watch_message_tracking(
                subscription_id=subscription.subscription_id,
                lifecycle_state=self._normalize_lifecycle_state(subscription.lifecycle_state),
                tracked_message_id=message.id,
                tracked_message_kind=self.LINK_STATUS_KIND,
            )
            if previous_message is not None and previous_message.id != message.id:
                await self._delete_quietly(previous_message, subscription.table_id, operation="delete_replaced_card")
        return "adopted"

    async def _delete_quietly(self, message: discord.Message, table_id: str, *, operation: str) -> None:
        try:
            await self.discord.call(
                operation,
                table_id,
                lambda: message.delete(),
                channel_id=str(message.channel.id),
                operation_kind="delete",
            )
        except discord.NotFound:
            pass
        except discord.DiscordException as exc:
            LOGGER.warning("Could not delete message %s for table %s: %s", message.id, table_id, exc)

    async def redraw_card_from_saved_state(self, subscription_id: int) -> None:
        """Render a table card from the stored state, without waiting for BGA.

        Used when a link for an already-watched table is posted again: the stored
        state is current, so the adopted note can become the card immediately.
        """
        subscription = await self.db.get_watch_subscription(subscription_id)
        if subscription is None:
            return
        lifecycle_state = self._normalize_lifecycle_state(subscription.lifecycle_state)
        if lifecycle_state == self.LIFECYCLE_FINISHED:
            return
        if lifecycle_state == self.LIFECYCLE_IN_PROGRESS and not subscription.is_initialized:
            return
        await self._publish_or_update_lifecycle_message(
            subscription=subscription,
            table_id=subscription.table_id,
            lifecycle_state=lifecycle_state,
            waiting_ids=list(subscription.last_waiting_ids),
            player_names=dict(subscription.player_names),
            snapshot=None,
        )

    async def mark_link_status_waiting(self, subscription: WatchSubscription) -> None:
        """If the adopted note still says "registering", say it is waiting on BGA."""
        async with self._message_lock(subscription.subscription_id):
            active = self._active_messages.get(subscription.subscription_id)
            if active is None or active.kind != self.LINK_STATUS_KIND:
                return
            await self.edit_link_status(
                active.message,
                table_id=subscription.table_id,
                text=tr("link_status_waiting", table_id=subscription.table_id),
                color=discord.Color.light_grey(),
                table_url=subscription.table_url or build_table_url(subscription.table_id),
            )

    async def reset_tracked_message(self, subscription: WatchSubscription) -> bool:
        active_message = self._active_messages.get(subscription.subscription_id)
        refreshed_subscription = subscription

        if active_message is None and subscription.tracked_message_id is not None:
            channel = await self._resolve_channel(subscription, subscription.table_id)
            if not self._is_supported_message_channel(channel):
                return False
            message = await self._resolve_tracked_message(subscription, channel)
            refreshed_subscription = await self.db.get_watch_subscription(subscription.subscription_id) or subscription
            if message is None:
                if refreshed_subscription.tracked_message_id is not None:
                    return False
            else:
                active_message = self._active_messages.get(subscription.subscription_id)

        if active_message is not None:
            deleted = await self._delete_tracked_message(
                subscription=refreshed_subscription,
                active_message=active_message,
                table_id=refreshed_subscription.table_id,
            )
            if not deleted:
                return False

        self._active_messages.pop(subscription.subscription_id, None)
        await self.db.update_watch_message_tracking(
            subscription_id=subscription.subscription_id,
            lifecycle_state=self._normalize_lifecycle_state(refreshed_subscription.lifecycle_state),
            tracked_message_id=None,
            tracked_message_kind=None,
        )
        return True

    @staticmethod
    def _is_supported_message_channel(channel: object | None) -> bool:
        return isinstance(channel, (discord.TextChannel, discord.Thread))

    async def delete_discord_message(
        self,
        message: discord.Message,
        *,
        operation: str = "delete",
    ) -> bool:
        """Delete *message* via the rate-limit-aware retry wrapper.

        Returns ``True`` if the message was deleted (or was already gone),
        ``False`` if rate-limit retries were exhausted or Discord I/O is
        currently paused.
        """
        if not self.discord.io_allowed():
            return False
        try:
            result = await self.discord.call(
                operation,
                "?",
                lambda: message.delete(),
                channel_id=str(message.channel.id),
                operation_kind="delete",
            )
            return result is not None
        except discord.NotFound:
            return True
        except discord.DiscordException:
            return False

    async def _sync_subscriptions_from_snapshot(
        self,
        subscriptions: list[WatchSubscription],
        snapshot: BgaTableSnapshot,
    ) -> list[WatchSubscription]:
        updated = False
        for subscription in subscriptions:
            desired_url = snapshot.table_url or subscription.table_url or build_table_url(snapshot.table_id)
            desired_gameserver = snapshot.gameserver or subscription.gameserver or ""
            desired_game_name = snapshot.game_name or subscription.game_name
            desired_base_url = subscription.base_url or BASE_URL
            if (
                subscription.table_url == desired_url
                and (subscription.gameserver or "") == desired_gameserver
                and subscription.game_name == desired_game_name
                and (subscription.base_url or "") == desired_base_url
            ):
                continue
            await self.db.upsert_watch_subscription(
                table_id=subscription.table_id,
                table_url=desired_url,
                base_url=desired_base_url,
                gameserver=desired_gameserver,
                guild_id=subscription.guild_id,
                channel_id=subscription.channel_id,
                created_by_discord_user_id=subscription.created_by_discord_user_id,
                game_name=desired_game_name,
            )
            updated = True
        if updated:
            return await self._subscriptions_for_table(snapshot.table_id)
        return subscriptions

    async def _subscriptions_for_table(self, table_id: str) -> list[WatchSubscription]:
        return [item for item in await self.db.list_watch_subscriptions() if item.table_id == table_id]

    @classmethod
    def _normalize_lifecycle_state(cls, lifecycle_state: str | None) -> str:
        if lifecycle_state in cls._LIFECYCLE_ORDER:
            return lifecycle_state
        return cls.LIFECYCLE_RECRUITING

    @classmethod
    def _can_transition_lifecycle(cls, current: str | None, target: str) -> bool:
        current_state = cls._normalize_lifecycle_state(current)
        return cls._LIFECYCLE_ORDER[target] >= cls._LIFECYCLE_ORDER[current_state]

    @staticmethod
    def _select_previous_waiting_ids(subscriptions: list[WatchSubscription]) -> list[str]:
        initialized_subscriptions = [item for item in subscriptions if item.is_initialized]
        if not initialized_subscriptions:
            return []
        initialized_subscriptions.sort(key=lambda item: item.last_packet_id, reverse=True)
        return initialized_subscriptions[0].last_waiting_ids

    @staticmethod
    def _merge_player_names(subscriptions: list[WatchSubscription]) -> dict[str, str]:
        merged: dict[str, str] = {}
        for subscription in subscriptions:
            merged.update(subscription.player_names)
        return merged
