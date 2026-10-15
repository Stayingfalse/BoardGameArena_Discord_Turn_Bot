"""/codenames seats: an admin says which name in the room each invited player is."""

from __future__ import annotations

from types import SimpleNamespace

import discord

from bga_turn.codenames.commands import CodenamesCommands
from bga_turn.codenames.views import SeatAssignView, _AssignSelect, build_seat_assign_view
from bga_turn.database import AsyncDatabase
from bga_turn.i18n import tr

ROOM = "juzaf-hitos"
GUILD = "500"
CHANNEL = "600"
ALICE, BOB, CAROL, DAVE = "111", "222", "333", "444"


def _setup(database):
    database.get_or_create_codenames_room(ROOM, bot_credentials="aaa", bot_name="🤖 Curious Cat")
    database.record_codenames_match(ROOM, "m1", bot_player_id="9")
    watch, _ = database.add_codenames_watch(ROOM, guild_id=GUILD, channel_id=CHANNEL, created_by_discord_user_id=ALICE)
    database.update_codenames_player_names(ROOM, {"0": "Potato", "1": "Code Bot", "2": "Bobby", "9": "🤖 Curious Cat"})
    database.add_codenames_watch_members(watch.watch_id, [ALICE, BOB])
    return watch


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.edits: list[dict] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


class FakeMonitor:
    def __init__(self) -> None:
        self.refreshed: list[str] = []

    async def refresh_room(self, room_id: str) -> None:
        self.refreshed.append(room_id)


def _interaction(database, *, admin: bool = True, monitor: FakeMonitor | None = None):
    permissions = discord.Permissions(manage_messages=True) if admin else discord.Permissions.none()
    names = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol", DAVE: "Dave"}
    guild = SimpleNamespace(get_member=lambda uid: SimpleNamespace(display_name=names.get(str(uid), "?")))
    return SimpleNamespace(
        guild_id=int(GUILD),
        channel_id=int(CHANNEL),
        guild=guild,
        user=SimpleNamespace(id=int(ALICE)),
        permissions=permissions,
        client=SimpleNamespace(database=database, codenames_monitor=monitor or FakeMonitor()),
        response=FakeResponse(),
    )


def _texts(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _selects(view: discord.ui.LayoutView) -> list[_AssignSelect]:
    return [item for item in view.walk_children() if isinstance(item, _AssignSelect)]


async def _pick(select: _AssignSelect, value: str, interaction) -> None:
    select._values = [value]  # what discord.py sets from the interaction payload
    await select.callback(interaction)


async def test_one_dropdown_per_invited_player_with_every_name_in_the_room(database):
    watch = _setup(database)
    database.claim_codenames_player(ROOM, "2", CAROL)  # Carol claimed a seat but wasn't invited
    view = await build_seat_assign_view(AsyncDatabase(database), ROOM, watch.watch_id, _interaction(database).guild)
    assert isinstance(view, SeatAssignView)
    selects = _selects(view)
    # Invited players first, then anyone else linked to a seat.
    assert [s.user_id for s in selects] == [ALICE, BOB, CAROL]
    labels = [o.label for o in selects[0].options]
    assert labels == ["Potato", "Code Bot", "Bobby", tr("cn_seats_option_not_playing")]  # no bot seat
    bobby = next(o for o in selects[0].options if o.label == "Bobby")
    assert bobby.description == tr("cn_seats_option_held_by", name="Carol")
    texts = _texts(view)
    assert tr("cn_seats_row_unlinked", user=f"<@{ALICE}>") in texts
    assert tr("cn_seats_row_linked", user=f"<@{CAROL}>", name="Bobby") in texts


async def test_admin_links_a_player_and_takes_the_seat_from_a_wrong_claim(database):
    watch = _setup(database)
    database.claim_codenames_player(ROOM, "2", CAROL)  # Carol wrongly claimed Bob's name
    monitor = FakeMonitor()
    interaction = _interaction(database, monitor=monitor)
    view = await build_seat_assign_view(AsyncDatabase(database), ROOM, watch.watch_id, interaction.guild)
    bob_select = next(s for s in _selects(view) if s.user_id == BOB)
    await _pick(bob_select, "2", interaction)

    links = {p.player_id: p.discord_user_id for p in database.list_codenames_players(ROOM)}
    assert links["2"] == BOB
    assert CAROL not in links.values()
    assert monitor.refreshed == [ROOM]
    updated = interaction.response.edits[0]["view"]
    texts = _texts(updated)
    assert tr("cn_seats_done_linked", user=f"<@{BOB}>", name="Bobby") in texts
    assert tr("cn_seats_done_replaced", user=f"<@{CAROL}>") in texts
    assert tr("cn_seats_row_linked", user=f"<@{BOB}>", name="Bobby") in texts


async def test_moving_a_player_releases_their_old_seat_and_not_playing_unlinks(database):
    watch = _setup(database)
    database.claim_codenames_player(ROOM, "0", ALICE)
    interaction = _interaction(database)
    view = await build_seat_assign_view(AsyncDatabase(database), ROOM, watch.watch_id, interaction.guild)
    await _pick(_selects(view)[0], "1", interaction)
    links = {p.player_id: p.discord_user_id for p in database.list_codenames_players(ROOM)}
    assert (links["0"], links["1"]) == (None, ALICE)

    view = interaction.response.edits[-1]["view"]
    await _pick(_selects(view)[0], "__not_playing__", interaction)
    assert ALICE not in {p.discord_user_id for p in database.list_codenames_players(ROOM)}
    assert tr("cn_seats_done_released", user=f"<@{ALICE}>") in _texts(interaction.response.edits[-1]["view"])


async def test_only_admins_can_use_it(database):
    watch = _setup(database)
    cog = CodenamesCommands(database, monitor=FakeMonitor())
    interaction = _interaction(database, admin=False)
    await CodenamesCommands.seats.callback(cog, interaction)
    assert interaction.response.messages == [{"content": tr("cn_seats_not_allowed"), "ephemeral": True}]

    view = await build_seat_assign_view(AsyncDatabase(database), ROOM, watch.watch_id, interaction.guild)
    await _pick(_selects(view)[0], "0", interaction)
    assert all(p.discord_user_id is None for p in database.list_codenames_players(ROOM))


async def test_command_sends_the_card_privately(database):
    _setup(database)
    cog = CodenamesCommands(database, monitor=FakeMonitor())
    interaction = _interaction(database)
    await CodenamesCommands.seats.callback(cog, interaction)
    sent = interaction.response.messages[0]
    assert sent["ephemeral"] is True
    assert isinstance(sent["view"], SeatAssignView)


async def test_empty_room_is_explained(database):
    database.get_or_create_codenames_room(ROOM, bot_credentials="aaa", bot_name="🤖 Curious Cat")
    database.add_codenames_watch(ROOM, guild_id=GUILD, channel_id=CHANNEL, created_by_discord_user_id=ALICE)
    cog = CodenamesCommands(database, monitor=FakeMonitor())
    interaction = _interaction(database)
    await CodenamesCommands.seats.callback(cog, interaction)
    assert interaction.response.messages == [{"content": tr("cn_seats_room_empty"), "ephemeral": True}]


def test_a_full_card_fits_discords_component_limit():
    from bga_turn.codenames.models import CodenamesPlayer

    seats = [CodenamesPlayer(ROOM, str(i), f"Name {i}", None) for i in range(30)]
    view = SeatAssignView(ROOM, 1, seats, [str(1000 + i) for i in range(14)], None, note="✅ done")
    assert view.total_children_count <= 40
    first = _selects(view)[0]
    assert len(first.options) == 25 and first.options[-1].value == "__not_playing__"
    assert tr("cn_seats_more", count=4) in _texts(view)
