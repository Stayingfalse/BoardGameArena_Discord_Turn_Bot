from __future__ import annotations

import sqlite3
from importlib.resources import files

import pytest

from bga_turn.codenames.store import CodenamesSeatTakenError
from bga_turn.database import Database

GUILD = "500"
CHANNEL = "600"
CREATOR = "700"


def _room(database: Database, room_id: str = "juzaf-hitos"):
    return database.get_or_create_codenames_room(room_id, bot_credentials="aaa-bbb-ccc", bot_name="🤖 Curious Cat")


def _watch(database: Database, room_id: str = "juzaf-hitos", channel_id: str = CHANNEL):
    _room(database, room_id)
    watch, _ = database.add_codenames_watch(
        room_id, guild_id=GUILD, channel_id=channel_id, created_by_discord_user_id=CREATOR
    )
    return watch


def _history_rows(database: Database) -> list[sqlite3.Row]:
    return database._connection.execute("SELECT * FROM game_history ORDER BY history_id").fetchall()


def test_upgrading_a_bga_database_adds_provider_and_keeps_history(tmp_path):
    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    # game_history as it was before providers existed.
    connection.executescript(
        """
        CREATE TABLE game_history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_id TEXT NOT NULL,
            game_name TEXT,
            guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            created_by_discord_user_id TEXT NOT NULL,
            recruiting_started_at TEXT NOT NULL,
            game_started_at TEXT,
            finished_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('finished', 'cancelled', 'unwatched')),
            winner_names TEXT NOT NULL DEFAULT '[]',
            final_standings TEXT NOT NULL DEFAULT '[]',
            player_count INTEGER
        );
        INSERT INTO game_history (table_id, game_name, guild_id, channel_id, created_by_discord_user_id,
            recruiting_started_at, finished_at, outcome, winner_names)
        VALUES ('123456', 'azul', '500', '600', '700', '2026-01-01T00:00:00+00:00',
            '2026-01-02T00:00:00+00:00', 'finished', '["Alice"]');
        """
    )
    connection.commit()
    connection.close()

    database = Database(db_path=db_path, schema_sql=files("bga_turn").joinpath("schema.sql").read_text("utf-8"))
    database.initialize()
    try:
        [entry] = database.list_game_history_for_guild("500")
        assert (entry.table_id, entry.winner_names, entry.provider) == ("123456", ["Alice"], "bga")
        assert database._table_exists("codenames_rooms")
        assert database._table_exists("codenames_games")
    finally:
        database.close()


def test_room_identity_is_kept_after_creation(database):
    first = _room(database)
    again = database.get_or_create_codenames_room("juzaf-hitos", bot_credentials="other", bot_name="🤖 Silent Butler")
    assert (again.bot_credentials, again.bot_name) == (first.bot_credentials, first.bot_name)


def test_new_match_ids_become_the_next_game_number(database):
    _room(database)
    assert database.record_codenames_match("juzaf-hitos", "Y1L4zY59RZO", "2") == (1, True)
    # A resync of the same match (e.g. after a restart) is not a new game.
    assert database.record_codenames_match("juzaf-hitos", "Y1L4zY59RZO", "2") == (1, False)
    assert database.record_codenames_match("juzaf-hitos", "QuVMIPSqNpU", None) == (2, True)
    room = database.get_codenames_room("juzaf-hitos")
    assert (room.current_match_id, room.game_count, room.bot_player_id) == ("QuVMIPSqNpU", 2, "2")


def test_watching_the_same_room_twice_in_a_channel_is_one_watch(database):
    first = _watch(database)
    again, created = database.add_codenames_watch(
        "juzaf-hitos", guild_id=GUILD, channel_id=CHANNEL, created_by_discord_user_id="999"
    )
    assert not created
    assert again.watch_id == first.watch_id
    assert [w.watch_id for w in database.list_codenames_watches("juzaf-hitos")] == [first.watch_id]


def test_each_match_gets_its_own_game_and_the_latest_is_current(database):
    watch = _watch(database)
    game1, created = database.get_or_create_codenames_game(watch.watch_id, "Y1L4zY59RZO", 1)
    assert created and game1.lifecycle_state == "lobby"
    again, created = database.get_or_create_codenames_game(watch.watch_id, "Y1L4zY59RZO", 1)
    assert not created and again.game_id == game1.game_id
    game2, _ = database.get_or_create_codenames_game(watch.watch_id, "QuVMIPSqNpU", 2)
    assert database.get_latest_codenames_game(watch.watch_id).game_id == game2.game_id


def test_updating_a_game_changes_only_the_given_fields(database):
    watch = _watch(database)
    game, _ = database.get_or_create_codenames_game(watch.watch_id, "m1", 1)
    database.update_codenames_game(game.game_id, message_id=42, state={"activeTeam": "red"})
    database.update_codenames_game(game.game_id, lifecycle_state="in_progress", turn_key="red:spymasters")
    stored = database.get_codenames_game(game.game_id)
    assert stored.message_id == 42
    assert stored.state == {"activeTeam": "red"}
    assert (stored.lifecycle_state, stored.turn_key) == ("in_progress", "red:spymasters")
    with pytest.raises(ValueError):
        database.update_codenames_game(game.game_id, lifecycle_state="finished")


def test_finishing_a_game_records_codenames_history_once(database):
    watch = _watch(database)
    game, _ = database.get_or_create_codenames_game(watch.watch_id, "m1", 1)
    database.update_codenames_game(game.game_id, lifecycle_state="in_progress", started_at="2026-10-11T20:40:00+00:00")

    assert database.finish_codenames_game(game.game_id, winner_team="blue", finish_reason="assassin", player_count=4)
    assert not database.finish_codenames_game(game.game_id, winner_team="blue", finish_reason="assassin")

    stored = database.get_codenames_game(game.game_id)
    assert (stored.lifecycle_state, stored.winner_team, stored.finish_reason) == ("finished", "blue", "assassin")
    [entry] = database.list_game_history_for_guild(GUILD)
    assert (entry.provider, entry.table_id, entry.game_name) == ("codenames", "juzaf-hitos", "Codenames")
    assert (entry.outcome, entry.winner_names, entry.player_count) == ("finished", ["blue"], 4)
    assert entry.game_started_at == "2026-10-11T20:40:00+00:00"


def test_unwatching_archives_a_game_in_progress_but_not_a_lobby(database):
    in_progress = _watch(database, "room-one")
    game, _ = database.get_or_create_codenames_game(in_progress.watch_id, "m1", 1)
    database.update_codenames_game(game.game_id, lifecycle_state="in_progress")
    database.add_codenames_watch_members(in_progress.watch_id, ["111"])
    lobby = _watch(database, "room-two")
    database.get_or_create_codenames_game(lobby.watch_id, "m2", 1)

    assert database.remove_codenames_watch(in_progress.watch_id)
    assert database.remove_codenames_watch(lobby.watch_id)
    assert not database.remove_codenames_watch(lobby.watch_id)

    rows = _history_rows(database)
    assert [(row["table_id"], row["outcome"], row["winner_names"]) for row in rows] == [("room-one", "unwatched", "[]")]
    assert database.get_codenames_game(game.game_id) is None
    assert database.list_codenames_watch_members(in_progress.watch_id) == []


def test_removing_a_guild_drops_its_codenames_watches(database):
    _watch(database, "room-one")
    _watch(database, "room-two")
    assert database.remove_all_codenames_watches_for_guild(GUILD) == 2
    assert database.list_codenames_watches_for_guild(GUILD) == []


def test_tagged_members_are_listed_once(database):
    watch = _watch(database)
    database.add_codenames_watch_members(watch.watch_id, ["111", "222"])
    database.add_codenames_watch_members(watch.watch_id, ["111"])
    assert sorted(database.list_codenames_watch_members(watch.watch_id)) == ["111", "222"]
    assert database.remove_codenames_watch_member(watch.watch_id, "111")
    assert database.list_codenames_watch_members(watch.watch_id) == ["222"]


def test_a_seat_keeps_its_discord_link_when_the_player_renames(database):
    _room(database)
    database.update_codenames_player_names("juzaf-hitos", {"0": "Potato", "1": "Code Bot"})
    database.claim_codenames_player("juzaf-hitos", "0", "111")
    database.update_codenames_player_names("juzaf-hitos", {"0": "Baked Potato"})
    players = {p.player_id: p for p in database.list_codenames_players("juzaf-hitos")}
    assert (players["0"].display_name, players["0"].discord_user_id) == ("Baked Potato", "111")
    assert players["1"].discord_user_id is None


def test_claiming_a_seat_releases_the_users_previous_seat_and_remembers_the_name(database):
    _room(database)
    database.update_codenames_player_names("juzaf-hitos", {"0": "Potato", "1": "Code Bot"})
    database.claim_codenames_player("juzaf-hitos", "0", "111")
    database.claim_codenames_player("juzaf-hitos", "1", "111")
    players = {p.player_id: p.discord_user_id for p in database.list_codenames_players("juzaf-hitos")}
    assert players == {"0": None, "1": "111"}
    assert database.get_codenames_remembered_name("111") == "Code Bot"
    assert database.get_codenames_remembered_name("222") is None


def test_a_seat_claimed_by_someone_else_needs_override(database):
    _room(database)
    database.update_codenames_player_names("juzaf-hitos", {"0": "Potato"})
    database.claim_codenames_player("juzaf-hitos", "0", "111")
    with pytest.raises(CodenamesSeatTakenError) as error:
        database.claim_codenames_player("juzaf-hitos", "0", "222")
    assert error.value.holder_discord_user_id == "111"
    database.claim_codenames_player("juzaf-hitos", "0", "222", override=True)
    assert database.list_codenames_players("juzaf-hitos")[0].discord_user_id == "222"
    assert database.release_codenames_player("juzaf-hitos", "222")
    assert not database.release_codenames_player("juzaf-hitos", "222")


async def test_codenames_methods_are_available_on_async_database(database):
    from bga_turn.database import AsyncDatabase

    db = AsyncDatabase(database)
    room = await db.get_or_create_codenames_room("juzaf-hitos", bot_credentials="x", bot_name="🤖 Agent Antenna")
    assert room.bot_name == "🤖 Agent Antenna"
