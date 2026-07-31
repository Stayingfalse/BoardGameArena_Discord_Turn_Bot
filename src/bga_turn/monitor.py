from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import tasks

from .bga_client import BgaClient, BgaClientError, BgaNotPublicError, BgaTableUnavailableError
from .database import Database
from .i18n import tr
from .models import BgaTableInfo, BgaTableSnapshot, LinkedUser, WatchSubscription
from .utils import BASE_URL, build_table_url, format_game_name

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveTableMessage:
    message: discord.Message
    kind: str
    waiting_ids: list[str]


@dataclass(slots=True)
class FollowSyncResult:
    player_name: str
    added: list[BgaTableInfo]
    already_watched: list[BgaTableInfo]


class BgaMonitor:
    LIFECYCLE_RECRUITING = "recruiting"
    LIFECYCLE_IN_PROGRESS = "in_progress"
    LIFECYCLE_FINISHED = "finished"
    _LIFECYCLE_ORDER = {
        LIFECYCLE_RECRUITING: 0,
        LIFECYCLE_IN_PROGRESS: 1,
        LIFECYCLE_FINISHED: 2,
    }
    # Followed players are re-scanned far less often than tables are polled: each
    # scan costs two HTTP round-trips to BGA and a new table only appears when a
    # player joins one, which is a human-scale event.
    _FOLLOW_SYNC_INTERVAL_SECONDS = 180.0
    # A table that just finished is unwatched by `_finalize_finished_table`, but BGA
    # can still list it as being played for a short while. Without this cooldown the
    # next follow scan would re-watch it and republish a turn message for a dead game.
    _FINISHED_TABLE_COOLDOWN_SECONDS = 3600.0

    def __init__(
        self,
        bot: discord.Client,
        database: Database,
        bga_client: BgaClient,
        poll_seconds: int,
        *,
        recruiting_only: bool = False,
        forced_channel_id: str | None = None,
    ) -> None:
        self.bot = bot
        self.database = database
        self.bga_client = bga_client
        self._poll_seconds = max(5, poll_seconds)
        self._recruiting_only = recruiting_only
        self._forced_channel_id = forced_channel_id
        self._table_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_messages: dict[int, ActiveTableMessage] = {}
        self._last_player_name_refresh_at: dict[str, float] = {}
        self._last_follow_sync_at: dict[tuple[str, str, str], float] = {}
        self._recently_finished_tables: dict[str, float] = {}
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
        self._last_player_name_refresh_at.clear()
        self._last_follow_sync_at.clear()
        self._recently_finished_tables.clear()

    @tasks.loop(seconds=30)
    async def sync_tables(self) -> None:
        await self._sync_tables_once()

    async def refresh_now(self) -> None:
        await self._sync_tables_once()

    async def _sync_tables_once(self) -> None:
        # Runs first so tables discovered for followed players get a worker in this
        # same pass instead of waiting for the next tick.
        await self._sync_followed_players()

        subscriptions = self.database.list_watch_subscriptions()
        active_table_ids = {subscription.table_id for subscription in subscriptions}
        active_subscription_ids = {subscription.subscription_id for subscription in subscriptions}

        for subscription_id in list(self._active_messages):
            if subscription_id not in active_subscription_ids:
                active_message = self._active_messages.pop(subscription_id, None)
                if active_message is not None:
                    try:
                        await active_message.message.delete()
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

    async def sync_followed_player(
        self,
        *,
        guild_id: str,
        discord_user_id: str,
        channel_id: str,
        bga_player_id: str,
        created_by_discord_user_id: str,
    ) -> FollowSyncResult:
        """Watch every table the player currently sits at that this channel misses.

        Raises ``BgaClientError`` (or ``BgaPlayerNotFoundError``) if BGA cannot be
        queried, so the caller can report it. Marks the follow as freshly synced so
        an immediate call from the slash command is not repeated by the next tick.
        """
        self._last_follow_sync_at[(guild_id, discord_user_id, channel_id)] = time.monotonic()
        player_tables = await asyncio.to_thread(self.bga_client.fetch_player_tables, bga_player_id)

        subscriptions = await asyncio.to_thread(self.database.list_watch_subscriptions)
        watched_table_ids = {
            item.table_id
            for item in subscriptions
            if item.guild_id == guild_id and item.channel_id == channel_id
        }

        added: list[BgaTableInfo] = []
        already_watched: list[BgaTableInfo] = []
        for table in player_tables.tables:
            if table.table_id in watched_table_ids:
                already_watched.append(table)
                continue
            if self._is_recently_finished(table.table_id):
                continue
            await asyncio.to_thread(
                self.database.upsert_watch_subscription,
                table_id=table.table_id,
                table_url=table.table_url,
                base_url=table.base_url,
                gameserver=table.gameserver,
                guild_id=guild_id,
                channel_id=channel_id,
                created_by_discord_user_id=created_by_discord_user_id,
                game_name=table.game_name,
            )
            added.append(table)

        return FollowSyncResult(
            player_name=player_tables.player_name,
            added=added,
            already_watched=already_watched,
        )

    async def _sync_followed_players(self) -> None:
        follows = await asyncio.to_thread(self.database.list_followed_players)
        follow_keys = {(item.guild_id, item.discord_user_id, item.channel_id) for item in follows}
        for stale_key in set(self._last_follow_sync_at) - follow_keys:
            del self._last_follow_sync_at[stale_key]
        if not follows:
            return

        now = time.monotonic()
        for follow in follows:
            follow_key = (follow.guild_id, follow.discord_user_id, follow.channel_id)
            if now - self._last_follow_sync_at.get(follow_key, 0.0) < self._FOLLOW_SYNC_INTERVAL_SECONDS:
                continue
            self._last_follow_sync_at[follow_key] = now

            linked_user = await asyncio.to_thread(
                self.database.get_linked_user, follow.guild_id, follow.discord_user_id
            )
            if linked_user is None or not linked_user.bga_player_id.strip():
                LOGGER.warning(
                    tr("follow_sync_skipped_without_id", discord_user_id=follow.discord_user_id)
                )
                continue

            try:
                result = await self.sync_followed_player(
                    guild_id=follow.guild_id,
                    discord_user_id=follow.discord_user_id,
                    channel_id=follow.channel_id,
                    bga_player_id=linked_user.bga_player_id,
                    created_by_discord_user_id=follow.created_by_discord_user_id,
                )
            except BgaClientError as exc:
                LOGGER.warning(
                    tr(
                        "follow_sync_failed",
                        discord_user_id=follow.discord_user_id,
                        bga_player_id=linked_user.bga_player_id,
                        error=exc,
                    )
                )
                continue

            if result.added:
                LOGGER.info(
                    tr(
                        "follow_sync_added",
                        count=len(result.added),
                        bga_player_id=linked_user.bga_player_id,
                        channel_id=follow.channel_id,
                        table_ids=", ".join(item.table_id for item in result.added),
                    )
                )

    def _remember_finished_table(self, table_id: str) -> None:
        self._recently_finished_tables[table_id] = time.monotonic()

    def _is_recently_finished(self, table_id: str) -> bool:
        finished_at = self._recently_finished_tables.get(table_id)
        if finished_at is None:
            return False
        if time.monotonic() - finished_at > self._FINISHED_TABLE_COOLDOWN_SECONDS:
            del self._recently_finished_tables[table_id]
            return False
        return True

    async def _run_table_worker(self, table_id: str) -> None:
        backoff_seconds = 5
        did_cleanup = False
        while True:
            try:
                subscriptions = self._subscriptions_for_table(table_id)
                if not subscriptions:
                    return

                reference = subscriptions[0]
                if not did_cleanup:
                    await self._cleanup_stale_table_messages(subscriptions, table_id)
                    did_cleanup = True
                base_url = reference.base_url or BASE_URL
                snapshot = await asyncio.to_thread(
                    self.bga_client.fetch_public_table_snapshot,
                    table_id,
                    base_url,
                )
                subscriptions = await self._sync_subscriptions_from_snapshot(subscriptions, snapshot)
                reference = subscriptions[0]
                if snapshot.is_finished:
                    await self._finalize_finished_table(subscriptions, table_id, snapshot=snapshot)
                    return
                if not snapshot.can_watch_turns:
                    await self._apply_invite_state(table_id, subscriptions, snapshot)
                    backoff_seconds = 5
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

                async for state in self.bga_client.watch_table(
                    table_info,
                    current_waiting_ids=current_waiting_ids,
                    known_player_names=known_player_names,
                ):
                    current_waiting_ids = state.waiting_ids or current_waiting_ids
                    known_player_names.update(state.player_names)
                    await self._apply_table_state(table_id, reference.game_name or "unknown", state)

                backoff_seconds = 5
            except asyncio.CancelledError:
                raise
            except BgaTableUnavailableError as exc:
                LOGGER.warning(tr("table_unavailable_autounwatch", table_id=table_id, error=exc))
                subscriptions = self._subscriptions_for_table(table_id)
                if subscriptions:
                    await self._finalize_finished_table(subscriptions, table_id)
                else:
                    self._table_tasks.pop(table_id, None)
                return
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
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60)

    async def _apply_table_state(self, table_id: str, fallback_game_name: str, state) -> None:
        subscriptions = self._subscriptions_for_table(table_id)
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
        for guild_id in {subscription.guild_id for subscription in subscriptions}:
            await asyncio.to_thread(
                self.database.enrich_linked_users_from_players, guild_id, merged_player_names
            )

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
            if self._recruiting_only:
                active_message = self._active_messages.get(subscription.subscription_id)
                if active_message is not None:
                    await self._delete_tracked_message(
                        subscription=subscription,
                        active_message=active_message,
                        table_id=table_id,
                    )
                    self._active_messages.pop(subscription.subscription_id, None)
                self.database.remove_watch_subscription(
                    table_id=subscription.table_id,
                    guild_id=subscription.guild_id,
                    channel_id=subscription.channel_id,
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

            self.database.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=table_packet_id,
                waiting_ids=waiting_ids,
                player_names=current_player_names,
                is_initialized=True,
                game_name=game_name,
                lifecycle_state=self.LIFECYCLE_IN_PROGRESS,
            )

    async def _finalize_finished_table(
        self,
        subscriptions: list[WatchSubscription],
        table_id: str,
        *,
        snapshot: BgaTableSnapshot | None = None,
    ) -> None:
        # Block the follow scan from immediately re-watching this table while BGA
        # still advertises it as being played.
        self._remember_finished_table(table_id)
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
            self.database.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=subscription.last_packet_id,
                waiting_ids=[],
                player_names=dict(subscription.player_names),
                is_initialized=True,
                game_name=subscription.game_name,
                lifecycle_state=self.LIFECYCLE_FINISHED,
            )

            self.database.remove_watch_subscription(
                table_id=subscription.table_id,
                guild_id=subscription.guild_id,
                channel_id=subscription.channel_id,
            )
            self._active_messages.pop(subscription.subscription_id, None)

        self._table_tasks.pop(table_id, None)
        LOGGER.info(tr("table_finished_cleanup", table_id=table_id))

    async def _handle_waiting_ids_transition(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        previous_waiting_ids: list[str],
        waiting_ids: list[str],
        player_names: dict[str, str],
        game_label: str,
    ) -> None:
        active_message = self._active_messages.get(subscription.subscription_id)
        previous_set = set(previous_waiting_ids)
        waiting_set = set(waiting_ids)
        is_same_turn_progress = bool(previous_waiting_ids) and waiting_set.issubset(previous_set)

        if active_message is not None and not waiting_ids:
            deleted = await self._delete_tracked_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
            )
            if deleted:
                self._active_messages.pop(subscription.subscription_id, None)
            return

        if active_message is not None and waiting_ids and is_same_turn_progress:
            edited = await self._edit_turn_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
                waiting_ids=waiting_ids,
                player_names=player_names,
                game_label=game_label,
            )
            if edited:
                active_message.waiting_ids = list(waiting_ids)
                return

        if active_message is not None and waiting_ids and not is_same_turn_progress:
            deleted = await self._delete_tracked_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
            )
            if deleted:
                self._active_messages.pop(subscription.subscription_id, None)

        if not waiting_ids:
            return

        message = await self._publish_turn_snapshot(
            subscription=subscription,
            table_id=table_id,
            waiting_ids=waiting_ids,
            player_names=player_names,
            game_label=game_label,
        )
        if message is not None:
            self._active_messages[subscription.subscription_id] = ActiveTableMessage(
                message=message,
                kind="turn",
                waiting_ids=list(waiting_ids),
            )

    async def _publish_turn_snapshot(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        game_label: str,
    ) -> discord.Message | None:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None:
            return None

        content = await self._build_turn_message_content(
            waiting_ids=waiting_ids,
            player_names=player_names,
            table_id=table_id,
            subscription=subscription,
            game_label=game_label,
        )

        try:
            message = await channel.send(content)
            LOGGER.info(tr("notification_sent", table_id=table_id, waiting_ids=waiting_ids))
            return message
        except discord.DiscordException as exc:
            LOGGER.error(
                tr(
                    "notification_send_failed",
                    table_id=table_id,
                    channel_id=subscription.channel_id,
                    error=exc,
                )
            )
            return None

    async def _edit_turn_message(
        self,
        *,
        subscription: WatchSubscription,
        active_message: ActiveTableMessage,
        table_id: str,
        waiting_ids: list[str],
        player_names: dict[str, str],
        game_label: str,
    ) -> bool:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return False

        message = active_message.message

        content = await self._build_turn_message_content(
            waiting_ids=waiting_ids,
            player_names=player_names,
            table_id=table_id,
            subscription=subscription,
            game_label=game_label,
        )
        try:
            await message.edit(content=content)
            LOGGER.info(tr("turn_message_updated", table_id=table_id, waiting_ids=waiting_ids))
            return True
        except discord.NotFound:
            LOGGER.info(tr("turn_message_missing_update", table_id=table_id))
            return False
        except discord.DiscordException as exc:
            LOGGER.error(tr("turn_message_update_failed", table_id=table_id, error=exc))
            return False

    async def _delete_tracked_message(
        self,
        *,
        subscription: WatchSubscription,
        active_message: ActiveTableMessage,
        table_id: str,
    ) -> bool:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return False

        message = active_message.message
        try:
            await message.delete()
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
            self.database.update_watch_state(
                subscription_id=subscription.subscription_id,
                last_packet_id=subscription.last_packet_id,
                waiting_ids=[],
                player_names=player_names,
                is_initialized=False,
                game_name=snapshot.game_name or subscription.game_name,
                lifecycle_state=self.LIFECYCLE_RECRUITING,
            )
            await self._publish_or_update_lifecycle_message(
                subscription=subscription,
                table_id=table_id,
                lifecycle_state=self.LIFECYCLE_RECRUITING,
                waiting_ids=[],
                player_names=player_names,
                snapshot=snapshot,
            )

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
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return

        message = await self._resolve_tracked_message(subscription, channel)
        content, embed, view = await self._build_lifecycle_message_payload(
            lifecycle_state=lifecycle_state,
            table_id=table_id,
            subscription=subscription,
            waiting_ids=waiting_ids,
            player_names=player_names,
            snapshot=snapshot,
        )

        if message is None:
            try:
                message = await channel.send(content=content, embed=embed, view=view)
            except discord.DiscordException as exc:
                LOGGER.error(
                    tr(
                        "notification_send_failed",
                        table_id=table_id,
                        channel_id=subscription.channel_id,
                        error=exc,
                    )
                )
                return
        else:
            try:
                await message.edit(content=content, embed=embed, view=view)
            except discord.NotFound:
                self.database.update_watch_message_tracking(
                    subscription_id=subscription.subscription_id,
                    lifecycle_state=self._normalize_lifecycle_state(subscription.lifecycle_state),
                    tracked_message_id=None,
                    tracked_message_kind=None,
                )
                self._active_messages.pop(subscription.subscription_id, None)
                return await self._publish_or_update_lifecycle_message(
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
        )
        self.database.update_watch_message_tracking(
            subscription_id=subscription.subscription_id,
            lifecycle_state=lifecycle_state,
            tracked_message_id=message.id,
            tracked_message_kind=lifecycle_state,
        )

    async def _resolve_tracked_message(
        self,
        subscription: WatchSubscription,
        channel: discord.TextChannel,
    ) -> discord.Message | None:
        active = self._active_messages.get(subscription.subscription_id)
        if active is not None:
            return active.message
        if subscription.tracked_message_id is None:
            return None
        try:
            message = await channel.fetch_message(subscription.tracked_message_id)
        except discord.NotFound:
            self.database.update_watch_message_tracking(
                subscription_id=subscription.subscription_id,
                lifecycle_state=self._normalize_lifecycle_state(subscription.lifecycle_state),
                tracked_message_id=None,
                tracked_message_kind=None,
            )
            return None
        except discord.DiscordException:
            return None
        self._active_messages[subscription.subscription_id] = ActiveTableMessage(
            message=message,
            kind=subscription.tracked_message_kind or subscription.lifecycle_state,
            waiting_ids=list(subscription.last_waiting_ids),
        )
        return message

    async def _build_lifecycle_message_payload(
        self,
        *,
        lifecycle_state: str,
        table_id: str,
        subscription: WatchSubscription,
        waiting_ids: list[str],
        player_names: dict[str, str],
        snapshot: BgaTableSnapshot | None,
    ) -> tuple[str | None, discord.Embed, discord.ui.View | None]:
        if lifecycle_state == self.LIFECYCLE_RECRUITING:
            embed = await self._build_recruiting_embed(
                table_id=table_id,
                subscription=subscription,
                player_names=player_names,
                snapshot=snapshot,
            )
            return None, embed, self._build_join_view(table_id, subscription)
        if lifecycle_state == self.LIFECYCLE_FINISHED:
            embed = self._build_finished_embed(
                table_id=table_id,
                subscription=subscription,
                snapshot=snapshot,
            )
            return None, embed, None
        mention_line = await self._build_turn_mentions(
            waiting_ids=waiting_ids,
            player_names=player_names,
            subscription=subscription,
        )
        embed = await self._build_in_progress_embed(
            table_id=table_id,
            subscription=subscription,
            waiting_ids=waiting_ids,
            player_names=player_names,
        )
        return mention_line or None, embed, self._build_join_view(table_id, subscription)

    def _build_join_view(self, table_id: str, subscription: WatchSubscription) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=tr("button_join_table"),
                url=subscription.table_url or build_table_url(table_id),
            )
        )
        return view

    async def _build_recruiting_embed(
        self,
        *,
        table_id: str,
        subscription: WatchSubscription,
        player_names: dict[str, str],
        snapshot: BgaTableSnapshot | None,
    ) -> discord.Embed:
        game_name = format_game_name(
            (snapshot.game_name if snapshot is not None else None) or subscription.game_name
        )
        embed = discord.Embed(
            title=tr("lifecycle_recruiting_title"),
            description=tr(
                "lifecycle_common_description",
                game_label=tr("label_game"),
                game_name=game_name,
                table_label=tr("label_table"),
                table_id=table_id,
            ),
            color=discord.Color.gold(),
        )
        if snapshot is not None and snapshot.cover_image_url:
            embed.set_image(url=snapshot.cover_image_url)

        players_value = await self._build_player_lines(
            guild_id=subscription.guild_id,
            player_names=player_names,
            player_avatars={} if snapshot is None else snapshot.player_avatars,
        )
        embed.add_field(
            name=tr("label_players_joined"),
            value=players_value or tr("value_none"),
            inline=False,
        )
        if snapshot is not None and snapshot.seats_total is not None and snapshot.seats_remaining is not None:
            seats_value = tr(
                "invite_message_seats_value",
                seats_taken=snapshot.seats_taken,
                seats_total=snapshot.seats_total,
                seats_remaining=snapshot.seats_remaining,
            )
        else:
            joined_count = len(player_names)
            seats_value = tr("invite_message_seats_unknown", seats_taken=joined_count)
        embed.add_field(name=tr("label_seats"), value=seats_value, inline=True)
        embed.add_field(
            name=tr("label_status"),
            value=(snapshot.status if snapshot is not None else "") or tr("value_unknown"),
            inline=True,
        )
        embed.add_field(
            name=tr("label_url"),
            value=subscription.table_url or build_table_url(table_id),
            inline=False,
        )
        embed.set_footer(text=tr("invite_message_footer"))
        return embed

    async def _build_in_progress_embed(
        self,
        *,
        table_id: str,
        subscription: WatchSubscription,
        waiting_ids: list[str],
        player_names: dict[str, str],
    ) -> discord.Embed:
        game_name = format_game_name(subscription.game_name)
        embed = discord.Embed(
            title=tr("lifecycle_in_progress_title"),
            description=tr(
                "lifecycle_common_description",
                game_label=tr("label_game"),
                game_name=game_name,
                table_label=tr("label_table"),
                table_id=table_id,
            ),
            color=discord.Color.blurple(),
        )
        waiting_players = await self._build_waiting_players_text(
            waiting_ids=waiting_ids,
            player_names=player_names,
            subscription=subscription,
        )
        embed.add_field(
            name=tr("label_current_turn"),
            value=waiting_players or tr("value_none"),
            inline=False,
        )
        embed.add_field(
            name=tr("label_players_still_waiting"),
            value=waiting_players or tr("value_none"),
            inline=False,
        )
        embed.add_field(
            name=tr("label_url"),
            value=subscription.table_url or build_table_url(table_id),
            inline=False,
        )
        embed.set_footer(text=tr("lifecycle_in_progress_footer"))
        return embed

    def _build_finished_embed(
        self,
        *,
        table_id: str,
        subscription: WatchSubscription,
        snapshot: BgaTableSnapshot | None,
    ) -> discord.Embed:
        game_name = format_game_name(
            (snapshot.game_name if snapshot is not None else None) or subscription.game_name
        )
        embed = discord.Embed(
            title=tr("lifecycle_finished_title"),
            description=tr(
                "lifecycle_common_description",
                game_label=tr("label_game"),
                game_name=game_name,
                table_label=tr("label_table"),
                table_id=table_id,
            ),
            color=discord.Color.dark_green(),
        )
        if snapshot is not None and snapshot.winner_names:
            embed.add_field(
                name=tr("label_winner"),
                value=", ".join(snapshot.winner_names),
                inline=False,
            )
        if snapshot is not None and snapshot.final_standings:
            standings_value = "\n".join(snapshot.final_standings[:10])
            embed.add_field(name=tr("label_final_standings"), value=standings_value, inline=False)
        if snapshot is not None and snapshot.finish_reason:
            embed.add_field(name=tr("label_finish_reason"), value=snapshot.finish_reason, inline=False)
        if snapshot is not None and snapshot.finished_at:
            embed.add_field(name=tr("label_finished_at"), value=snapshot.finished_at, inline=False)
        embed.add_field(
            name=tr("label_url"),
            value=subscription.table_url or build_table_url(table_id),
            inline=False,
        )
        embed.set_footer(text=tr("lifecycle_finished_footer"))
        return embed

    async def _build_turn_mentions(
        self,
        *,
        waiting_ids: list[str],
        player_names: dict[str, str],
        subscription: WatchSubscription,
    ) -> str:
        linked_users = await asyncio.to_thread(
            self.database.get_linked_users_for_players,
            subscription.guild_id,
            {player_id: player_names.get(player_id, "") for player_id in waiting_ids},
        )
        mentions = [f"<@{item.discord_user_id}>" for item in linked_users]
        if not mentions:
            return ""
        return tr("turn_ping_prefix", mentions=" ".join(sorted(set(mentions))))

    async def _build_waiting_players_text(
        self,
        *,
        waiting_ids: list[str],
        player_names: dict[str, str],
        subscription: WatchSubscription,
    ) -> str:
        observed_waiting_players = {
            player_id: player_names.get(player_id, "")
            for player_id in waiting_ids
        }
        linked_users = await asyncio.to_thread(
            self.database.get_linked_users_for_players,
            subscription.guild_id,
            observed_waiting_players,
        )
        linked_users_by_bga_id = {user.bga_player_id: user for user in linked_users if user.bga_player_id}
        linked_users_by_name = {
            user.bga_player_name.casefold(): user
            for user in linked_users
            if user.bga_player_name
        }
        waiting_descriptions = ", ".join(
            self._format_waiting_player(
                player_id,
                player_names,
                linked_users_by_bga_id,
                linked_users_by_name,
            )
            for player_id in waiting_ids
        )
        return waiting_descriptions

    async def _build_player_lines(
        self,
        *,
        guild_id: str,
        player_names: dict[str, str],
        player_avatars: dict[str, str],
    ) -> str:
        linked_users = await asyncio.to_thread(
            self.database.get_linked_users_for_players,
            guild_id,
            player_names,
        )
        linked_by_bga_id = {item.bga_player_id: item for item in linked_users if item.bga_player_id}
        linked_by_name = {
            item.bga_player_name.casefold(): item
            for item in linked_users
            if item.bga_player_name
        }
        lines: list[str] = []
        for player_id, player_name in sorted(player_names.items(), key=lambda item: item[1].casefold()):
            linked = linked_by_bga_id.get(player_id)
            if linked is None and player_name:
                linked = linked_by_name.get(player_name.casefold())
            mention = f"<@{linked.discord_user_id}> " if linked is not None else ""
            avatar = player_avatars.get(player_id)
            avatar_suffix = f" • [avatar]({avatar})" if avatar else ""
            lines.append(f"{mention}{player_name} ({player_id}){avatar_suffix}")
        return "\n".join(lines)

    async def _publish_invite_snapshot(
        self,
        *,
        subscription: WatchSubscription,
        table_id: str,
        snapshot: BgaTableSnapshot,
    ) -> discord.Message | None:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None:
            return None

        embed = self._build_invite_embed(table_id=table_id, subscription=subscription, snapshot=snapshot)
        try:
            message = await channel.send(embed=embed)
            LOGGER.info(tr("invite_message_sent", table_id=table_id))
            return message
        except discord.DiscordException as exc:
            LOGGER.error(
                tr(
                    "invite_message_send_failed",
                    table_id=table_id,
                    channel_id=subscription.channel_id,
                    error=exc,
                )
            )
            return None

    async def _edit_invite_message(
        self,
        *,
        subscription: WatchSubscription,
        active_message: ActiveTableMessage,
        table_id: str,
        snapshot: BgaTableSnapshot,
    ) -> bool:
        channel = await self._resolve_channel(subscription, table_id)
        if channel is None or not isinstance(channel, discord.TextChannel):
            return False

        embed = self._build_invite_embed(table_id=table_id, subscription=subscription, snapshot=snapshot)
        try:
            await active_message.message.edit(content=None, embed=embed)
            LOGGER.info(tr("invite_message_updated", table_id=table_id))
            return True
        except discord.NotFound:
            LOGGER.info(tr("invite_message_missing_update", table_id=table_id))
            return False
        except discord.DiscordException as exc:
            LOGGER.error(tr("invite_message_update_failed", table_id=table_id, error=exc))
            return False

    async def _cleanup_stale_table_messages(
        self,
        subscriptions: list[WatchSubscription],
        table_id: str,
    ) -> None:
        if self.bot.user is None:
            return

        seen_channels: set[str] = set()
        deleted_count = 0
        table_markers = {f"{tr('label_table')} : {table_id}", f"{tr('label_table')}: {table_id}", f"Table : {table_id}", f"Table: {table_id}"}

        for subscription in subscriptions:
            effective_channel_id = self._forced_channel_id or subscription.channel_id
            if effective_channel_id in seen_channels:
                continue
            seen_channels.add(effective_channel_id)

            channel = await self._resolve_channel(subscription, table_id)
            if channel is None or not hasattr(channel, "history"):
                continue

            try:
                async for message in channel.history(limit=100):
                    if message.author.id != self.bot.user.id:
                        continue
                    if not self._message_contains_table_marker(message, table_markers):
                        continue
                    try:
                        await message.delete()
                        deleted_count += 1
                    except discord.NotFound:
                        continue
                    except discord.DiscordException as exc:
                        LOGGER.warning(
                            tr(
                                "stale_message_delete_failed",
                                table_id=table_id,
                                channel_id=subscription.channel_id,
                                error=exc,
                            )
                        )
            except discord.DiscordException as exc:
                LOGGER.warning(
                    tr(
                        "channel_history_cleanup_failed",
                        channel_id=subscription.channel_id,
                        table_id=table_id,
                        error=exc,
                    )
                )

        if deleted_count:
            LOGGER.info(tr("startup_cleanup", deleted_count=deleted_count, table_id=table_id))

    @staticmethod
    def _message_contains_table_marker(message: discord.Message, table_markers: set[str]) -> bool:
        content_parts = [message.content]
        for embed in message.embeds:
            content_parts.extend(
                filter(
                    None,
                    [
                        embed.title,
                        embed.description,
                        *(field.name for field in embed.fields),
                        *(field.value for field in embed.fields),
                    ],
                )
            )
        return any(marker in "\n".join(content_parts) for marker in table_markers)

    async def _build_turn_message_content(
        self,
        *,
        waiting_ids: list[str],
        player_names: dict[str, str],
        table_id: str,
        subscription: WatchSubscription,
        game_label: str,
    ) -> str:
        observed_waiting_players = {
            player_id: player_names.get(player_id, "")
            for player_id in waiting_ids
        }
        linked_users = await asyncio.to_thread(
            self.database.get_linked_users_for_players,
            subscription.guild_id,
            observed_waiting_players,
        )
        linked_users_by_bga_id = {user.bga_player_id: user for user in linked_users if user.bga_player_id}
        linked_users_by_name = {
            user.bga_player_name.casefold(): user
            for user in linked_users
            if user.bga_player_name
        }
        waiting_descriptions = ", ".join(
            self._format_waiting_player(
                player_id,
                player_names,
                linked_users_by_bga_id,
                linked_users_by_name,
            )
            for player_id in waiting_ids
        )

        return tr(
            "turn_message_content",
            game_label=tr("label_game"),
            game_name=game_label,
            table_label=tr("label_table"),
            table_id=table_id,
            players_label=tr("label_players_still_waiting"),
            players=waiting_descriptions or tr("value_none"),
            url_label=tr("label_url"),
            table_url=subscription.table_url or build_table_url(table_id),
        )

    def _build_invite_embed(
        self,
        *,
        table_id: str,
        subscription: WatchSubscription,
        snapshot: BgaTableSnapshot,
    ) -> discord.Embed:
        players = sorted(snapshot.player_names.values(), key=str.casefold)
        players_text = ", ".join(players) if players else tr("value_none")
        if snapshot.seats_total is not None and snapshot.seats_remaining is not None:
            seats_text = tr(
                "invite_message_seats_value",
                seats_taken=snapshot.seats_taken,
                seats_total=snapshot.seats_total,
                seats_remaining=snapshot.seats_remaining,
            )
        else:
            seats_text = tr("invite_message_seats_unknown", seats_taken=snapshot.seats_taken)

        embed = discord.Embed(
            title=tr("invite_message_title"),
            description=tr(
                "invite_message_description",
                game_label=tr("label_game"),
                game_name=format_game_name(snapshot.game_name or subscription.game_name),
                table_label=tr("label_table"),
                table_id=table_id,
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name=tr("label_status"), value=snapshot.status or tr("value_unknown"), inline=True)
        embed.add_field(name=tr("label_seats"), value=seats_text, inline=True)
        embed.add_field(name=tr("label_players_joined"), value=players_text, inline=False)
        embed.add_field(
            name=tr("label_url"),
            value=subscription.table_url or build_table_url(table_id),
            inline=False,
        )
        embed.set_footer(text=tr("invite_message_footer"))
        return embed

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
        channel_id = self._forced_channel_id or subscription.channel_id
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
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

    async def _clear_active_invite_messages(
        self,
        subscriptions: list[WatchSubscription],
        table_id: str,
    ) -> None:
        for subscription in subscriptions:
            active_message = self._active_messages.get(subscription.subscription_id)
            if active_message is None or active_message.kind != "invite":
                continue
            deleted = await self._delete_tracked_message(
                subscription=subscription,
                active_message=active_message,
                table_id=table_id,
            )
            if deleted:
                self._active_messages.pop(subscription.subscription_id, None)

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
            self.database.upsert_watch_subscription(
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
            return self._subscriptions_for_table(snapshot.table_id)
        return subscriptions

    def _subscriptions_for_table(self, table_id: str) -> list[WatchSubscription]:
        return [item for item in self.database.list_watch_subscriptions() if item.table_id == table_id]

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
    def _format_player_reference(player_id: str, player_names: dict[str, str]) -> str:
        player_name = player_names.get(player_id)
        if player_name and player_name != player_id:
            return f"{player_name} ({player_id})"
        return player_id

    @classmethod
    def _format_waiting_player(
        cls,
        player_id: str,
        player_names: dict[str, str],
        linked_users_by_bga_id: dict[str, LinkedUser],
        linked_users_by_name: dict[str, LinkedUser],
    ) -> str:
        linked_user = linked_users_by_bga_id.get(player_id)
        if linked_user is None:
            player_name = player_names.get(player_id, "").strip()
            if player_name:
                linked_user = linked_users_by_name.get(player_name.casefold())
        if linked_user is None:
            return cls._format_player_reference(player_id, player_names)
        player_label = linked_user.bga_player_name or player_names.get(player_id, "").strip() or player_id
        player_id_label = linked_user.bga_player_id or player_id
        return f"<@{linked_user.discord_user_id}> {player_label} ({player_id_label})"

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
