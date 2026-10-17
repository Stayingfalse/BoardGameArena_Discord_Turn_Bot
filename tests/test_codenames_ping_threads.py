"""Turn pings go to a thread for the room when the card is in an ordinary channel."""

from __future__ import annotations

import discord
import pytest
from test_codenames_monitor import (
    CHANNEL,
    GUILD,
    ROOM,
    FakeChannel,
    FakeClient,
    FakeMessage,
    PassThroughLimiter,
    _all_players_lobby,
    _ping_users,
    _show,
    _text,
)
from test_codenames_state import _replay

from bga_turn.codenames import cards
from bga_turn.i18n import tr
from bga_turn.codenames.monitor import CodenamesMonitor
from bga_turn.codenames.ping_threads import PRIVATE, PUBLIC, ping_thread_kind

DISCORD = {"0": "222", "1": "111", "3": "333", "4": "444"}
THREAD_ID = 7000


class _Messages:
    """FakeChannel's message handling, mixed into real discord.py channel classes below."""

    def _init_messages(self, channel_id: int) -> None:
        self.id = channel_id
        self.messages: list[FakeMessage] = []

    send = FakeChannel.send
    fetch_message = FakeChannel.fetch_message
    get_partial_message = FakeChannel.get_partial_message
    cards = FakeChannel.cards
    pings = FakeChannel.pings


class FakeThread(_Messages, discord.Thread):
    def __init__(self, thread_id: int, kind: discord.ChannelType, parent) -> None:  # noqa: D401
        self._init_messages(thread_id)
        self._type = kind.value
        self.parent_id = parent.id
        self.guild = parent.guild
        self.added: list[int] = []

    @property
    def type(self) -> discord.ChannelType:
        return discord.ChannelType(self._type)

    async def add_user(self, user) -> None:
        self.added.append(user.id)


class FakeTextChannel(_Messages, discord.TextChannel):
    """A real ``discord.TextChannel`` (so isinstance checks pass) with chosen permissions."""

    def __init__(self, channel_id: int, permissions: discord.Permissions, *, news: bool = False) -> None:
        self._init_messages(channel_id)
        self._type = (discord.ChannelType.news if news else discord.ChannelType.text).value
        self._permissions = permissions
        self.guild = type_("Guild", me=object(), id=int(GUILD), get_channel=lambda _, cid: self if cid == channel_id else None)
        self.created_threads: list[FakeThread] = []
        self.fail_create = False

    @property
    def type(self) -> discord.ChannelType:
        return discord.ChannelType(self._type)

    def permissions_for(self, member) -> discord.Permissions:
        return self._permissions

    async def create_thread(self, *, name, type, auto_archive_duration, reason):  # noqa: A002
        if self.fail_create:
            raise discord.Forbidden(type_("Response", status=403, reason="Forbidden"), "missing access")
        thread = FakeThread(THREAD_ID + len(self.created_threads), type, self)
        thread.name = name
        self.created_threads.append(thread)
        return thread


def type_(name, **attrs):
    """A one-off object with *attrs* (callables become methods)."""
    return type(name, (), attrs)()


class ThreadBot:
    def __init__(self, channel: FakeTextChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id):
        if channel_id == self.channel.id:
            return self.channel
        return next((t for t in self.channel.created_threads if t.id == channel_id), None)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(type_("Response", status=404, reason="Not Found"), "missing")


PRIVATE_OK = discord.Permissions(view_channel=True, send_messages_in_threads=True, create_private_threads=True, create_public_threads=True)
PUBLIC_ONLY = discord.Permissions(view_channel=True, send_messages_in_threads=True, create_public_threads=True)
NO_THREADS = discord.Permissions(view_channel=True, send_messages=True)


def _monitor(database, channel, **kwargs) -> CodenamesMonitor:
    FakeClient.instances.clear()
    return CodenamesMonitor(
        ThreadBot(channel), database, update_delay=0, client_factory=FakeClient, rate_limiter=PassThroughLimiter(), **kwargs
    )


async def _play(monitor, database, *, until_turns: int | None = None) -> None:
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    views = list(_replay())
    await _show(monitor, _all_players_lobby(views))
    for seat, user_id in DISCORD.items():
        database.claim_codenames_player(ROOM, seat, user_id)
    turns = 0
    last_turn = None
    for view in views:
        if until_turns is not None and view.turn_key and view.turn_key != last_turn:
            turns += 1
            last_turn = view.turn_key
            if turns > until_turns:
                break
        await _show(monitor, view)


def test_thread_kind_follows_the_bots_permissions():
    me = object()
    assert ping_thread_kind(FakeTextChannel(1, PRIVATE_OK), me) == PRIVATE
    assert ping_thread_kind(FakeTextChannel(1, PUBLIC_ONLY), me) == PUBLIC
    assert ping_thread_kind(FakeTextChannel(1, NO_THREADS), me) is None
    # Announcement channels can't have private threads.
    assert ping_thread_kind(FakeTextChannel(1, PRIVATE_OK, news=True), me) == PUBLIC
    # Already a thread (e.g. a tournament match), or not a text channel: leave it.
    thread = FakeThread(2, discord.ChannelType.public_thread, FakeTextChannel(1, PRIVATE_OK))
    assert ping_thread_kind(thread, me) is None
    assert ping_thread_kind(FakeChannel(1), me) is None
    assert ping_thread_kind(FakeTextChannel(1, PRIVATE_OK), None) is None


async def test_pings_go_to_a_private_thread_and_the_card_stays_in_the_channel(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    monitor = _monitor(database, channel)
    await _play(monitor, database, until_turns=3)

    [thread] = channel.created_threads
    assert thread.type == discord.ChannelType.private_thread
    assert ROOM in thread.name
    # The channel only has the card; every ping (and the intro) is in the thread.
    assert channel.pings == [] and len(channel.cards) == 1
    pings = [m for m in thread.messages if m.kwargs.get("allowed_mentions") and m.kwargs["allowed_mentions"].users]
    assert [_ping_users(p) for p in pings] == [["111"], ["222"], ["333"]]
    assert ROOM in thread.messages[0].content  # intro first, pinging nobody
    # Pings can't reply to a card in another channel.
    assert all("reference" not in p.kwargs for p in pings)
    # Every seated player is added to the private thread, once, before anyone is pinged.
    assert sorted(thread.added) == [111, 222, 333, 444]
    # The thread keeps a copy of the card, updated along with the one in the channel.
    [copy] = thread.cards
    assert thread.messages.index(copy) < thread.messages.index(pings[0])
    assert copy.edits and _text(copy.view) == _text(channel.cards[0].view).replace(
        "\n" + tr("cn_pings_in_thread", thread=f"<#{thread.id}>"), ""
    )
    assert f"<#{thread.id}>" not in _text(copy.view)
    # The previous ping is deleted from the thread, not looked for in the channel.
    assert [p.deleted for p in pings] == [True, True, False]
    # The card says where the pings are.
    assert f"<#{thread.id}>" in _text(channel.cards[0].view)
    assert database.list_codenames_watches(ROOM)[0].ping_thread_id == str(thread.id)


async def test_public_thread_when_private_is_not_allowed(database):
    channel = FakeTextChannel(CHANNEL, PUBLIC_ONLY)
    await _play(_monitor(database, channel), database, until_turns=2)
    [thread] = channel.created_threads
    assert thread.type == discord.ChannelType.public_thread
    assert thread.added == []  # anyone can see a public thread
    assert channel.pings == []
    assert len(thread.cards) == 1


async def test_the_thread_opens_for_invited_players_before_the_game(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    monitor = _monitor(database, channel)
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    database.add_codenames_watch_members(database.list_codenames_watches(ROOM)[0].watch_id, ["111", "555"])
    await _show(monitor, list(_replay())[0])
    [thread] = channel.created_threads
    assert sorted(thread.added) == [111, 555]
    assert len(thread.cards) == 1 and thread.pings[0].content.startswith("🔔")  # intro, no pings yet


async def test_nobody_to_add_means_no_thread_yet(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    monitor = _monitor(database, channel)
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="999")
    await _show(monitor, list(_replay())[0])
    assert channel.created_threads == [] and len(channel.cards) == 1


async def test_the_claim_button_works_on_the_copy_in_the_thread(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    await _play(_monitor(database, channel), database, until_turns=1)
    [copy] = channel.created_threads[0].cards
    game = database.get_codenames_game_by_message(copy.id)
    assert game is not None and game.game_id == database.get_codenames_game_by_message(channel.cards[0].id).game_id


async def test_without_thread_permissions_nothing_changes(database):
    channel = FakeTextChannel(CHANNEL, NO_THREADS)
    await _play(_monitor(database, channel), database, until_turns=2)
    assert channel.created_threads == []
    assert [_ping_users(p) for p in channel.pings] == [["111"], ["222"]]
    assert "reference" in channel.pings[0].kwargs  # still a reply to the card


async def test_a_failed_thread_falls_back_to_the_channel(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    channel.fail_create = True
    await _play(_monitor(database, channel), database, until_turns=2)
    assert channel.created_threads == []
    assert len(channel.pings) == 2


async def test_it_can_be_turned_off(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    await _play(_monitor(database, channel, ping_threads=False), database, until_turns=2)
    assert channel.created_threads == [] and len(channel.pings) == 2


async def test_the_thread_is_reused_after_a_restart(database):
    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    await _play(_monitor(database, channel), database, until_turns=1)
    restarted = _monitor(database, channel)
    await restarted.start()
    views = [v for v in _replay() if v.turn_key]
    second_turn = next(v for v in views if v.turn_key != views[0].turn_key)
    await _show(restarted, second_turn)
    assert len(channel.created_threads) == 1
    assert channel.pings == []
    assert len(channel.created_threads[0].cards) == 1  # the copy is edited, not posted again


def test_card_mentions_the_ping_thread_only_while_the_game_is_on():
    data = cards.CardData(room_id=ROOM, game_number=1, view=None, pings_channel_id="7000")
    assert "<#7000>" in _text(cards.build_card(data))
    closed = cards.CardData(room_id=ROOM, game_number=1, view=None, pings_channel_id="7000", closed_note="Stopped")
    assert "<#7000>" not in _text(cards.build_card(closed))


@pytest.fixture(autouse=True)
def _clear_clients():
    FakeClient.instances.clear()


async def test_a_webhook_that_cannot_post_in_the_thread_falls_back_to_the_bot(database):
    from bga_turn.codenames.posting import VIA_BOT, VIA_WEBHOOK

    channel = FakeTextChannel(CHANNEL, PRIVATE_OK)
    thread = FakeThread(THREAD_ID, discord.ChannelType.private_thread, channel)

    class RefusingWebhook:
        via = VIA_WEBHOOK

        async def send(self, content=None, **kwargs):
            raise discord.HTTPException(type_("Response", status=400, reason="Bad Request"), "cannot post here")

    monitor = _monitor(database, channel)
    sent, poster = await monitor._send(
        "send_codenames_ping", ROOM, thread, RefusingWebhook(), "ping", allowed_mentions=discord.AllowedMentions.none()
    )
    assert poster.via == VIA_BOT
    assert sent is thread.messages[0] and sent.content == "ping"
