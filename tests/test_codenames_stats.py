from __future__ import annotations

from conftest import ADMIN_A, GUILD_A_ID, login

GUILD = str(GUILD_A_ID)
GUILD_A = f"/dashboard/{GUILD_A_ID}"


def _bga_game(database, *, guild_id: str = GUILD, winner: str = "Alice") -> None:
    database._connection.execute(
        """
        INSERT INTO game_history (table_id, game_name, guild_id, channel_id, created_by_discord_user_id,
            recruiting_started_at, game_started_at, finished_at, outcome, winner_names, final_standings, player_count)
        VALUES ('123456', 'azul', ?, '1001', '700', '2026-10-01T10:00:00+00:00', '2026-10-01T10:30:00+00:00',
            '2026-10-01T12:00:00+00:00', 'finished', ?, '["#1 Alice (1) • score 50"]', 2)
        """,
        (guild_id, f'["{winner}"]'),
    )
    database._connection.commit()


def _codenames_game(database, room_id: str, winner: str, *, guild_id: str = GUILD, finish: bool = True):
    database.get_or_create_codenames_room(room_id, bot_credentials="c", bot_name="🤖 Curious Cat")
    watch, _ = database.add_codenames_watch(
        room_id, guild_id=guild_id, channel_id="1001", created_by_discord_user_id="700"
    )
    number, _ = database.record_codenames_match(room_id, f"{room_id}-{winner}-{finish}", "2")
    game, _ = database.get_or_create_codenames_game(watch.watch_id, f"{room_id}-{winner}-{finish}", number)
    database.update_codenames_game(game.game_id, lifecycle_state="in_progress", started_at="2026-10-02T20:00:00+00:00")
    if finish:
        database.finish_codenames_game(
            game.game_id, winner_team=winner, finish_reason="assassin", player_count=4,
            finished_at="2026-10-02T20:30:00+00:00",
        )
    return watch


def test_bga_stats_leave_codenames_games_out(database):
    _bga_game(database)
    _codenames_game(database, "room-one", "blue")
    _codenames_game(database, "room-two", "red")

    for stats in (database.get_guild_extended_stats(GUILD), database.get_global_extended_stats()):
        assert stats["total_games"] == 1
        assert [item["name"] for item in stats["games_by_name"]] == ["azul"]
        assert [item["name"] for item in stats["top_winners"]] == ["Alice"]
        assert stats["avg_players_per_game"] == 2.0


def test_history_can_be_listed_per_provider(database):
    _bga_game(database)
    _codenames_game(database, "room-one", "blue")
    assert {e.provider for e in database.list_game_history_for_guild(GUILD)} == {"bga", "codenames"}
    assert [e.provider for e in database.list_game_history_for_guild(GUILD, provider="bga")] == ["bga"]
    recent = database.list_recent_game_history_for_guild(GUILD, provider="codenames")
    assert [e.table_id for e in recent] == ["room-one"]


def test_codenames_stats_count_wins_rooms_and_live_games(database):
    _codenames_game(database, "room-one", "blue")
    _codenames_game(database, "room-one", "blue")
    _codenames_game(database, "room-two", "red")
    _codenames_game(database, "room-three", "red", finish=False)
    _codenames_game(database, "elsewhere", "red", guild_id="42")
    _bga_game(database)

    stats = database.get_codenames_stats(GUILD)
    assert (stats["finished_games"], stats["red_wins"], stats["blue_wins"]) == (3, 1, 2)
    assert stats["avg_game_minutes"] == 30.0
    assert (stats["watching_rooms"], stats["games_in_progress"]) == (3, 1)
    assert stats["top_rooms"] == [{"room_id": "room-one", "games": 2}, {"room_id": "room-two", "games": 1}]

    everywhere = database.get_codenames_stats()
    assert everywhere["finished_games"] == 4
    # Room links let anyone join, so cross-server stats never list them.
    assert everywhere["top_rooms"] == []


async def test_dashboard_shows_codenames_rooms_and_results(client, database):
    _codenames_game(database, "room-one", "blue")
    _codenames_game(database, "room-two", "red", finish=False)
    login(client, database, ADMIN_A.id, guild_ids=[GUILD_A_ID])

    tables = await (await client.get(GUILD_A + "/tables")).text()
    assert "Codenames rooms" in tables
    assert "https://codenames.game/r/room-two" in tables and "Playing" in tables
    # The finished game links to the room, not to a BGA table.
    assert "Room room-one" in tables and "🔵 Blue team" in tables
    assert "boardgamearena.com/table?table=room-one" not in tables

    stats = await (await client.get(GUILD_A + "/stats")).text()
    assert "🕵️ Codenames" in stats and "Blue wins" in stats


async def test_codenames_winners_are_not_offered_as_bga_players(client, database):
    _codenames_game(database, "room-one", "blue")
    _bga_game(database, winner="Zed")
    login(client, database, ADMIN_A.id, guild_ids=[GUILD_A_ID])
    members = await (await client.get(GUILD_A + "/members")).text()
    assert "Zed" in members
    assert ">blue<" not in members


async def test_global_stats_include_codenames_totals_without_rooms(client, database):
    _codenames_game(database, "secret-room", "red")
    page = await (await client.get("/global/stats")).text()
    assert "🕵️ Codenames" in page
    assert "secret-room" not in page
