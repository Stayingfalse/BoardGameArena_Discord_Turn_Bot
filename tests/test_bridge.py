from __future__ import annotations

import time
from types import SimpleNamespace

import aiohttp
import pytest
from test_codenames_monitor import CHANNEL, DISCORD, GUILD, ROOM, FakeChannel, _monitor, _show, _watch_and_link

from bga_turn.bridge.api import create_bridge_app
from bga_turn.bridge.service import TournamentBridge
from bga_turn.codenames.commands import CodenamesCommands

TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PRIMARY = "http://tournament:3000/api/bridge/codenames/result"
FALLBACK = "https://tournament.example.com/api/bridge/codenames/result"
PLAYERS = ["111", "222", "333", "444"]


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    async def text(self) -> str:
        return "body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Answers POSTs from a per-URL script: an int is a status, an exception is raised."""

    def __init__(self, script: dict[str, list]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, **kwargs) -> "FakeSession":
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, *, json, headers):
        self.calls.append((url, json, headers))
        outcome = self.script[url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(outcome)


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel(CHANNEL)


@pytest.fixture
def monitor(database, channel):
    return _monitor(database, channel)


def _bridge(database, monitor, session=None) -> TournamentBridge:
    return TournamentBridge(
        database,
        monitor,
        token=TOKEN,
        callback_urls=[PRIMARY, FALLBACK],
        session_factory=session or FakeSession({}),
    )


async def _play_full_game(monitor, database) -> None:
    for view in await _watch_and_link(monitor, database):
        await _show(monitor, view)


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------


async def test_a_finished_game_in_a_bound_channel_is_queued_with_the_result(database, monitor):
    bridge = _bridge(database, monitor)
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M2", players=PLAYERS)

    await _play_full_game(monitor, database)

    [entry] = database.list_bridge_results(str(CHANNEL))
    payload = entry.payload
    assert entry.status == "pending"
    assert payload["event"] == "codenames.game_finished"
    assert payload["external_ref"] == "R1M2"
    assert payload["room_url"] == f"https://codenames.game/r/{ROOM}"
    assert payload["game_number"] == 1
    # The recording ends with Alice (red) picking the assassin: blue wins.
    assert (payload["winner"], payload["assassin"]) == ("blue", True)
    assert payload["loser_remaining"] == payload["remaining"]["red"]
    seats = payload["seats"]
    assert [s["discord_user_id"] for s in seats["red"]["spymasters"]] == [DISCORD["1"]]
    assert [s["discord_user_id"] for s in seats["red"]["operatives"]] == [DISCORD["0"]]
    assert [s["discord_user_id"] for s in seats["blue"]["spymasters"]] == [DISCORD["3"]]
    assert [s["discord_user_id"] for s in seats["blue"]["operatives"]] == [DISCORD["4"]]
    # The whole clue history, with who gave and guessed what, for tournament stats.
    first, second = payload["turns"][:2]
    assert (first["team"], first["word"], first["number"]) == ("red", "CLUE 1", "4")
    assert first["giver"]["discord_user_id"] == DISCORD["1"]
    assert [(g["word"], g["discord_user_id"]) for g in first["guesses"]] == [
        ("BATTERY", DISCORD["0"]), ("LITTER", DISCORD["0"]), ("SEED", DISCORD["0"]), ("WING", DISCORD["0"])
    ]
    assert [g["color"] for g in first["guesses"]] == ["red", "red", "red", "neutral"]
    assert second["giver"]["discord_user_id"] == DISCORD["3"]
    assert payload["turns"][-1]["guesses"][0]["color"] == "black"
    assert len(payload["board"]) == 25 and payload["starting_team"] == "red"


async def test_a_replayed_final_state_is_queued_once(database, channel):
    first = _monitor(database, channel)
    bridge = _bridge(database, first)
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M2", players=PLAYERS)
    views = await _watch_and_link(first, database)
    for view in views:
        await _show(first, view)

    restarted = _monitor(database, channel)
    _bridge(database, restarted)
    await _show(restarted, views[-1])

    assert len(database.list_bridge_results(str(CHANNEL))) == 1


async def test_unbound_channels_report_nothing(database, monitor):
    _bridge(database, monitor)
    await _play_full_game(monitor, database)
    assert database.list_bridge_results(str(CHANNEL)) == []


async def test_binding_invites_the_players_to_rooms_already_watched(database, monitor):
    bridge = _bridge(database, monitor)
    watch, _ = await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M2", players=PLAYERS)
    assert sorted(database.list_codenames_watch_members(watch.watch_id)) == PLAYERS


async def test_unbinding_stops_watching_the_thread(database, monitor):
    bridge = _bridge(database, monitor)
    await monitor.watch_room(ROOM, guild_id=GUILD, channel_id=str(CHANNEL), created_by_discord_user_id="9")
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M2", players=PLAYERS)

    assert await bridge.unbind(str(CHANNEL))
    assert database.list_codenames_watches(ROOM) == []
    assert await bridge.binding_for(str(CHANNEL)) is None


async def test_a_room_posted_in_a_bound_thread_stays_there_and_invites_the_players(database, monitor):
    bridge = _bridge(database, monitor)
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M2", players=PLAYERS)
    # The server routes notifications elsewhere, but a match thread keeps its own card.
    database.upsert_guild_settings(GUILD, recruiting_only=False, delete_invite_message=False, forced_channel_id="777")
    cog = CodenamesCommands(database, monitor, bridge=bridge)

    async def add_reaction(emoji):
        pass

    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=999),
        guild=SimpleNamespace(id=int(GUILD), get_channel_or_thread=lambda _id: object()),
        channel=SimpleNamespace(id=CHANNEL),
        content=f"our room: https://codenames.game/r/{ROOM}",
        add_reaction=add_reaction,
    )
    await cog.on_message(message)

    [watch] = database.list_codenames_watches(ROOM)
    assert watch.channel_id == str(CHANNEL)
    assert sorted(database.list_codenames_watch_members(watch.watch_id)) == PLAYERS


# ----------------------------------------------------------------------
# Delivery
# ----------------------------------------------------------------------


async def _queued(database, monitor, session) -> TournamentBridge:
    bridge = _bridge(database, monitor, session)
    await bridge.bind(str(CHANNEL), guild_id=GUILD, external_ref="R1M2", players=PLAYERS)
    await _play_full_game(monitor, database)
    return bridge


async def test_delivery_uses_the_internal_url_with_the_token(database, monitor):
    session = FakeSession({PRIMARY: [200]})
    bridge = await _queued(database, monitor, session)

    await bridge.deliver_due()

    [(url, body, headers)] = session.calls
    assert url == PRIMARY
    assert headers["Authorization"] == AUTH["Authorization"]
    assert headers["Idempotency-Key"] == body["event_id"]
    [entry] = database.list_bridge_results(str(CHANNEL))
    assert (entry.status, entry.attempts) == ("delivered", 1)


async def test_delivery_falls_back_to_the_public_url(database, monitor):
    session = FakeSession({PRIMARY: [aiohttp.ClientConnectionError("refused")], FALLBACK: [200]})
    bridge = await _queued(database, monitor, session)

    await bridge.deliver_due()

    assert [call[0] for call in session.calls] == [PRIMARY, FALLBACK]
    [entry] = database.list_bridge_results(str(CHANNEL))
    assert entry.status == "delivered"


async def test_a_refused_result_is_not_retried(database, monitor):
    session = FakeSession({PRIMARY: [409]})
    bridge = await _queued(database, monitor, session)

    await bridge.deliver_due()

    [entry] = database.list_bridge_results(str(CHANNEL))
    assert entry.status == "rejected"
    assert "409" in entry.last_error


async def test_an_outage_is_retried_later(database, monitor):
    session = FakeSession({PRIMARY: [503, 200], FALLBACK: [aiohttp.ClientConnectionError("down")]})
    bridge = await _queued(database, monitor, session)

    await bridge.deliver_due()
    [entry] = database.list_bridge_results(str(CHANNEL))
    assert (entry.status, entry.attempts) == ("pending", 1)
    assert entry.next_attempt_at > time.time()

    await bridge.deliver_due()  # not due yet: nothing sent
    assert len(session.calls) == 2

    database.update_bridge_result(entry.outbox_id, status="pending", attempts=1, next_attempt_at=0)
    await bridge.deliver_due()
    [entry] = database.list_bridge_results(str(CHANNEL))
    assert (entry.status, entry.attempts) == ("delivered", 2)


# ----------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------


@pytest.fixture
async def api(aiohttp_client, database, monitor):
    bridge = _bridge(database, monitor)
    return await aiohttp_client(create_bridge_app(bridge, token=TOKEN))


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}])
async def test_api_needs_the_token(api, headers):
    response = await api.get("/api/bridge/health", headers=headers)
    assert response.status == 401


async def test_api_binds_reads_and_unbinds_a_thread(api, database):
    path = f"/api/bridge/codenames/bindings/{CHANNEL}"
    response = await api.put(path, json={"guild_id": GUILD, "external_ref": "R1M2", "players": PLAYERS}, headers=AUTH)
    assert response.status == 200
    assert (await response.json())["players"] == PLAYERS

    response = await api.get(path, headers=AUTH)
    body = await response.json()
    assert (body["external_ref"], body["rooms"], body["results"]) == ("R1M2", [], [])

    response = await api.delete(path, headers=AUTH)
    assert (await response.json()) == {"removed": True}
    assert (await api.get(path, headers=AUTH)).status == 404


@pytest.mark.parametrize(
    "body",
    [
        {"guild_id": "x", "external_ref": "R1M2", "players": PLAYERS},
        {"guild_id": GUILD, "external_ref": "", "players": PLAYERS},
        {"guild_id": GUILD, "external_ref": "R1M2", "players": ["not-an-id"]},
        {"guild_id": GUILD, "external_ref": "R1M2", "players": [str(i) for i in range(11)]},
        ["not", "an", "object"],
    ],
)
async def test_api_rejects_bad_bindings(api, body):
    response = await api.put(f"/api/bridge/codenames/bindings/{CHANNEL}", json=body, headers=AUTH)
    assert response.status == 400
