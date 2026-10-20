from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest
from test_codenames_monitor import (
    CHANNEL,
    DISCORD,
    GUILD,
    ROOM,
    FakeBot,
    FakeChannel,
    FakeClient,
    PassThroughLimiter,
    _all_players_lobby,
    _ping_users,
    _show,
    _text,
    _watch_and_link,
)
from test_codenames_state import _replay

from bga_turn.codenames.monitor import CodenamesMonitor, normalise_name

# The recording's seats: red spymaster Bob (111), red operative Alice (222),
# blue spymaster Carol (333), blue operative Dave (444).
RIGHT = {"red": {"spymaster": "111", "operative": "222"}, "blue": {"spymaster": "333", "operative": "444"}}
SWAPPED_BLUE = {"red": RIGHT["red"], "blue": {"spymaster": "444", "operative": "333"}}


class GuildBot(FakeBot):
    """FakeBot with a guild whose members have Discord names."""

    def __init__(self, channel, members: dict[str, str]) -> None:
        super().__init__(channel)
        self.members = members

    def get_guild(self, guild_id):
        def get_member(user_id):
            name = self.members.get(str(user_id))
            if name is None:
                return None
            return SimpleNamespace(nick=None, display_name=name, global_name=None, name=name.lower())

        async def fetch_member(user_id):
            raise LookupError  # never reached: get_member answers or returns None

        return SimpleNamespace(get_member=get_member, fetch_member=fetch_member)


def _monitor(database, channel, members=None) -> CodenamesMonitor:
    FakeClient.instances.clear()
    return CodenamesMonitor(
        GuildBot(channel, members or {}),
        database,
        update_delay=0,
        client_factory=FakeClient,
        rate_limiter=PassThroughLimiter(),
    )


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel(CHANNEL)


def _bind(database, lineup_seats, *, game=1, last_event_id=None, names=None):
    database.upsert_codenames_binding(
        str(CHANNEL),
        guild_id=GUILD,
        external_ref="R1M1",
        players=list(DISCORD.values()),
        player_names=names or {},
        lineup={"game": game, "last_event_id": last_event_id, **lineup_seats},
    )


def _in_progress(views):
    return next(v for v in views if v.phase == "in_progress")


async def test_the_card_confirms_a_correct_line_up(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, RIGHT)
    views = await _watch_and_link(monitor, database)
    await _show(monitor, _in_progress(views))
    assert "Game 1 line-up** ✅" in _text(channel.cards[0].view)


async def test_the_card_flags_players_in_the_wrong_seat(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, SWAPPED_BLUE, game=2)
    views = await _watch_and_link(monitor, database)
    await _show(monitor, _in_progress(views))

    text = _text(channel.cards[0].view)
    assert "Game 2 line-up**" in text and "✅ everyone" not in text
    assert "⚠️ 🔵 🧠 Spymaster: <@444> · sitting as 🔵 🔎 Operatives" in text
    assert "⚠️ 🔵 🔎 Operatives: <@333> · sitting as 🔵 🧠 Spymaster" in text
    assert "✅ 🔴 🧠 Spymaster: <@111>" in text


async def test_unclaimed_players_are_told_to_press_im_playing(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, RIGHT)
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    await _show(monitor, _in_progress(list(_replay())))
    assert "⏳ 🔴 🧠 Spymaster: <@111> · hasn't pressed **I'm playing** yet" in _text(channel.cards[0].view)


async def test_the_line_up_ping_is_sent_once_in_the_lobby_and_removed_at_the_start(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, RIGHT)
    views = await _watch_and_link(monitor, database)
    lobby = _all_players_lobby(views)  # nobody on a team yet

    await _show(monitor, lobby)
    await _show(monitor, dataclasses.replace(lobby, state_id=(lobby.state_id or 0) + 1))
    [ping] = [p for p in channel.pings if "line-up" in p.content]
    assert sorted(_ping_users(ping)) == sorted(DISCORD.values())
    assert "🔵 🧠 Spymaster <@333>" in ping.content

    await _show(monitor, _in_progress(views))
    assert ping.deleted


async def test_a_new_line_up_replaces_the_old_ping(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, RIGHT)
    views = await _watch_and_link(monitor, database)
    lobby = _all_players_lobby(views)
    await _show(monitor, lobby)

    _bind(database, SWAPPED_BLUE, game=2)
    await _show(monitor, dataclasses.replace(lobby, state_id=(lobby.state_id or 0) + 1))

    first, second = [p for p in channel.pings if "line-up" in p.content]
    assert first.deleted and not second.deleted
    assert "Game 2" in second.content


async def test_a_line_up_behind_the_reported_results_is_not_shown(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, RIGHT, last_event_id=None)
    # The bot reported a game the tournament hasn't processed yet: its line-up is stale.
    database.enqueue_bridge_result("room:m1", channel_id=str(CHANNEL), external_ref="R1M1", payload={})
    # Re-sending the same line-up (say the deadline moved) doesn't make it fresh.
    _bind(database, RIGHT, last_event_id=None)
    views = await _watch_and_link(monitor, database)
    await _show(monitor, _all_players_lobby(views))

    assert "line-up" not in _text(channel.cards[0].view)
    assert not [p for p in channel.pings if "line-up" in p.content]

    _bind(database, RIGHT, game=2, last_event_id="room:m1")
    await monitor.refresh_room(ROOM)
    await _show(monitor, _all_players_lobby(views))
    assert "Game 2 line-up" in _text(channel.cards[0].view)


async def test_a_line_up_sent_after_an_unprocessed_result_is_shown(database, channel):
    monitor = _monitor(database, channel)
    _bind(database, RIGHT, last_event_id=None)
    # Game 1's result never reached the tournament, so it was logged by hand there
    # and the tournament moved on to Game 2 without knowing this result's event_id.
    database.enqueue_bridge_result("room:m1", channel_id=str(CHANNEL), external_ref="R1M1", payload={})
    _bind(database, SWAPPED_BLUE, game=2, last_event_id=None)

    views = await _watch_and_link(monitor, database)
    await _show(monitor, _all_players_lobby(views))
    assert "Game 2 line-up" in _text(channel.cards[0].view)
    assert [p for p in channel.pings if "Game 2" in p.content]


async def test_bindings_stored_before_the_line_up_marker_fall_back_to_timestamps(database, channel):
    database.enqueue_bridge_result("room:m1", channel_id=str(CHANNEL), external_ref="R1M1", payload={})
    _bind(database, SWAPPED_BLUE, game=2, last_event_id=None)
    binding = database.get_codenames_binding(str(CHANNEL))
    [result] = database.list_bridge_results(str(CHANNEL))
    legacy = dataclasses.replace(binding, lineup_after_outbox_id=None)
    created = database._connection.execute(
        "SELECT created_at FROM codenames_result_outbox WHERE outbox_id = ?", (result.outbox_id,)
    ).fetchone()["created_at"]

    assert database.codenames_lineup_is_current(dataclasses.replace(legacy, updated_at=created + "Z"))
    assert not database.codenames_lineup_is_current(dataclasses.replace(legacy, updated_at=created))


# ----------------------------------------------------------------------
# Matching seats by name
# ----------------------------------------------------------------------


def test_names_are_compared_without_case_spaces_or_emoji():
    assert normalise_name("🤖 Alice B.") == normalise_name("alice_b") == "aliceb"
    assert normalise_name("🤖") == ""


async def _lobby_with_invites(database, monitor, invited):
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    [watch] = database.list_codenames_watches(ROOM)
    database.add_codenames_watch_members(watch.watch_id, invited)
    await _show(monitor, _all_players_lobby(list(_replay())))
    return {p.player_id: p.discord_user_id for p in database.list_codenames_players(ROOM)}


async def test_invited_players_get_the_seat_matching_their_discord_name(database, channel):
    monitor = _monitor(database, channel, members={"222": "alice ✨", "111": "Bobby"})
    seats = await _lobby_with_invites(database, monitor, ["222", "111"])
    assert seats["0"] == "222"  # "Alice" in the room
    assert seats["1"] is None  # "Bob" isn't "Bobby"


async def test_tournament_names_are_used_too(database, channel):
    monitor = _monitor(database, channel, members={"333": "Caroline"})
    database.upsert_codenames_binding(
        str(CHANNEL), guild_id=GUILD, external_ref="R1M1", players=["333"], player_names={"333": "Carol"}
    )
    seats = await _lobby_with_invites(database, monitor, ["333"])
    assert seats["3"] == "333"


async def test_ambiguous_names_are_left_alone(database, channel):
    monitor = _monitor(database, channel, members={"222": "Alice", "555": "alice"})
    seats = await _lobby_with_invites(database, monitor, ["222", "555"])
    assert seats["0"] is None
