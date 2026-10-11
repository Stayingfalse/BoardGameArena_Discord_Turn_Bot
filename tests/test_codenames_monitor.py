from __future__ import annotations

import dataclasses
import itertools
from datetime import datetime, timezone

import discord
import pytest
from test_codenames_state import _replay

from bga_turn.codenames import cards
from bga_turn.codenames.client import CLOSE_EXPIRED
from bga_turn.codenames.monitor import CodenamesMonitor
from bga_turn.codenames.views import ClaimSeatButton

ROOM = "test-room"
GUILD = "500"
CHANNEL = 600
# Discord users for the recorded players: Alice (seat 0), Bob (1), Carol (3), Dave (4).
DISCORD = {"0": "222", "1": "111", "3": "333", "4": "444"}

_ids = itertools.count(9000)


class FakeMessage:
    def __init__(self, channel: "FakeChannel", content: str | None, view, **kwargs) -> None:
        self.id = next(_ids)
        self.channel = channel
        self.content = content
        self.view = view
        self.kwargs = kwargs
        self.created_at = datetime.now(timezone.utc)
        self.edits: list = []
        self.deleted = False

    async def edit(self, *, view=None, allowed_mentions=None):
        self.view = view
        self.edits.append(view)
        return self

    async def delete(self):
        self.deleted = True

    def to_reference(self, fail_if_not_exists=True):
        return discord.MessageReference(message_id=self.id, channel_id=self.channel.id)


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: list[FakeMessage] = []

    async def send(self, content=None, *, view=None, **kwargs):
        message = FakeMessage(self, content, view, **kwargs)
        self.messages.append(message)
        return message

    async def fetch_message(self, message_id):
        for message in self.messages:
            if message.id == message_id and not message.deleted:
                return message
        raise discord.NotFound(_FakeResponse(), "missing")

    def get_partial_message(self, message_id):
        return next(m for m in self.messages if m.id == message_id)

    @property
    def cards(self) -> list[FakeMessage]:
        return [m for m in self.messages if m.view is not None]

    @property
    def pings(self) -> list[FakeMessage]:
        return [m for m in self.messages if m.content is not None]


class _FakeResponse:
    status = 404
    reason = "Not Found"


class FakeBot:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(_FakeResponse(), "missing")


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, room_id, *, credentials, bot_name, on_view, on_closed):
        self.room_id, self.credentials, self.bot_name = room_id, credentials, bot_name
        self.on_view, self.on_closed = on_view, on_closed
        self.started = self.stopped = False
        FakeClient.instances.append(self)

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class PassThroughLimiter:
    def io_allowed(self) -> bool:
        return True

    async def call(self, operation, key, factory, **kwargs):
        return await factory()


def _text(layout: discord.ui.LayoutView) -> str:
    parts: list[str] = []

    def walk(item):
        if isinstance(item, discord.ui.TextDisplay):
            parts.append(item.content)
        for child in getattr(item, "children", None) or getattr(item, "_children", []) or []:
            walk(child)
        if isinstance(item, discord.ui.Section):
            walk(item.accessory)

    for child in layout.children:
        walk(child)
    return "\n".join(parts)


def _buttons(layout: discord.ui.LayoutView) -> list[discord.ui.Button]:
    return [item for item in layout.walk_children() if isinstance(item, discord.ui.Button)]


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel(CHANNEL)


@pytest.fixture
def monitor(database, channel) -> CodenamesMonitor:
    FakeClient.instances.clear()
    return _monitor(database, channel)


def _monitor(database, channel) -> CodenamesMonitor:
    return CodenamesMonitor(
        FakeBot(channel),
        database,
        update_delay=0,
        client_factory=FakeClient,
        rate_limiter=PassThroughLimiter(),
    )


async def _show(monitor: CodenamesMonitor, view) -> None:
    monitor._views[ROOM] = view
    await monitor.update_room(ROOM)


async def _watch_and_link(monitor: CodenamesMonitor, database) -> list:
    """Watch the room, show the lobby, and have every recorded player claim their seat."""
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    views = list(_replay())
    await _show(monitor, _all_players_lobby(views))
    for seat, user_id in DISCORD.items():
        database.claim_codenames_player(ROOM, seat, user_id)
    return views


def _all_players_lobby(views):
    """The first lobby view, but with every player's name already known."""
    last_lobby = [v for v in views if v.phase == "lobby"][-1]
    return dataclasses.replace(views[0], player_names=last_lobby.player_names)


def _ping_users(message: FakeMessage) -> list[str]:
    return [str(user.id) for user in message.kwargs["allowed_mentions"].users]


async def test_watching_starts_one_client_with_the_rooms_stored_identity(monitor, database):
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id="601", created_by_discord_user_id="999")
    [client] = FakeClient.instances
    room = database.get_codenames_room(ROOM)
    assert client.started
    assert (client.credentials, client.bot_name) == (room.bot_credentials, room.bot_name)
    assert room.bot_name.startswith("🤖 ")


async def test_a_full_game_posts_one_card_and_pings_each_turn(monitor, database, channel):
    views = await _watch_and_link(monitor, database)
    for view in views:
        await _show(monitor, view)

    [card] = channel.cards
    assert "Game 1" in _text(card.view)
    # Exactly one ping per turn change, to exactly the players who must act.
    assert [_ping_users(p) for p in channel.pings] == [
        ["111"], ["222"], ["333"], ["444"], ["111"], ["222"], ["333"], ["444"], ["111"], ["222"]
    ]
    assert "your turn to give a clue" in channel.pings[0].content
    assert "**CLUE 1** · 4" in channel.pings[1].content
    # Each ping replaces the last; the game's end clears the final one.
    assert all(p.deleted for p in channel.pings)
    # Cards never ping by themselves.
    assert card.kwargs["allowed_mentions"].users is False

    final = _text(card.view)
    assert "Blue wins" in final and "assassin" in final
    assert not any(isinstance(b, ClaimSeatButton) for b in _buttons(card.view))
    [entry] = database.list_game_history_for_guild(GUILD)
    assert (entry.provider, entry.winner_names, entry.player_count) == ("codenames", ["blue"], 4)


async def test_play_again_posts_game_2_and_leaves_game_1(monitor, database, channel):
    views = await _watch_and_link(monitor, database)
    for view in views:
        await _show(monitor, view)
    game_1 = channel.cards[0]
    edits_before = len(game_1.edits)

    rematch = dataclasses.replace(views[0], match_id="QuVMIPSqNpU")
    await _show(monitor, rematch)

    assert len(channel.cards) == 2
    assert "Game 2" in _text(channel.cards[1].view)
    assert len(game_1.edits) == edits_before
    assert "Blue wins" in _text(game_1.view)


async def test_a_restart_mid_turn_does_not_ping_again(database, channel):
    FakeClient.instances.clear()
    first = _monitor(database, channel)
    views = await _watch_and_link(first, database)
    in_turn = next(v for v in views if v.turn_key == "red:spymasters")
    await _show(first, in_turn)
    assert len(channel.pings) == 1

    restarted = _monitor(database, channel)
    await restarted.start()
    await _show(restarted, in_turn)
    assert len(channel.pings) == 1
    assert len(channel.cards) == 1  # the existing card is found and reused


async def test_guesses_within_a_turn_only_edit_the_card(monitor, database, channel):
    views = await _watch_and_link(monitor, database)
    guessing = [v for v in views if v.turn_key == "red:operatives"][:4]
    for view in guessing:
        await _show(monitor, view)
    assert len(channel.pings) == 1
    assert len(channel.cards) == 1


async def test_unlinked_players_get_no_ping(monitor, database, channel):
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    in_turn = next(v for v in _replay() if v.turn_key == "red:spymasters")
    await _show(monitor, in_turn)
    assert channel.pings == []
    assert database.get_latest_codenames_game(1).turn_key == "red:spymasters"


async def test_invited_members_are_matched_by_their_remembered_name(monitor, database, channel):
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    views = list(_replay())
    # Alice played as "Alice" in another room before.
    database.get_or_create_codenames_room("old-room", bot_credentials="x", bot_name="🤖 Silent Butler")
    database.update_codenames_player_names("old-room", {"7": "Alice"})
    database.claim_codenames_player("old-room", "7", "222")
    [watch] = database.list_codenames_watches(ROOM)
    database.add_codenames_watch_members(watch.watch_id, ["222", "555"])

    await _show(monitor, _all_players_lobby(views))

    players = {p.player_id: p.discord_user_id for p in database.list_codenames_players(ROOM)}
    assert players["0"] == "222"
    text = _text(channel.cards[0].view)
    assert "<@222>" in text  # shown next to Alice
    assert "Asked to play: <@555>" in text  # still to pick a name


async def test_room_closing_marks_the_card_and_stops_watching(monitor, database, channel):
    views = await _watch_and_link(monitor, database)
    in_turn = next(v for v in views if v.turn_key == "red:spymasters")
    await _show(monitor, in_turn)

    await FakeClient.instances[0].on_closed(CLOSE_EXPIRED)

    card = channel.cards[0]
    assert "room has closed" in _text(card.view)
    assert channel.pings[0].deleted
    assert database.list_codenames_watches(ROOM) == []
    [entry] = database.list_game_history_for_guild(GUILD)
    assert entry.outcome == "cancelled"


async def test_unwatching_the_last_channel_stops_the_client(monitor, database, channel):
    watch, _ = await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    other, _ = await monitor.watch_room(ROOM, guild_id=GUILD, channel_id="601", created_by_discord_user_id="9")
    [client] = FakeClient.instances
    await monitor.unwatch(other)
    assert not client.stopped
    await monitor.unwatch(watch)
    assert client.stopped and not monitor.is_watching(ROOM)


async def test_removing_a_guild_stops_its_rooms(monitor, database):
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    assert await monitor.remove_guild(GUILD) == 1
    assert FakeClient.instances[0].stopped


async def test_an_idle_room_is_closed(monitor, database, channel):
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    monitor.idle_seconds = 0
    monitor._last_active[ROOM] = 0.0
    await _show(monitor, next(_replay()))
    assert database.list_codenames_watches(ROOM) == []
    assert FakeClient.instances[0].stopped


def test_ping_is_skipped_when_nobody_to_act_is_linked():
    view = next(v for v in _replay() if v.turn_key == "red:operatives")
    assert cards.build_turn_ping(view, {}, 1) is None
    text, users = cards.build_turn_ping(view, {"0": "222"}, 3)
    assert users == [222]
    assert "Game 3" in text and "**CLUE 1** · 4" in text


def test_card_names_are_escaped():
    view = dataclasses.replace(next(_replay()), player_names={"0": "**bold** @everyone"})
    text = _text(cards.build_card(cards.CardData(room_id=ROOM, game_number=1, view=view)))
    assert "**bold**" not in text and "@everyone" not in text


def _players(*rows):
    from bga_turn.codenames.models import CodenamesPlayer

    return [CodenamesPlayer(room_id=ROOM, player_id=pid, display_name=name, discord_user_id=uid) for pid, name, uid in rows]


async def test_seat_picker_never_preselects_so_any_name_can_be_picked():
    from bga_turn.codenames.views import SeatPickerView

    players = _players(("0", "Alice", None), ("1", "Bob", None), ("3", "Carol", "333"))
    # Released their seat earlier, but "Bob" is still the remembered name.
    view = SeatPickerView(ROOM, players, "111", remembered="bob")
    [select] = [item for item in view.children if isinstance(item, discord.ui.Select)]
    assert not any(option.default for option in select.options)
    assert [option.label for option in select.options] == ["Bob", "Alice", "Carol"]
    assert select.options[0].description == "The name you used last time"
    assert select.options[2].description == "Claimed by someone else"
    assert not any(isinstance(item, discord.ui.Button) for item in view.children)


async def test_seat_picker_marks_the_current_seat_and_offers_release():
    from bga_turn.codenames.views import SeatPickerView

    players = _players(("0", "Alice", None), ("1", "Bob", "111"))
    view = SeatPickerView(ROOM, players, "111", remembered="Alice")
    [select] = [item for item in view.children if isinstance(item, discord.ui.Select)]
    assert not any(option.default for option in select.options)
    assert (select.options[0].label, select.options[0].description) == ("Bob", "That's you now")
    assert any(isinstance(item, discord.ui.Button) for item in view.children)
