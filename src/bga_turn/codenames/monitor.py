"""Watch Codenames rooms and keep their Discord cards and turn pings up to date.

One :class:`~.client.CodenamesRoomClient` per watched room, however many
channels watch it. Every state change from the room is debounced, then each
watching channel's card for the current match ("Game N") is sent or edited,
and the players whose turn it is get a short ping message (the previous ping
is deleted so the channel doesn't fill up). In an ordinary channel the pings go
to a thread for the room when the bot may create one (see :mod:`.ping_threads`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable

import discord

from ..database import AsyncDatabase, Database
from ..discord_io import DiscordRateLimiter
from ..i18n import tr
from ..utils import utc_now_iso
from . import cards
from .board_image import FILENAME as BOARD_FILENAME
from .board_image import BoardSnapshot, render_board_png
from .client import CLOSE_FAILED, CLOSE_STOPPED, CLOSE_UNSUPPORTED, CodenamesRoomClient
from .identity import DEFAULT_BOT_NAMES, ensure_room_identity
from .models import CodenamesGame, CodenamesWatch
from .ping_threads import PRIVATE, ping_thread_kind, ping_thread_name
from .posting import VIA_BOT, VIA_WEBHOOK, BotPoster, Posters, make_file
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
# The copy of the card kept in the ping thread, so the players can follow the game there.
THREAD_CARD_KEY = "thread_card_message_id"
_THREAD_CARD_VIA_KEY = "thread_card_via"
_BOARD_KEY = "board"
_LINEUP_PING_KEY = "lineup_ping_message_id"
_LINEUP_PING_VIA_KEY = "lineup_ping_via"
_LINEUP_SENT_KEY = "lineup_pinged"
# Channel (or thread) each ping was sent to: "<ping key>_channel".
_CHANNEL_SUFFIX = "_channel"
# After failing to open a ping thread in a channel, wait this long before trying again.
_PING_THREAD_RETRY_SECONDS = 600
_TURN_STARTED_KEY = "turn_started_at"  # unix seconds
_REMINDED_KEY = "reminded_at"  # unix seconds
# The tournament's number for this game ("Game 2" of the match), shown instead of the room's own count.
_MATCH_GAME_KEY = "match_game"

ClientFactory = Callable[..., CodenamesRoomClient]


def normalise_name(name: str | None) -> str:
    """Lower-case letters and digits only: "🤖 Alice B." and "alice_b" both become "aliceb"."""
    return "".join(ch for ch in (name or "").casefold() if ch.isalnum())


def seat_links(players, view: GameView | None, devices: dict[str, str] | None = None) -> dict[str, str]:
    """Seat -> Discord user for claimed seats, then seats a player said are another device
    of theirs (*devices*), then unclaimed seats that look like one (see :func:`device_aliases`)."""
    links = {p.player_id: p.discord_user_id for p in players if p.discord_user_id}
    holders = set(links.values())
    links = {**{pid: u for pid, u in (devices or {}).items() if u in holders}, **links}
    return {**links, **device_aliases(view, links)} if view is not None else links


def _same_person_name(a: str, b: str) -> bool:
    """Whether one normalised name starts or ends the other ("alliee" / "allieepc", "notetreides" / "etreides")."""
    short, long = sorted((a, b), key=len)
    return long.startswith(short) or long.endswith(short)


def _first_word(name: str | None) -> str:
    """The first run of letters and digits, lower-cased: "Potato PC" -> "potato"."""
    word = ""
    for ch in (name or "").casefold():
        if ch.isalnum():
            word += ch
        elif word:
            break
    return word


def device_aliases(view: GameView, links: dict[str, str]) -> dict[str, str]:
    """Unclaimed seats taken to be a claimed player on a second device: the name is the
    player's name, or starts or ends it ("Alliee - PC" next to "Alliee", "NOT Etreides"
    next to "Etreides"), or the seat is in the player's team and role and the names
    share their first word ("Potato PC" next to "Potato Phone"). A seat is only matched
    when it fits exactly one Discord user; names and words under 3 letters never match."""
    role_of_seat = {
        pid: (team, role) for team, roles in view.rosters.items() for role, ids in roles.items() for pid in ids
    }
    claimed = [
        (normalise_name(view.player_name(pid)), _first_word(view.player_name(pid)), role_of_seat.get(pid), user_id)
        for pid, user_id in links.items()
    ]
    seats = set(role_of_seat) | set(view.spectator_ids)
    aliases: dict[str, str] = {}
    for pid in seats - set(links) - {view.bot_player_id}:
        name, word, seat = normalise_name(view.player_name(pid)), _first_word(view.player_name(pid)), role_of_seat.get(pid)
        users = {
            user_id
            for other, other_word, other_seat, user_id in claimed
            if (len(name) >= 3 and len(other) >= 3 and _same_person_name(name, other))
            or (seat is not None and seat == other_seat and len(word) >= 3 and word == other_word)
        }
        if len(users) == 1:
            aliases[pid] = users.pop()
    return aliases


def shown_game_number(game: CodenamesGame) -> int:
    """The number cards and pings call this game: the tournament's, when it has given one."""
    number = game.state.get(_MATCH_GAME_KEY)
    return number if isinstance(number, int) else game.game_number


# Told once per finished game: the watch, the game, the final view and seat -> Discord user links.
GameFinishedListener = Callable[[CodenamesWatch, CodenamesGame, GameView, dict[str, str]], Awaitable[None]]


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
        reminder_hours: float = 24.0,
        deadline_window_hours: float = 48.0,
        deadline_reminder_hours: float = 6.0,
        reminder_check_seconds: float = 300.0,
        ping_threads: bool = True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bot = bot
        self.ping_threads = ping_threads
        self._ping_thread_failed_at: dict[int, float] = {}  # watch_id -> monotonic time
        self._ping_thread_members: dict[int, set[int]] = {}  # private thread id -> user ids added
        self.reminder_seconds = reminder_hours * 3600
        self.deadline_window_seconds = deadline_window_hours * 3600
        self.deadline_reminder_seconds = deadline_reminder_hours * 3600
        self.reminder_check_seconds = reminder_check_seconds
        self._clock = clock
        self._reminder_task: asyncio.Task | None = None
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
        # Keyed by (game_id, is the copy in the ping thread).
        self._messages: dict[tuple[int, bool], discord.Message] = {}
        self._signatures: dict[tuple[int, bool], str] = {}
        self.game_finished_listeners: list[GameFinishedListener] = []
        self._member_name_cache: dict[tuple[str, str], tuple[list[str], float]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Reconnect to every watched room (after a restart)."""
        for room_id in sorted({watch.room_id for watch in await self.db.list_codenames_watches()}):
            await self._ensure_client(room_id)
        if self.reminder_seconds > 0 and (self._reminder_task is None or self._reminder_task.done()):
            self._reminder_task = asyncio.create_task(self._reminder_loop(), name="codenames-reminders")

    async def stop(self) -> None:
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            await asyncio.gather(self._reminder_task, return_exceptions=True)
            self._reminder_task = None
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
            idle = time.monotonic() - self._last_active.get(room_id, time.monotonic()) > self.idle_seconds
            if idle and not await self._room_in_tournament(room_id):
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
            links = await self.seat_links(room_id, view)
            for watch in await self.db.list_codenames_watches(room_id):
                try:
                    lineup = await self._current_lineup(watch)
                    await self._update_watch(watch, view, game_number, links, lineup=lineup)
                except Exception:
                    LOGGER.exception("Codenames room %s: update failed for watch %s", room_id, watch.watch_id)

    async def seat_links(self, room_id: str, view: GameView | None) -> dict[str, str]:
        """Seat -> Discord user for everyone in *room_id* (see :func:`seat_links`)."""
        players = await self.db.list_codenames_players(room_id)
        return seat_links(players, view, await self.db.list_codenames_devices(room_id))

    async def _auto_claim_invited(self, watch: CodenamesWatch, players) -> None:
        """Give invited members the seat that is clearly theirs.

        First by the name they used last time (exact), then by their Discord or
        tournament names, ignoring case, spaces, punctuation and emoji. A seat is
        only claimed when exactly one member matches it and it matches only them.
        """
        linked = {p.discord_user_id for p in players if p.discord_user_id}
        waiting = [u for u in await self.db.list_codenames_watch_members(watch.watch_id) if u not in linked]
        free = [p for p in players if not p.discord_user_id and p.display_name]
        if not waiting or not free:
            return

        claims: dict[str, str] = {}  # seat -> Discord user
        for user_id in waiting:
            remembered = await self.db.get_codenames_remembered_name(user_id)
            if not remembered:
                continue
            matches = [p for p in free if (p.display_name or "").casefold() == remembered.casefold()]
            if len(matches) == 1 and matches[0].player_id not in claims:
                claims[matches[0].player_id] = user_id

        binding = await self.db.get_codenames_binding(watch.channel_id)
        candidates: dict[str, set[str]] = {}  # seat -> users whose names match it
        for user_id in waiting:
            if user_id in claims.values():
                continue
            names = {normalise_name(n) for n in await self._member_names(watch.guild_id, user_id)}
            if binding is not None and binding.player_names.get(user_id):
                names.add(normalise_name(binding.player_names[user_id]))
            names.discard("")
            seats = [p.player_id for p in free if p.player_id not in claims and normalise_name(p.display_name) in names]
            if len(seats) == 1:
                candidates.setdefault(seats[0], set()).add(user_id)
        for seat, users in candidates.items():
            if len(users) == 1:
                claims[seat] = next(iter(users))

        for seat, user_id in claims.items():
            await self.db.claim_codenames_player(watch.room_id, seat, user_id)

    async def _member_names(self, guild_id: str, user_id: str) -> list[str]:
        """A member's server nickname, display name and username (cached for an hour)."""
        cached = self._member_name_cache.get((guild_id, user_id))
        if cached is not None and time.monotonic() - cached[1] < 3600:
            return cached[0]
        names: list[str] = []
        get_guild = getattr(self.bot, "get_guild", None)
        guild = get_guild(int(guild_id)) if get_guild is not None and guild_id.isdigit() else None
        if guild is not None and user_id.isdigit():
            member = guild.get_member(int(user_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(user_id))
                except discord.DiscordException:
                    member = None
            if member is not None:
                for attr in ("nick", "display_name", "global_name", "name"):
                    value = getattr(member, attr, None)
                    if value and value not in names:
                        names.append(value)
        self._member_name_cache[(guild_id, user_id)] = (names, time.monotonic())
        return names

    async def _room_in_tournament(self, room_id: str) -> bool:
        """Tournament matches can run for weeks; the tournament ends their watch itself."""
        for watch in await self.db.list_codenames_watches(room_id):
            if await self.db.get_codenames_binding(watch.channel_id) is not None:
                return True
        return False

    async def _current_lineup(self, watch: CodenamesWatch) -> cards.Lineup | None:
        """The seats a tournament expects in this channel, unless the line-up is older than
        the last result reported from here (then it is for a game already played)."""
        binding = await self.db.get_codenames_binding(watch.channel_id)
        if binding is None or not await self.db.codenames_lineup_is_current(binding):
            return None
        return cards.Lineup.from_binding(binding.lineup)

    async def _update_watch(
        self,
        watch: CodenamesWatch,
        view: GameView,
        game_number: int,
        links: dict[str, str],
        *,
        lineup: cards.Lineup | None = None,
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
        thread = None
        if self.ping_threads:
            players = (*links.values(), *invited)
            # Open the thread as soon as there is someone to put in it, and add them all now.
            if watch.ping_thread_id or (players and view.phase != "finished"):
                thread = await self._ping_target(watch, channel, players, refresh=False)
                thread = thread if thread is not channel else None
        if lineup is not None and view.phase != "finished" and game.state.get(_MATCH_GAME_KEY) != lineup.game:
            # Fixed once the game ends, so the card doesn't move on with the tournament afterwards.
            state = {**game.state, _MATCH_GAME_KEY: lineup.game}
            await self.db.update_codenames_game(game.game_id, state=state)
            game.state = state
        data = cards.CardData(
            room_id=watch.room_id,
            game_number=shown_game_number(game),
            view=view,
            links=links,
            invited=invited,
            lineup=lineup if view.phase != "finished" else None,
            pings_channel_id=watch.ping_thread_id,
        )

        if game.lifecycle_state == "lobby" and view.phase in ("in_progress", "finished"):
            await self.db.update_codenames_game(
                game.game_id, lifecycle_state="in_progress", started_at=utc_now_iso()
            )
            game.lifecycle_state = "in_progress"

        if view.phase == "finished" and view.board and _BOARD_KEY not in game.state:
            # Keep the final board with the game so its image survives restarts and "play again".
            state = dict(game.state)
            state[_BOARD_KEY] = BoardSnapshot.from_view(view, shown_game_number(game)).to_dict()
            await self.db.update_codenames_game(game.game_id, state=state)
            game.state = state

        message = await self._send_or_edit_card(channel, game, data)
        if message is None:
            return
        if thread is not None:
            await self._update_thread_card(watch, thread, game, dataclasses.replace(data, pings_channel_id=None))

        await self._update_lineup_ping(watch, game, view, lineup, links, channel, message)

        if view.phase == "finished":
            await self._delete_ping(watch, game)
            if game.lifecycle_state != "finished":
                players_on_teams = {pid for roles in view.rosters.values() for ids in roles.values() for pid in ids}
                finished = await self.db.finish_codenames_game(
                    game.game_id,
                    winner_team=view.winner_team,
                    finish_reason=view.finish_reason,
                    player_count=len(players_on_teams) or None,
                )
                if finished:
                    await self._notify_game_finished(watch, game, view, links)
            return

        if view.turn_key != game.turn_key:
            state = await self._send_turn_ping(watch, game, view, links, channel, message)
            state[_TURN_STARTED_KEY] = int(self._clock())
            state.pop(_REMINDED_KEY, None)
            await self.db.update_codenames_game(game.game_id, turn_key=view.turn_key, state=state)
            game.turn_key, game.state = view.turn_key, state

    async def _send_turn_ping(
        self,
        watch: CodenamesWatch,
        game: CodenamesGame,
        view: GameView,
        links: dict[str, str],
        channel,
        card: discord.Message | None,
        *,
        reminder: str | None = None,
    ) -> dict:
        """Replace the game's turn ping with a new one (deleting the old one so the
        mention notifies again). Returns the game's new state, not yet saved."""
        await self._delete_ping(watch, game)
        ping = cards.build_turn_ping(view, links, shown_game_number(game)) if view.turn_key else None
        target = await self._ping_target(watch, channel, ping[1] if ping is not None else ())
        poster = await self.posters.for_sending(target)
        ping_id = None
        if ping is not None:
            text, user_ids = cards.build_turn_ping(view, links, shown_game_number(game), branded=poster.via == VIA_BOT)
            if reminder:
                text = f"{reminder}\n{text}"
            sent, poster = await self._send(
                "send_codenames_ping",
                watch.room_id,
                target,
                poster,
                text,
                view=cards.build_ping_view(watch.room_id),
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=uid) for uid in user_ids], everyone=False, roles=False
                ),
                # A reply can't point at a message in another channel.
                reply_to=card if target is channel else None,
            )
            ping_id = sent.id if sent is not None else None
        state = dict(game.state)
        state[_PING_KEY] = ping_id
        state[_PING_VIA_KEY] = poster.via
        state[_PING_KEY + _CHANNEL_SUFFIX] = str(target.id)
        return state

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reminder_check_seconds)
            try:
                await self.send_due_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Codenames reminders failed")

    def _reminder_interval(self, deadline: int | None, now: float) -> float:
        """How long a turn may wait between pings; shorter close to a tournament deadline."""
        interval = self.reminder_seconds
        if deadline and 0 < deadline - now <= self.deadline_window_seconds:
            interval = min(interval, self.deadline_reminder_seconds)
        return interval

    async def send_due_reminders(self, now: float | None = None) -> int:
        """Ping again the players a turn has been waiting on for too long. Returns how many were sent."""
        if self.reminder_seconds <= 0:
            return 0
        now = self._clock() if now is None else now
        sent = 0
        for room_id, view in list(self._views.items()):
            if view.phase != "in_progress" or not view.turn_key:
                continue
            async with self._locks.setdefault(room_id, asyncio.Lock()):
                links = await self.seat_links(room_id, view)
                for watch in await self.db.list_codenames_watches(room_id):
                    game = await self.db.get_latest_codenames_game(watch.watch_id)
                    if game is None or game.match_id != view.match_id or game.turn_key != view.turn_key:
                        continue
                    started = game.state.get(_TURN_STARTED_KEY)
                    if not isinstance(started, (int, float)):
                        # A turn from before reminders existed: start counting now.
                        state = dict(game.state)
                        state[_TURN_STARTED_KEY] = int(now)
                        await self.db.update_codenames_game(game.game_id, state=state)
                        continue
                    binding = await self.db.get_codenames_binding(watch.channel_id)
                    deadline = binding.deadline if binding is not None else None
                    last = game.state.get(_REMINDED_KEY) or started
                    if now - last < self._reminder_interval(deadline, now):
                        continue
                    channel = await self._channel(watch)
                    if channel is None:
                        continue
                    card = await self._card_message(channel, game)
                    note = cards.build_reminder_note(
                        int(now - started), deadline if deadline and deadline > now else None
                    )
                    state = await self._send_turn_ping(watch, game, view, links, channel, card, reminder=note)
                    state[_REMINDED_KEY] = int(now)
                    await self.db.update_codenames_game(game.game_id, state=state)
                    sent += 1
        return sent

    async def _notify_game_finished(
        self, watch: CodenamesWatch, game: CodenamesGame, view: GameView, links: dict[str, str]
    ) -> None:
        for listener in self.game_finished_listeners:
            try:
                await listener(watch, game, view, links)
            except Exception:
                LOGGER.exception("Codenames room %s: game-finished listener failed", watch.room_id)

    async def _send_or_edit_card(
        self, channel: discord.abc.Messageable, game: CodenamesGame, data: cards.CardData, *, copy: bool = False
    ) -> discord.Message | None:
        """Post or update the game's card in *channel*; with *copy*, the copy in the ping thread."""
        if game.state.get(_BOARD_KEY) and data.board_image is None:
            data = dataclasses.replace(data, board_image=BOARD_FILENAME)
        signature = cards.card_signature(data)
        key = (game.game_id, copy)
        message = await self._card_message(channel, game, copy=copy)
        if message is not None and self._signatures.get(key) == signature:
            return message
        attachment = await self._board_attachment(game) if data.board_image else None
        if attachment is None and data.board_image:
            data = dataclasses.replace(data, board_image=None)  # never point the card at a missing file
        layout = cards.build_card(data)
        edit_kwargs = (lambda: {"attachments": make_file(attachment)}) if attachment is not None else dict
        if message is not None:
            try:
                edited = await self._call(
                    "edit_codenames_card",
                    data.room_id,
                    lambda: message.edit(view=layout, allowed_mentions=discord.AllowedMentions.none(), **edit_kwargs()),
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
                attachment=attachment,
            )
            if message is None:
                return None
            state = dict(game.state)
            if copy:
                state[THREAD_CARD_KEY] = str(message.id)
                state[_THREAD_CARD_VIA_KEY] = poster.via
                await self.db.update_codenames_game(game.game_id, state=state)
            else:
                state[_CARD_VIA_KEY] = poster.via
                await self.db.update_codenames_game(game.game_id, message_id=message.id, state=state)
                game.message_id = message.id
            game.state = state
        self._messages[key] = message
        self._signatures[key] = signature
        return message

    async def _board_attachment(self, game: CodenamesGame) -> tuple[str, bytes] | None:
        try:
            snapshot = BoardSnapshot.from_dict(game.state[_BOARD_KEY])
            png = await asyncio.to_thread(render_board_png, snapshot)
        except Exception:
            LOGGER.exception("Codenames game %s: could not draw the board", game.game_id)
            return None
        return BOARD_FILENAME, png

    async def _card_message(
        self, channel: discord.abc.Messageable, game: CodenamesGame, *, copy: bool = False
    ) -> discord.Message | None:
        key = (game.game_id, copy)
        cached = self._messages.get(key)
        if cached is not None:
            return cached
        message_id = game.state.get(THREAD_CARD_KEY) if copy else game.message_id
        if not message_id or not str(message_id).isdecimal():
            return None
        poster = await self.posters.for_message(
            channel, game.state.get(_THREAD_CARD_VIA_KEY if copy else _CARD_VIA_KEY, VIA_BOT)
        )
        if poster is None:
            return None
        try:
            message = await self._call(
                "fetch_codenames_card", game.match_id, lambda: poster.fetch(int(message_id)), channel, kind="read"
            )
        except discord.NotFound:
            return None
        if message is not None:
            self._messages[key] = message
        return message

    async def _retire_game(self, watch: CodenamesWatch, game: CodenamesGame) -> None:
        """The room moved on to a new match before this one finished."""
        await self._delete_ping(watch, game)
        await self._delete_ping(watch, game, key=_LINEUP_PING_KEY, via_key=_LINEUP_PING_VIA_KEY)
        if game.lifecycle_state == "finished":
            return
        await self._close_card(watch, game, tr("cn_note_superseded"))

    async def _update_lineup_ping(
        self,
        watch: CodenamesWatch,
        game: CodenamesGame,
        view: GameView,
        lineup: cards.Lineup | None,
        links: dict[str, str],
        channel,
        card: discord.Message,
    ) -> None:
        """Before a tournament game starts, tell its players once where to sit."""
        if view.phase != "lobby":
            await self._delete_ping(watch, game, key=_LINEUP_PING_KEY, via_key=_LINEUP_PING_VIA_KEY)
            return
        if lineup is None or game.state.get(_LINEUP_SENT_KEY) == lineup.key or cards.lineup_ready(view, lineup, links):
            return
        await self._delete_ping(watch, game, key=_LINEUP_PING_KEY, via_key=_LINEUP_PING_VIA_KEY)
        text, user_ids = cards.build_lineup_ping(lineup)
        target = await self._ping_target(watch, channel, user_ids)
        sent, poster = await self._send(
            "send_codenames_lineup",
            watch.room_id,
            target,
            await self.posters.for_sending(target),
            text,
            view=cards.build_ping_view(watch.room_id),
            allowed_mentions=discord.AllowedMentions(
                users=[discord.Object(id=uid) for uid in user_ids], everyone=False, roles=False
            ),
            reply_to=card if target is channel else None,
        )
        state = dict(game.state)
        state[_LINEUP_PING_KEY] = sent.id if sent is not None else None
        state[_LINEUP_PING_VIA_KEY] = poster.via
        state[_LINEUP_PING_KEY + _CHANNEL_SUFFIX] = str(target.id)
        state[_LINEUP_SENT_KEY] = lineup.key
        await self.db.update_codenames_game(game.game_id, state=state)
        game.state = state

    async def _delete_ping(
        self, watch: CodenamesWatch, game: CodenamesGame, *, key: str = _PING_KEY, via_key: str = _PING_VIA_KEY
    ) -> None:
        ping_id = game.state.get(key)
        if not ping_id:
            return
        sent_in = game.state.get(key + _CHANNEL_SUFFIX)
        channel = await self._channel_by_id(sent_in, watch.room_id) if sent_in else await self._channel(watch)
        if channel is not None:
            # A webhook ping whose webhook is gone can still be deleted by the bot (Manage Messages).
            poster = await self.posters.for_message(channel, game.state.get(via_key, VIA_BOT)) or BotPoster(channel)
            try:
                await self._call(
                    "delete_codenames_ping", watch.room_id, lambda: poster.delete(int(ping_id)), channel, kind="delete"
                )
            except discord.NotFound:
                pass
            except discord.DiscordException as exc:
                LOGGER.warning("Codenames room %s: could not delete ping: %s", watch.room_id, exc)
        state = dict(game.state)
        state[key] = None
        await self.db.update_codenames_game(game.game_id, state=state)
        game.state = state

    async def _close_card(self, watch: CodenamesWatch, game: CodenamesGame, note: str) -> None:
        channel = await self._channel(watch)
        if channel is None:
            return
        view = self._views.get(watch.room_id)
        view = view if view is not None and view.match_id == game.match_id else None
        data = cards.CardData(
            room_id=watch.room_id,
            game_number=shown_game_number(game),
            # The last known state of this game, if the room was still on it.
            view=view,
            links=await self.seat_links(watch.room_id, view),
            closed_note=note,
        )
        await self._send_or_edit_card(channel, game, data)
        if game.state.get(THREAD_CARD_KEY) and watch.ping_thread_id:
            thread = await self._channel_by_id(watch.ping_thread_id, watch.room_id)
            if thread is not None:
                await self._update_thread_card(watch, thread, game, data)

    async def _update_thread_card(self, watch: CodenamesWatch, thread, game: CodenamesGame, data: cards.CardData) -> None:
        """The copy of the card in the ping thread; the one in the channel matters more."""
        try:
            await self._send_or_edit_card(thread, game, data, copy=True)
        except discord.DiscordException as exc:
            LOGGER.warning("Codenames room %s: could not update the card in the ping thread: %s", watch.room_id, exc)

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

    async def _channel_by_id(self, channel_id: str, room_id: str):
        if not str(channel_id).isdecimal():
            return None
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.DiscordException as exc:
                LOGGER.info("Codenames room %s: channel %s unavailable: %s", room_id, channel_id, exc)
                return None
        return channel if callable(getattr(channel, "send", None)) else None

    async def _ping_target(self, watch: CodenamesWatch, channel, user_ids=(), *, refresh: bool = True):
        """Where *watch*'s pings go: its ping thread when it has (or can open) one, else
        *channel*. Adds *user_ids* (the players) to a private thread so they can see it."""
        thread = await self._ping_thread(watch, channel, refresh=refresh) if self.ping_threads else None
        if thread is None:
            return channel
        if thread.type == discord.ChannelType.private_thread:
            added = self._ping_thread_members.setdefault(thread.id, set())
            for user_id in dict.fromkeys(int(uid) for uid in user_ids):
                if user_id in added:
                    continue
                try:
                    await self._call(
                        "add_codenames_thread_member",
                        watch.room_id,
                        lambda uid=user_id: thread.add_user(discord.Object(id=uid)),
                        thread,
                        kind="edit",
                    )
                    added.add(user_id)
                except discord.DiscordException as exc:
                    LOGGER.warning("Codenames room %s: could not add %s to the ping thread: %s", watch.room_id, user_id, exc)
        return thread

    async def _ping_thread(self, watch: CodenamesWatch, channel, *, refresh: bool = True) -> discord.Thread | None:
        """The watch's ping thread, opening one if the bot may (``None``: ping in the channel)."""
        if watch.ping_thread_id:
            thread = await self._channel_by_id(watch.ping_thread_id, watch.room_id)
            if isinstance(thread, discord.Thread):
                return thread
            # Deleted (or no longer visible): forget it, and open a new one below.
            await self.db.set_codenames_ping_thread(watch.watch_id, None)
            watch.ping_thread_id = None
        kind = ping_thread_kind(channel, getattr(getattr(channel, "guild", None), "me", None))
        if kind is None:
            return None
        failed_at = self._ping_thread_failed_at.get(watch.watch_id)
        if failed_at is not None and time.monotonic() - failed_at < _PING_THREAD_RETRY_SECONDS:
            return None
        try:
            thread = await self._call(
                "create_codenames_ping_thread",
                watch.room_id,
                lambda: channel.create_thread(
                    name=ping_thread_name(watch.room_id),
                    type=discord.ChannelType.private_thread if kind == PRIVATE else discord.ChannelType.public_thread,
                    auto_archive_duration=10080,
                    reason="Codenames turn pings, so the channel stays quiet",
                ),
                channel,
            )
        except discord.DiscordException as exc:
            LOGGER.warning("Codenames room %s: could not open a ping thread in %s: %s", watch.room_id, channel.id, exc)
            thread = None
        if thread is None:
            self._ping_thread_failed_at[watch.watch_id] = time.monotonic()
            return None
        self._ping_thread_failed_at.pop(watch.watch_id, None)
        await self.db.set_codenames_ping_thread(watch.watch_id, str(thread.id))
        watch.ping_thread_id = str(thread.id)
        LOGGER.info("Codenames room %s: turn pings go to %s thread %s", watch.room_id, kind, thread.id)
        try:
            await self._call(
                "send_codenames_thread_intro",
                watch.room_id,
                lambda: thread.send(
                    tr("cn_ping_thread_intro", room=watch.room_id, channel=f"<#{channel.id}>"),
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                thread,
            )
        except discord.DiscordException as exc:
            LOGGER.info("Codenames room %s: could not post the ping thread intro: %s", watch.room_id, exc)
        if refresh:
            await self.refresh_room(watch.room_id)  # the card now says where the pings are, and gets its copy
        return thread

    async def _send(self, operation: str, room_id: str, channel, poster, content=None, **kwargs):
        """Send with *poster*; if its webhook was deleted, set it up again or fall back to the bot.
        Returns ``(message or None, the poster that sent it)``."""
        try:
            return await self._call(operation, room_id, lambda: poster.send(content, **kwargs), channel), poster
        except discord.NotFound:
            if poster.via != VIA_WEBHOOK:
                raise
        except discord.HTTPException as exc:
            # E.g. a thread the webhook may not post in: send it as the bot instead.
            if poster.via != VIA_WEBHOOK:
                raise
            LOGGER.info("Codenames room %s: webhook could not post in %s (%s); sending as the bot", room_id, channel.id, exc)
            poster = BotPoster(channel)
            return await self._call(operation, room_id, lambda: poster.send(content, **kwargs), channel), poster
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
