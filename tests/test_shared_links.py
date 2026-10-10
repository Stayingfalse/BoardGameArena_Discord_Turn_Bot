"""The on_message flow: status note first, then the card or an explanation."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest
from conftest import GUILD_A_ID, make_text_channel

from bga_turn.bga_client import (
    BgaClientError,
    BgaNotPublicError,
    BgaRateLimitError,
    BgaServerError,
    BgaTableFinishedError,
    BgaTableUnavailableError,
)
from bga_turn.commands_bga import BgaCommands
from bga_turn.i18n import tr
from bga_turn.monitor import ActiveTableMessage, BgaMonitor

TABLE_ID = "891538070"


class FakeMessage:
    _next_id = 5000

    def __init__(self, channel, *, content: str = "", author_id: int = 200) -> None:
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.channel = channel
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=False)
        self.guild = SimpleNamespace(id=GUILD_A_ID)
        self.created_at = discord.utils.utcnow()
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class FakeMonitor:
    """Records what the cog asks the real monitor to do."""

    LINK_STATUS_WAITING_AFTER_SECONDS = 0.0
    _default_forced_channel_id = None
    _default_recruiting_only = False

    def __init__(self, *, adopt_outcome: str = "adopted", card_shown: bool = True) -> None:
        self.adopt_outcome = adopt_outcome
        self.card_shown = card_shown
        self.sent: list[str] = []
        self.edits: list[dict] = []
        self.notes: list[FakeMessage] = []
        self.deleted: list[FakeMessage] = []
        self.waiting_marked = 0
        self.redrawn: list[int] = []

    async def send_link_status(self, channel, *, table_id, text, delete_after=None):
        self.sent.append(text)
        note = FakeMessage(channel)
        self.notes.append(note)
        return note

    async def edit_link_status(self, message, **kwargs):
        self.edits.append(kwargs)
        return True

    async def adopt_link_status_message(self, subscription, message):
        return self.adopt_outcome

    async def redraw_card_from_saved_state(self, subscription_id):
        self.redrawn.append(subscription_id)

    async def reset_tracked_message(self, subscription):
        return True

    async def refresh_now(self):
        pass

    async def wait_for_active_messages(self, ids, *, timeout_seconds):
        return self.card_shown

    async def mark_link_status_waiting(self, subscription):
        self.waiting_marked += 1

    async def delete_discord_message(self, message, *, operation):
        self.deleted.append(message)
        return True


def _cog(database, monitor, *, register):
    cog = BgaCommands(database, bga_client=None, monitor=monitor)
    cog._register_watch = register
    return cog


def _registration(*, replaced=False):
    subscription = SimpleNamespace(
        subscription_id=1,
        table_id=TABLE_ID,
        guild_id=str(GUILD_A_ID),
        channel_id="1001",
        game_name="azul",
        table_url=f"https://boardgamearena.com/1/azul?table={TABLE_ID}",
    )
    return SimpleNamespace(subscription=subscription, replaced_existing_watch=replaced)


@pytest.fixture
def channel(guilds):
    return guilds[0].text_channels[0]


def _set_delete(database, enabled: bool) -> None:
    database.upsert_guild_settings(
        str(GUILD_A_ID), recruiting_only=False, delete_invite_message=enabled, forced_channel_id=None
    )


async def test_note_is_posted_then_adopted_and_link_deleted(database, channel):
    _set_delete(database, True)
    monitor = FakeMonitor()

    async def register(**kwargs):
        assert monitor.sent == [tr("link_status_detecting", table_id=TABLE_ID)]  # note first
        return _registration()

    message = FakeMessage(channel, content=f"anyone? ||https://boardgamearena.com/table?table={TABLE_ID}||")
    await _cog(database, monitor, register=register).on_message(message)
    assert monitor.edits == []  # the monitor turns the note into the card
    assert monitor.deleted == [message]


async def test_link_kept_when_tidy_setting_is_off(database, channel):
    _set_delete(database, False)
    monitor = FakeMonitor()

    async def register(**kwargs):
        return _registration()

    message = FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    await _cog(database, monitor, register=register).on_message(message)
    assert monitor.deleted == []


@pytest.mark.parametrize(
    ("error", "reason_key"),
    [
        (ValueError("bad"), "link_error_invalid"),
        (BgaTableFinishedError("x"), "link_error_finished"),
        (BgaTableUnavailableError("x"), "link_error_unavailable"),
        (BgaNotPublicError("x"), "link_error_not_public"),
        (BgaRateLimitError("x", retry_after=30), "link_error_rate_limited"),
        (BgaServerError("x", status_code=503), "link_error_bga_down"),
        (BgaClientError("Table not found"), "link_error_bga"),
        (RuntimeError("boom"), "link_error_unexpected"),
    ],
)
async def test_failures_update_the_note_and_keep_the_link(database, channel, error, reason_key):
    _set_delete(database, True)
    monitor = FakeMonitor()

    async def register(**kwargs):
        raise error

    message = FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    await _cog(database, monitor, register=register).on_message(message)

    assert len(monitor.edits) == 1
    edit = monitor.edits[0]
    assert edit["color"] == discord.Color.red()
    expected_reason = BgaCommands._describe_link_error(error)
    assert expected_reason == tr(reason_key, status_code=503, detail="Table not found")
    assert edit["text"] == tr("link_status_failed", table_id=TABLE_ID, reason=expected_reason)
    assert monitor.deleted == []


async def test_forced_channel_note_points_there(database, channel):
    monitor = FakeMonitor(adopt_outcome="other_channel")
    database.upsert_guild_settings(
        str(GUILD_A_ID), recruiting_only=False, delete_invite_message=False, forced_channel_id="1002"
    )

    async def register(**kwargs):
        return _registration()

    await _cog(database, monitor, register=register).on_message(
        FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    )
    assert "<#1002>" in monitor.edits[0]["text"]
    assert monitor.edits[0]["delete_after"]


async def test_slow_bga_marks_note_waiting_and_keeps_link(database, channel):
    _set_delete(database, True)
    monitor = FakeMonitor(card_shown=False)

    async def register(**kwargs):
        return _registration()

    message = FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    cog = _cog(database, monitor, register=register)
    cog._CARD_WAIT_EXTRA_SECONDS = 0
    await cog.on_message(message)
    assert monitor.waiting_marked == 1
    assert monitor.deleted == []


async def test_reposted_link_redraws_card_immediately(database, channel):
    monitor = FakeMonitor()

    async def register(**kwargs):
        return _registration(replaced=True)

    await _cog(database, monitor, register=register).on_message(
        FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    )
    assert monitor.redrawn == [1]


async def test_forwarded_message_links_are_detected(database, channel):
    monitor = FakeMonitor()

    async def register(**kwargs):
        return _registration()

    message = FakeMessage(channel, content="")
    message.message_snapshots = [SimpleNamespace(content=f"https://bga.li/t/{TABLE_ID}")]
    await _cog(database, monitor, register=register).on_message(message)
    assert monitor.sent == [tr("link_status_detecting", table_id=TABLE_ID)]


# ---------------------------------------------------------------------------
# Real monitor: handing the note over to the table card
# ---------------------------------------------------------------------------


def _subscription(database, channel_id="1001"):
    return database.upsert_watch_subscription(
        table_id=TABLE_ID,
        table_url=f"https://boardgamearena.com/table?table={TABLE_ID}",
        base_url="https://boardgamearena.com",
        gameserver="",
        guild_id=str(GUILD_A_ID),
        channel_id=channel_id,
        created_by_discord_user_id="200",
        game_name="azul",
    )


async def test_adopt_replaces_older_card(database, bot, channel):
    monitor = BgaMonitor(bot, database, bga_client=None, poll_seconds=15)
    subscription = _subscription(database)
    old_card = FakeMessage(channel)
    monitor._active_messages[subscription.subscription_id] = ActiveTableMessage(old_card, "recruiting", [])
    note = FakeMessage(channel)

    assert await monitor.adopt_link_status_message(subscription, note) == "adopted"
    assert old_card.deleted and not note.deleted
    assert monitor._active_messages[subscription.subscription_id].message is note
    assert database.get_watch_subscription(subscription.subscription_id).tracked_message_id == note.id
    # The card is not "shown" until the note has been turned into it.
    assert not await monitor.wait_for_active_messages([subscription.subscription_id], timeout_seconds=0)


async def test_adopt_keeps_a_card_posted_after_the_note(database, bot, channel):
    monitor = BgaMonitor(bot, database, bga_client=None, poll_seconds=15)
    subscription = _subscription(database)
    note = FakeMessage(channel)
    fresh_card = FakeMessage(channel)  # created after the note
    monitor._active_messages[subscription.subscription_id] = ActiveTableMessage(fresh_card, "recruiting", [])

    assert await monitor.adopt_link_status_message(subscription, note) == "card_posted"
    assert note.deleted and not fresh_card.deleted


async def test_adopt_refuses_when_table_posts_elsewhere(database, bot, guilds, channel):
    guilds[0].text_channels.append(make_text_channel(1002, "games", guilds[0]))
    database.upsert_guild_settings(
        str(GUILD_A_ID), recruiting_only=False, delete_invite_message=False, forced_channel_id="1002"
    )
    monitor = BgaMonitor(bot, database, bga_client=None, poll_seconds=15)
    subscription = _subscription(database)
    assert await monitor.adopt_link_status_message(subscription, FakeMessage(channel)) == "other_channel"


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------


def _edit(message, *, before: str | None):
    cached = None
    if before is not None:
        cached = FakeMessage(message.channel, content=before)
        cached.id = message.id
    return SimpleNamespace(message=message, cached_message=cached)


def _counting_register(calls):
    async def register(**kwargs):
        calls.append(kwargs["table_or_url"])
        return _registration()

    return register


async def test_link_added_by_edit_is_registered(database, channel):
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    message = FakeMessage(channel, content="who's up for a game?")
    await cog.on_message(message)
    assert calls == []

    message.content = f"who's up for a game? https://boardgamearena.com/table?table={TABLE_ID}"
    await cog.on_raw_message_edit(_edit(message, before="who's up for a game?"))
    assert calls == [f"https://boardgamearena.com/table?table={TABLE_ID}"]
    assert monitor.sent == [tr("link_status_detecting", table_id=TABLE_ID)]


async def test_edit_without_new_links_does_nothing(database, channel):
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    url = f"https://boardgamearena.com/table?table={TABLE_ID}"
    message = FakeMessage(channel, content=url)
    await cog.on_message(message)
    assert len(calls) == 1

    # Typo fix, and Discord unfurling the link preview (also an edit event).
    message.content = f"{url} tonight!"
    await cog.on_raw_message_edit(_edit(message, before=url))
    await cog.on_raw_message_edit(_edit(message, before=None))
    assert len(calls) == 1
    assert len(monitor.sent) == 1


async def test_failed_link_is_not_retried_on_every_edit(database, channel):
    monitor, attempts = FakeMonitor(), []

    async def register(**kwargs):
        attempts.append(1)
        raise BgaTableFinishedError("x")

    cog = _cog(database, monitor, register=register)
    message = FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    await cog.on_message(message)
    await cog.on_raw_message_edit(_edit(message, before=None))
    assert len(attempts) == 1
    assert len(monitor.edits) == 1


async def test_edit_after_restart_skips_tables_already_watched_here(database, channel):
    _subscription(database)  # this channel already watches the table
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    message = FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    # Nothing cached, no on_message seen (e.g. the bot restarted since posting).
    await cog.on_raw_message_edit(_edit(message, before=None))
    assert calls == []


async def test_edit_only_registers_the_new_link(database, channel):
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    first = "https://boardgamearena.com/table?table=1111111"
    message = FakeMessage(channel, content=first)
    await cog.on_message(message)

    message.content = f"{first} and https://boardgamearena.com/2/azul?table=2222222"
    await cog.on_raw_message_edit(_edit(message, before=first))
    assert calls == [first, "https://boardgamearena.com/2/azul?table=2222222"]


async def test_edits_by_bots_are_ignored(database, channel):
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    message = FakeMessage(channel, content=f"https://boardgamearena.com/table?table={TABLE_ID}")
    message.author.bot = True
    await cog.on_raw_message_edit(_edit(message, before=""))
    assert calls == []


def _limit_note() -> str:
    return tr(
        "link_limit_note",
        per_message=BgaCommands._MAX_LINKS_PER_MESSAGE,
        per_user=BgaCommands._MAX_LINKS_PER_USER,
        minutes=int(BgaCommands._USER_LINK_WINDOW_SECONDS // 60),
    )


async def test_links_per_message_are_capped(database, channel):
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    content = " ".join(f"https://boardgamearena.com/table?table={1000 + n}" for n in range(7))
    await cog.on_message(FakeMessage(channel, content=content))
    assert len(calls) == BgaCommands._MAX_LINKS_PER_MESSAGE
    assert monitor.sent.count(_limit_note()) == 1


async def test_links_per_person_are_capped_and_noted_once(database, channel):
    monitor, calls = FakeMonitor(), []
    cog = _cog(database, monitor, register=_counting_register(calls))
    for n in range(15):
        await cog.on_message(FakeMessage(channel, content=f"https://boardgamearena.com/table?table={2000 + n}"))
    assert len(calls) == BgaCommands._MAX_LINKS_PER_USER
    assert monitor.sent.count(_limit_note()) == 1
    # Someone else still has their own allowance.
    await cog.on_message(FakeMessage(channel, content="https://boardgamearena.com/table?table=3000", author_id=201))
    assert len(calls) == BgaCommands._MAX_LINKS_PER_USER + 1
