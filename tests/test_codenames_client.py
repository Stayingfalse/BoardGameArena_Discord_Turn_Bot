from __future__ import annotations

from test_codenames_state import _events

from bga_turn.codenames.client import (
    CLOSE_UNSUPPORTED,
    NAMESPACE,
    CodenamesRoomClient,
)

SHARD = "https://server-discord.gke.codenames.game/0"


class FakeSio:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, tuple]] = []

    async def emit(self, event, args, namespace=None):
        assert namespace == NAMESPACE
        self.emitted.append((event, args))


def _client(views: list) -> CodenamesRoomClient:
    async def on_view(view):
        views.append(view)

    client = CodenamesRoomClient("test-room", credentials="test-creds-bot", bot_name="🤖 Curious Cat", on_view=on_view)
    client._sio = FakeSio()
    client._server_url = SHARD
    return client


async def test_connecting_asks_the_server_for_the_rooms_current_match():
    client = _client([])
    await client._on_connect()
    [(event, args)] = client._sio.emitted
    assert event == "sync"
    assert args[:3] == ("default", None, "test-creds-bot")
    assert args[3]["roomID"] == "test-room"
    assert args[3]["playerName"] == "🤖 Curious Cat"
    assert args[3]["credentials"] == "test-creds-bot"


async def test_the_lobby_redirect_moves_to_the_rooms_game_server():
    client = _client([])
    client._server_url = "https://server-discord.gke.codenames.game"
    await client._handle_event("updateServerURL", (SHARD,))
    assert client._move_to == SHARD
    assert client._connection_ended.is_set()


async def test_created_match_on_another_server_redirects_instead_of_syncing():
    client = _client([])
    await client._handle_event("createdMatch", ("test-room", "m1", "2", "test-creds-bot", SHARD + "-other"))
    assert client._move_to == SHARD + "-other"
    assert client._sio.emitted == []


async def test_replaying_a_recorded_game_reports_every_change():
    views: list = []
    client = _client(views)
    for event, args in _events():
        if event != "updateServerURL":  # we are already on the room's server
            await client._handle_event(event, tuple(args))
    # After createdMatch the client asks for the match's state with its seat.
    assert client._sio.emitted[0] == ("sync", ("Y1L4zY59RZO", "2", "test-creds-bot", client._sio.emitted[0][1][3]))
    assert client.player_id == "2"
    assert client._joined.is_set()
    assert views[0].phase == "lobby"
    assert views[-1].phase == "finished" and views[-1].winner_team == "blue"
    # Every patch applied cleanly: no resync was needed.
    assert len(client._sio.emitted) == 1


async def test_an_out_of_step_patch_triggers_a_resync():
    views: list = []
    client = _client(views)
    await client._handle_event("createdMatch", ("test-room", "m1", "2", "test-creds-bot", SHARD))
    await client._handle_event("sync", ("m1", {"state": {"_stateID": 3, "G": {}, "ctx": {}}}))
    client._sio.emitted.clear()
    await client._handle_event("patch", ("m1", 7, 8, [{"op": "replace", "path": "/_stateID", "value": 8}], [], 0))
    assert client._sio.emitted == [("sync", ("m1", "2", "test-creds-bot", client._sio.emitted[0][1][3]))]
    assert len(views) == 1  # only the sync produced a view


async def test_events_for_another_match_are_ignored():
    views: list = []
    client = _client(views)
    await client._handle_event("createdMatch", ("test-room", "m1", "2", "test-creds-bot", SHARD))
    await client._handle_event("sync", ("old-match", {"state": {"_stateID": 1}}))
    await client._handle_event("matchData", ("old-match", [{"id": 0, "name": "Alice"}]))
    assert views == []


async def test_play_again_starts_a_new_match_tracker():
    views: list = []
    client = _client(views)
    await client._handle_event("createdMatch", ("test-room", "m1", "2", "test-creds-bot", SHARD))
    await client._handle_event("sync", ("m1", {"state": {"_stateID": 40, "G": {"gameover": {"team": "red"}}}}))
    client._sio.emitted.clear()

    await client._handle_event("playNextMatch", ("m1", "m2"))
    assert client._sio.emitted[0][1][:2] == ("default", None)
    await client._handle_event("createdMatch", ("test-room", "m2", "2", "test-creds-bot", SHARD))
    await client._handle_event("sync", ("m2", {"state": {"_stateID": 0, "G": {}, "ctx": {"phase": "lobby"}}}))
    assert [(v.match_id, v.phase) for v in views] == [("m1", "finished"), ("m2", "lobby")]


async def test_room_timeout_is_a_pause_not_the_end_of_the_room():
    client = _client([])
    await client._handle_event("room-timeout", ({"matchID": "m1"},))
    assert client._close_reason is None and not client._stopping.is_set()
    assert client._connection_ended.is_set() and client._room_timed_out


async def test_after_a_room_timeout_the_client_rejoins_through_the_lobby():
    closed: list[str] = []

    async def on_closed(reason):
        closed.append(reason)

    async def on_view(view):
        pass

    client = CodenamesRoomClient(
        "test-room", credentials="c", bot_name="🤖 Curious Cat", on_view=on_view, on_closed=on_closed,
        room_timeout_delay=0.01,
    )
    servers: list[str] = []

    async def fake_connection():
        servers.append(client._server_url)
        if len(servers) == 1:
            client._server_url = SHARD  # we were on the room's server
            client._room_timed_out = True  # ...when it went idle
            return True
        client._close("stopped")
        return True

    client._run_connection = fake_connection
    await client.run()
    assert len(servers) == 2  # it came back instead of giving up
    assert servers[1] == client.lobby_url
    assert closed == ["stopped"]


async def test_duet_rooms_close_the_connection_for_good():
    duet = _client([])
    await duet._handle_event("wrongNamespace", ("cnduet",))
    assert duet._close_reason == CLOSE_UNSUPPORTED and duet._stopping.is_set()


async def test_a_sync_error_drops_the_connection_to_retry():
    client = _client([])
    await client._handle_event("sync-error", ({"matchID": "default", "error": "404"},))
    assert client._connection_ended.is_set()
    assert client._close_reason is None


async def test_errors_in_a_handler_are_logged_not_raised(caplog):
    async def broken_view(view):
        raise RuntimeError("card update failed")

    client = CodenamesRoomClient("test-room", credentials="c", bot_name="🤖 Curious Cat", on_view=broken_view)
    client._sio = FakeSio()
    client._server_url = SHARD
    await client._on_event("createdMatch", "test-room", "m1", "2", "c", SHARD)
    await client._on_event("sync", "m1", {"state": {"_stateID": 1}})
    assert "card update failed" in caplog.text
    assert client._close_reason is None
