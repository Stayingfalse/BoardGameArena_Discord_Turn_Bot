"""Watch Codenames rooms and keep their Discord cards and turn pings up to date.

One :class:`~.client.CodenamesRoomClient` per watched room, however many
channels watch it. Every state change from the room is debounced, then each
watching channel's card for the current match ("Game N") is sent or edited,
and the players whose turn it is get a short ping message (the previous ping
is deleted so the channel doesn't fill up).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import discord

from ..database import AsyncDatabase, Database
from ..discord_io import DiscordRateLimiter
from ..i18n import tr
from ..utils import utc_now_iso
from . import cards
from .client import CLOSE_FAILED, CLOSE_STOPPED, CLOSE_UNSUPPORTED, CodenamesRoomClient
from .identity import DEFAULT_BOT_NAMES, ensure_room_identity
from .models import CodenamesGame, CodenamesWatch
from .posting import VIA_BOT, VIA_WEBHOOK, BotPoster, Posters
from .state import GameView

LOGGER = logging.getLogger(__name__)

CLOSE_IDLE = "idle"
_CLOSE_NOTES = {
    CLOSE_UNSUPPORTED: "cn_note_unsupported",
    CLOSE_FAILED: "cn_note_unreachable",
    CLOSE_IDLE: "cn_note_idle",
}
_PING_KEY = "ping_message_id"
_PING_VIA_KEY = "ping_via"
_CARD_VIA_KEY = "card_via"

ClientFactory = Callable[..., CodenamesRoomClient]


class CodenamesMonitor:
    def __init__(
        self,
        bot: discord.Client,
        database: Database,
        *,
        bot_names: tuple[str, ...] = DEFAULT_BOT_NAMES,
        idle_hours: float = 168.0,
        update_delay: float = 1.5,
        client_factory: ClientFactory = CodenamesRoomClient,
        rate_limiter: DiscordRateLimiter | None = None,
        posters: Posters | None = None,
    ) -> None:
        self.bot = bot
        self.db = AsyncDatabase(database)
        self.bot_names = bot_names
        self.idle_seconds = idle_hours * 3600
        self.update_delay = update_delay
        self.discord = rate_limiter or DiscordRateLimiter()
        self._client_factory = client_factory
        self.posters = posters or Posters(bot)
        self._clients: dict[str, CodenamesRoomClient] = {}
        self._views: dict[str, GameView] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_active: dict[str, float] = {}
        self._last_state: dict[str, tuple[str, int | None]] = {}
        self._messages: dict[int, discord.Message] = {}
        self._signatures: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Reconnect to every watched room (after a restart)."""
        for room_id in sorted({watch.room_id for watch in await self.db.list_codenames_watches()}):
            await self._ensure_client(room_id)

    async def stop(self) -> None:
        for task in self._pending.values():
            task.cancel()
        clients = list(self._clients.values())
        self._clients.clear()
        await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)

    def is_watching(self, room_id: str) -> bool:
        return room_id in self._clients

    async def watch_room(
        self, room_id: str, *, guild_id: str, channel_id: str, created_by_discord_user_id: str
    ) -> tuple[CodenamesWatch, bool]:
        await ensure_room_identity(self.db, room_id, self.bot_names)
        watch, created = await self.db.add_codenames_watch(
            room_id, guild_id=guild_id, channel_id=channel_id, created_by_discord_user_id=created_by_discord_user_id
        )
        await self._ensure_client(room_id)
        if room_id in self._views:
            self._schedule(room_id, delay=0)
        return watch, created

    async def unwatch(self, watch: CodenamesWatch, *, outcome: str = "unwatched") -> None:
        game = await self.db.get_latest_codenames_game(watch.watch_id)
        if game is not None:
            await self._delete_ping(watch, game)
        await self.db.remove_codenames_watch(watch.watch_id, outcome=outcome)
        await self._stop_if_unwatched(watch.room_id)

    async def remove_guild(self, guild_id: str) -> int:
        watches = await self.db.list_codenames_watches_for_guild(guild_id)
        removed = await self.db.remove_all_codenames_watches_for_guild(guild_id, outcome="cancelled")
        for room_id in {watch.room_id for watch in watches}:
            await self._stop_if_unwatched(room_id)
        return removed

    async def refresh_room(self, room_id: str) -> None:
        """Redraw the room's cards now (after someone claims a seat, for example)."""
        if room_id in self._views:
            self._schedule(room_id, delay=0)

    def latest_view(self, room_id: str) -> GameView | None:
        return self._views.get(room_id)

    async def _ensure_client(self, room_id: str) -> None:
        if room_id in self._clients:
            return
        room = await ensure_room_identity(self.db, room_id, self.bot_names)

        async def on_view(view: GameView) -> None:
            self._on_view(room_id, view)

        async def on_closed(reason: str) -> None:
            await self._on_closed(room_id, reason)

        client = self._client_factory(
            room_id,
            credentials=room.bot_credentials,
            bot_name=room.bot_name,
            on_view=on_view,
            on_closed=on_closed,
        )
        self._clients[room_id] = client
        self._last_active[room_id] = time.monotonic()
        client.start()

    async def _stop_if_unwatched(self, room_id: str) -> None:
        if await self.db.list_codenames_watches(room_id):
            return
        client = self._clients.pop(room_id, None)
        self._views.pop(room_id, None)
        task = self._pending.pop(room_id, None)
        if task is not None:
            task.cancel()
        if client is not None:
            await client.stop()

    # ------------------------------------------------------------------
    # Room events
    # ------------------------------------------------------------------

    def _on_view(self, room_id: str, view: GameView) -> None:
        self._views[room_id] = view
        # A move (any state change) or someone in the room counts as activity. Turn-based
        # games can go days between moves, so being connected alone isn't required.
        state = (view.match_id, view.state_id)
        if view.connected_ids or self._last_state.get(room_id) != state:
            self._last_active[room_id] = time.monotonic()
        self._last_state[room_id] = state
        self._schedule(room_id, delay=self.update_delay)

    def _schedule(self, room_id: str, *, delay: float) -> None:
        """Coalesce bursts of room events into one update."""
        task = self._pending.get(room_id)
        if task is not None and not task.done():
            if delay > 0:
                return
            task.cancel()
        self._pending[room_id] = asyncio.create_task(self._delayed_update(room_id, delay))

    async def _delayed_update(self, room_id: str, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await self.update_room(room_id)
        except Exception:
            LOGGER.exception("Codenames room %s: card update failed", room_id)

    async def update_room(self, room_id: str) -> None:
        view = self._views.get(room_id)
        if view is None:
            return
        async with self._locks.setdefault(room_id, asyncio.Lock()):
            if time.monotonic() - self._last_active.get(room_id, time.monotonic()) > self.idle_seconds:
                client = self._clients.get(room_id)
                if client is not None:
                    await client.stop()  # on_closed is told "stopped"; close as idle here
                await self._close_room(room_id, CLOSE_IDLE)
                return
            game_number, _ = await self.db.record_codenames_match(room_id, view.match_id, view.bot_player_id)
            names = {pid: name for pid, name in view.player_names.items() if pid != view.bot_player_id}
            await self.db.update_codenames_player_names(room_id, names)
            players = await self.db.list_codenames_players(room_id)
            for watch in await self.db.list_codenames_watches(room_id):
                await self._auto_claim_invited(watch, players)
            players = await self.db.list_codenames_players(room_id)
            links = {p.player_id: p.discord_user_id for p in players if p.discord_user_id}
            for watch in await self.db.list_codenames_watches(room_id):
                try:
                    await self._update_watch(watch, view, game_number, links)
                except Exception:
                    LOGGER.exception("Codenames room %s: update failed for watch %s", room_id, watch.watch_id)

    async def _auto_claim_invited(self, watch: CodenamesWatch, players) -> None:
        """Invited members whose remembered name matches an unclaimed seat get it."""
        linked = {p.discord_user_id for p in players if p.discord_user_id}
        for user_id in await self.db.list_codenames_watch_members(watch.watch_id):
            if user_id in linked:
                continue
            remembered = await self.db.get_codenames_remembered_name(user_id)
            if not remembered:
                continue
            matches = [
                p for p in players
                if not p.discord_user_id and (p.display_name or "").casefold() == remembered.casefold()
            ]
            if len(matches) == 1:
                await self.db.claim_codenames_player(watch.room_id, matches[0].player_id, user_id)
                matches[0].discord_user_id = user_id
                linked.add(user_id)

    async def _update_watch(
        self, watch: CodenamesWatch, view: GameView, game_number: int, links: dict[str, str]
    ) -> None:
        previous = await self.db.get_latest_codenames_game(watch.watch_id)
        game, created = await self.db.get_or_create_codenames_game(watch.watch_id, view.match_id, game_number)
        if created and previous is not None and previous.game_id != game.game_id:
            await self._retire_game(watch, previous)

        channel = await self._channel(watch)
        if channel is None:
            return

        invited = tuple(
            user_id
            for user_id in await self.db.list_codenames_watch_members(watch.watch_id)
            if user_id not in links.values()
        )
        data = cards.CardData(
            room_id=watch.room_id, game_number=game.game_number, view=view, links=links, invited=invited
        )

        if game.lifecycle_state == "lobby" and view.phase in ("in_progress", "finished"):
            await self.db.update_codenames_game(
                game.game_id, lifecycle_state="in_progress", started_at=utc_now_iso()
            )
            game.lifecycle_state = "in_progress"

        message = await self._send_or_edit_card(channel, game, data)
        if message is None:
            return

        if view.phase == "finished":
            await self._delete_ping(watch, game)
            if game.lifecycle_state != "finished":
                players_on_teams = {pid for roles in view.rosters.values() for ids in roles.values() for pid in ids}
                await self.db.finish_codenames_game(
                    game.game_id,
                    winner_team=view.winner_team,
                    finish_reason=view.finish_reason,
                    player_count=len(players_on_teams) or None,
                )
            return

        if view.turn_key != game.turn_key:
            await self._delete_ping(watch, game)
            poster = await self.posters.for_sending(channel)
            ping = (
                cards.build_turn_ping(view, links, game.game_number, branded=poster.via == VIA_BOT)
                if view.turn_key
                else None
            )
            ping_id = None
            if ping is not None:
                text, user_ids = ping
                sent, poster = await self._send(
                    "send_codenames_ping",
                    watch.room_id,
                    channel,
                    poster,
                    text,
                    view=cards.build_ping_view(watch.room_id),
                    allowed_mentions=discord.AllowedMentions(
                        users=[discord.Object(id=uid) for uid in user_ids], everyone=False, roles=False
                    ),
                    reply_to=message,
                )
                ping_id = sent.id if sent is not None else None
            state = dict(game.state)
            state[_PING_KEY] = ping_id
            state[_PING_VIA_KEY] = poster.via
            await self.db.update_codenames_game(game.game_id, turn_key=view.turn_key, state=state)
            game.turn_key, game.state = view.turn_key, state

    async def _send_or_edit_card(
        self, channel: discord.abc.Messageable, game: CodenamesGame, data: cards.CardData
    ) -> discord.Message | None:
        signature = cards.card_signature(data)
        message = await self._card_message(channel, game)
        if message is not None and self._signatures.get(game.game_id) == signature:
            return message
        layout = cards.build_card(data)
        if message is not None:
            try:
                edited = await self._call(
                    "edit_codenames_card",
                    data.room_id,
                    lambda: message.edit(view=layout, allowed_mentions=discord.AllowedMentions.none()),
                    channel,
                    kind="edit",
                    created_at=message.created_at,
                )
            except discord.NotFound:
                message = None
            else:
                if edited is None:
                    return message
        if message is None:
            message, poster = await self._send(
                "send_codenames_card",
                data.room_id,
                channel,
                await self.posters.for_sending(channel),
                view=layout,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if message is None:
                return None
            state = dict(game.state)
            state[_CARD_VIA_KEY] = poster.via
            await self.db.update_codenames_game(game.game_id, message_id=message.id, state=state)
            game.message_id, game.state = message.id, state
        self._messages[game.game_id] = message
        self._signatures[game.game_id] = signature
        return message

    async def _card_message(self, channel: discord.abc.Messageable, game: CodenamesGame) -> discord.Message | None:
        cached = self._messages.get(game.game_id)
        if cached is not None:
            return cached
        if game.message_id is None:
            return None
        poster = await self.posters.for_message(channel, game.state.get(_CARD_VIA_KEY, VIA_BOT))
        if poster is None:
            return None
        try:
            message = await self._call(
                "fetch_codenames_card", game.match_id, lambda: poster.fetch(game.message_id), channel, kind="read"
            )
        except discord.NotFound:
            return None
        if message is not None:
            self._messages[game.game_id] = message
        return message

    async def _retire_game(self, watch: CodenamesWatch, game: CodenamesGame) -> None:
        """The room moved on to a new match before this one finished."""
        await self._delete_ping(watch, game)
        if game.lifecycle_state == "finished":
            return
        await self._close_card(watch, game, tr("cn_note_superseded"))

    async def _delete_ping(self, watch: CodenamesWatch, game: CodenamesGame) -> None:
        ping_id = game.state.get(_PING_KEY)
        if not ping_id:
            return
        channel = await self._channel(watch)
        if channel is not None:
            # A webhook ping whose webhook is gone can still be deleted by the bot (Manage Messages).
            poster = await self.posters.for_message(channel, game.state.get(_PING_VIA_KEY, VIA_BOT)) or BotPoster(channel)
            try:
                await self._call(
                    "delete_codenames_ping", watch.room_id, lambda: poster.delete(int(ping_id)), channel, kind="delete"
                )
            except discord.NotFound:
                pass
            except discord.DiscordException as exc:
                LOGGER.warning("Codenames room %s: could not delete ping: %s", watch.room_id, exc)
        state = dict(game.state)
        state[_PING_KEY] = None
        await self.db.update_codenames_game(game.game_id, state=state)
        game.state = state

    async def _close_card(self, watch: CodenamesWatch, game: CodenamesGame, note: str) -> None:
        channel = await self._channel(watch)
        if channel is None:
            return
        players = await self.db.list_codenames_players(watch.room_id)
        links = {p.player_id: p.discord_user_id for p in players if p.discord_user_id}
        view = self._views.get(watch.room_id)
        data = cards.CardData(
            room_id=watch.room_id,
            game_number=game.game_number,
            # The last known state of this game, if the room was still on it.
            view=view if view is not None and view.match_id == game.match_id else None,
            links=links,
            closed_note=note,
        )
        await self._send_or_edit_card(channel, game, data)

    async def _on_closed(self, room_id: str, reason: str) -> None:
        if reason == CLOSE_STOPPED:
            return
        async with self._locks.setdefault(room_id, asyncio.Lock()):
            await self._close_room(room_id, reason)

    async def _close_room(self, room_id: str, reason: str) -> None:
        """The bot can't follow the room any more: say so on each card and stop watching."""
        LOGGER.info("Codenames room %s: no longer watched (%s)", room_id, reason)
        note = tr(_CLOSE_NOTES.get(reason, "cn_note_unreachable"))
        for watch in await self.db.list_codenames_watches(room_id):
            game = await self.db.get_latest_codenames_game(watch.watch_id)
            if game is not None:
                await self._delete_ping(watch, game)
                if game.lifecycle_state != "finished":
                    await self._close_card(watch, game, note)
            else:
                channel = await self._channel(watch)
                if channel is not None:
                    await self._send(
                        "send_codenames_note",
                        room_id,
                        channel,
                        await self.posters.for_sending(channel),
                        tr("cn_note_room", room=room_id, note=note),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            await self.db.remove_codenames_watch(watch.watch_id, outcome="cancelled")
        self._clients.pop(room_id, None)
        self._views.pop(room_id, None)

    # ------------------------------------------------------------------
    # Discord helpers
    # ------------------------------------------------------------------

    async def _channel(self, watch: CodenamesWatch):
        channel = self.bot.get_channel(int(watch.channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(watch.channel_id))
            except discord.DiscordException as exc:
                LOGGER.warning("Codenames room %s: channel %s unavailable: %s", watch.room_id, watch.channel_id, exc)
                return None
        return channel if callable(getattr(channel, "send", None)) else None

    async def _send(self, operation: str, room_id: str, channel, poster, content=None, **kwargs):
        """Send with *poster*; if its webhook was deleted, set it up again or fall back to the bot.
        Returns ``(message or None, the poster that sent it)``."""
        try:
            return await self._call(operation, room_id, lambda: poster.send(content, **kwargs), channel), poster
        except discord.NotFound:
            if poster.via != VIA_WEBHOOK:
                raise
        self.posters.forget(channel)
        poster = await self.posters.for_sending(channel)
        return await self._call(operation, room_id, lambda: poster.send(content, **kwargs), channel), poster

    async def _call(self, operation: str, room_id: str, factory, channel, *, kind: str = "send", created_at=None):
        if not self.discord.io_allowed():
            return None
        return await self.discord.call(
            operation,
            room_id,
            factory,
            channel_id=str(getattr(channel, "id", "")) or None,
            operation_kind=kind,
            message_created_at=created_at,
        )
