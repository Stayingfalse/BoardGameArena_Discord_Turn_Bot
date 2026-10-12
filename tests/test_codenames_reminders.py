from __future__ import annotations

import time

import pytest
from test_codenames_monitor import (
    CHANNEL,
    GUILD,
    ROOM,
    FakeBot,
    FakeChannel,
    FakeClient,
    PassThroughLimiter,
    _ping_users,
    _show,
    _watch_and_link,
)

from bga_turn.bridge.service import TournamentBridge
from bga_turn.codenames import cards
from bga_turn.codenames.monitor import CodenamesMonitor

T0 = 1_800_000_000
HOUR = 3600


class Clock:
    def __init__(self) -> None:
        self.now = float(T0)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel(CHANNEL)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def monitor(database, channel, clock) -> CodenamesMonitor:
    FakeClient.instances.clear()
    return CodenamesMonitor(
        FakeBot(channel),
        database,
        update_delay=0,
        client_factory=FakeClient,
        rate_limiter=PassThroughLimiter(),
        clock=clock,
    )


async def _red_spymaster_turn(monitor, database):
    views = await _watch_and_link(monitor, database)
    view = next(v for v in views if v.turn_key == "red:spymasters")
    await _show(monitor, view)
    return view


def _turn_pings(channel):
    return [p for p in channel.pings if "line-up" not in p.content]


async def test_a_waiting_turn_is_pinged_again_after_a_day(monitor, database, channel):
    await _red_spymaster_turn(monitor, database)
    [first] = _turn_pings(channel)

    assert await monitor.send_due_reminders(now=T0 + 23 * HOUR) == 0
    assert await monitor.send_due_reminders(now=T0 + 25 * HOUR) == 1
    first_reminder = _turn_pings(channel)[-1]
    assert first.deleted and not first_reminder.deleted
    assert "Still waiting" in first_reminder.content
    assert "25 hours" in first_reminder.content
    assert _ping_users(first_reminder) == ["111"]  # Bob, red spymaster

    # The next one is a day after the reminder, not after the turn started.
    assert await monitor.send_due_reminders(now=T0 + 30 * HOUR) == 0
    assert await monitor.send_due_reminders(now=T0 + 50 * HOUR) == 1
    assert "2 days" in _turn_pings(channel)[-1].content


async def test_a_new_turn_restarts_the_clock(monitor, database, channel, clock):
    views = await _watch_and_link(monitor, database)
    red = next(v for v in views if v.turn_key == "red:spymasters")
    await _show(monitor, red)
    clock.now = T0 + 20 * HOUR
    await _show(monitor, next(v for v in views if v.turn_key == "red:operatives"))

    assert await monitor.send_due_reminders(now=T0 + 30 * HOUR) == 0
    assert await monitor.send_due_reminders(now=T0 + 45 * HOUR) == 1


async def test_reminders_speed_up_near_a_tournament_deadline(monitor, database, channel):
    deadline = T0 + 30 * HOUR
    database.upsert_codenames_binding(
        str(CHANNEL), guild_id=GUILD, external_ref="R1M1", players=[], deadline=deadline
    )
    await _red_spymaster_turn(monitor, database)

    assert await monitor.send_due_reminders(now=T0 + 5 * HOUR) == 0
    assert await monitor.send_due_reminders(now=T0 + 7 * HOUR) == 1
    assert f"<t:{deadline}:R>" in _turn_pings(channel)[-1].content
    assert await monitor.send_due_reminders(now=T0 + 12 * HOUR) == 0
    assert await monitor.send_due_reminders(now=T0 + 13 * HOUR) == 1


async def test_turns_from_before_reminders_start_counting_when_first_seen(monitor, database, channel):
    await _red_spymaster_turn(monitor, database)
    [watch] = database.list_codenames_watches(ROOM)
    game = database.get_latest_codenames_game(watch.watch_id)
    state = dict(game.state)
    del state["turn_started_at"]
    database.update_codenames_game(game.game_id, state=state)

    assert await monitor.send_due_reminders(now=T0 + 100 * HOUR) == 0
    assert await monitor.send_due_reminders(now=T0 + 125 * HOUR) == 1


async def test_reminders_can_be_turned_off(monitor, database):
    monitor.reminder_seconds = 0
    await _red_spymaster_turn(monitor, database)
    assert await monitor.send_due_reminders(now=T0 + 1000 * HOUR) == 0


def test_waits_are_written_in_hours_then_days():
    assert cards.format_wait(30 * 60) == "an hour"
    assert cards.format_wait(5 * HOUR) == "5 hours"
    assert cards.format_wait(47 * HOUR) == "47 hours"
    assert cards.format_wait(75 * HOUR) == "3 days"


async def test_tournament_rooms_are_never_dropped_for_being_idle(monitor, database, channel):
    views = await _watch_and_link(monitor, database)
    database.upsert_codenames_binding(str(CHANNEL), guild_id=GUILD, external_ref="R1M1", players=[])
    monitor.idle_seconds = 0
    monitor._last_active[ROOM] = time.monotonic() - 10
    await _show(monitor, views[0])
    assert database.list_codenames_watches(ROOM) != []

    database.remove_codenames_binding(str(CHANNEL))
    monitor._last_active[ROOM] = time.monotonic() - 10
    await _show(monitor, views[0])
    assert database.list_codenames_watches(ROOM) == []


async def test_the_binding_reports_whose_turn_it_is(monitor, database):
    bridge = TournamentBridge(database, monitor, token="t", callback_urls=[])
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M1", players=[])
    await _red_spymaster_turn(monitor, database)

    status = await bridge.describe(await bridge.binding_for(str(CHANNEL)))
    [room] = status["rooms"]
    assert room["phase"] == "in_progress"
    assert (room["active_team"], room["active_role"]) == ("red", "spymasters")
    assert room["waiting_on"] == [{"name": "Bob", "discord_user_id": "111"}]
    assert room["turn_started_at"].startswith("2027-01-15T08:00:00")
    assert room["game_number"] == 1
