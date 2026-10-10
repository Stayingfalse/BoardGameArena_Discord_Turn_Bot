"""One BGA account per Discord user, self-service changes, and private global stats."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bga_turn.commands_bga import BgaCommands, StatsLayoutView
from bga_turn.database import LinkConflictError
from bga_turn.i18n import tr
from bga_turn.views import LinkBgaModal, ManageOwnLinkView


def test_claimed_name_or_id_is_refused(database):
    database.link_user("1", "111", "Alice")
    with pytest.raises(LinkConflictError) as caught:
        database.link_user("2", None, "alice")  # names match case-insensitively
    assert caught.value.holder.discord_user_id == "1"
    with pytest.raises(LinkConflictError):
        database.link_user("2", "111", None)
    assert database.get_linked_user("2") is None


def test_override_takes_the_account_from_its_holder(database):
    database.link_user("1", "111", "Alice")
    database.link_user("2", "111", "Alice", override=True)
    assert database.get_linked_user("1") is None
    assert database.get_linked_user("2").bga_player_id == "111"


def test_relinking_to_a_new_name_drops_the_old_id(database):
    database.link_user("1", "111", "Alice")
    linked = database.link_user("1", None, "Bob")
    assert (linked.bga_player_id, linked.bga_player_name) == ("", "Bob")
    # The same name again keeps the ID the bot already learned.
    database.link_user("1", "222", "Bob")
    assert database.link_user("1", None, "bob").bga_player_id == "222"
    # An ID alone keeps the name only if the ID is unchanged.
    assert database.link_user("1", "222", None).bga_player_name == "bob"
    assert database.link_user("1", "333", None).bga_player_name == ""


def test_relinking_yourself_is_not_a_conflict(database):
    database.link_user("1", "111", "Alice")
    assert database.link_user("1", "111", "Alice").bga_player_name == "Alice"


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.edits: list[dict] = []
        self.modals: list[discord.ui.Modal] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.modals.append(modal)


def _interaction(database, user_id: int):
    return SimpleNamespace(
        guild_id=1,
        user=SimpleNamespace(id=user_id),
        client=SimpleNamespace(database=database),
        response=FakeResponse(),
    )


async def test_self_link_cannot_take_someone_elses_account(database):
    database.link_user("1", "111", "Alice")
    modal = LinkBgaModal()
    modal.bga_input._value = "Alice"
    interaction = _interaction(database, 2)
    await modal.on_submit(interaction)
    assert interaction.response.messages[0]["content"] == tr("link_self_conflict", bga_account="Alice")
    assert database.get_linked_user("2") is None


async def test_linked_member_can_unlink_themselves(database):
    database.link_user("2", "222", "Bob")
    view = ManageOwnLinkView()
    interaction = _interaction(database, 2)
    unlink = next(item for item in view.children if item.label == tr("button_link_remove"))
    await unlink.callback(interaction)
    assert database.get_linked_user("2") is None
    assert interaction.response.edits == [{"content": tr("link_self_removed"), "view": None}]


def _texts(view: discord.ui.LayoutView) -> str:
    return "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def _stats_with_names(database):
    sub = database.upsert_watch_subscription(
        table_id="900",
        table_url="",
        base_url="",
        gameserver="",
        guild_id="10",
        channel_id="20",
        created_by_discord_user_id="30",
        game_name="azul",
    )
    database.update_watch_state(
        subscription_id=sub.subscription_id,
        last_packet_id=1,
        waiting_ids=[],
        player_names={},
        seated_player_names={},
        is_initialized=True,
        game_name="azul",
        winner_names=["SecretPlayer"],
        final_standings=["#1 SecretPlayer (5) • score 10"],
    )
    database.remove_watch_subscription(table_id="900", guild_id="10", channel_id="20", outcome="finished")


@pytest.mark.parametrize("page", ["overview", "leaders", "activity"])
def test_global_discord_stats_hide_players(database, page):
    from bga_turn.database import redact_global_stats

    _stats_with_names(database)
    cog = BgaCommands(database, bga_client=None, monitor=SimpleNamespace())
    stats = database.get_global_extended_stats()
    assert stats["top_players"]  # the raw data does name the player
    view = StatsLayoutView(cog=cog, guild=None, stats=redact_global_stats(stats), is_global=True, page=page)
    assert "SecretPlayer" not in _texts(view)
