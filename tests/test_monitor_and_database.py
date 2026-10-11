from __future__ import annotations

import sqlite3
from importlib.resources import files
from types import SimpleNamespace

from conftest import GUILD_A_ID, make_text_channel

from bga_turn.database import Database
from bga_turn.bga.monitor import BgaMonitor


def test_initialize_drops_legacy_followed_players_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE followed_players (follow_id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    database = Database(db_path=db_path, schema_sql=files("bga_turn").joinpath("schema.sql").read_text())
    database.initialize()
    assert not database._table_exists("followed_players")
    database.close()


def _subscription(guild_id: int, channel_id: int):
    return SimpleNamespace(guild_id=str(guild_id), channel_id=str(channel_id))


async def test_forced_channel_in_another_guild_is_ignored(database, bot, guilds):
    guild_a, guild_b = guilds
    # Guild A's stored setting points at a channel of guild B.
    database.upsert_guild_settings(
        str(GUILD_A_ID), recruiting_only=False, delete_invite_message=False, forced_channel_id="2001"
    )
    monitor = BgaMonitor(bot, database, bga_client=None, poll_seconds=15)
    channel = await monitor._resolve_channel(_subscription(GUILD_A_ID, 1001), "123")
    assert channel.id == 1001


async def test_forced_channel_in_same_guild_is_used(database, bot, guilds):
    guild_a, _ = guilds
    guild_a.text_channels.append(make_text_channel(1002, "games", guild_a))
    database.upsert_guild_settings(
        str(GUILD_A_ID), recruiting_only=False, delete_invite_message=False, forced_channel_id="1002"
    )
    monitor = BgaMonitor(bot, database, bga_client=None, poll_seconds=15)
    channel = await monitor._resolve_channel(_subscription(GUILD_A_ID, 1001), "123")
    assert channel.id == 1002


async def test_async_database_runs_methods_off_the_event_loop(database):
    import threading

    from bga_turn.database import AsyncDatabase

    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = database.get_global_stats

    def recording():
        seen.append(threading.get_ident())
        return original()

    database.get_global_stats = recording
    assert await AsyncDatabase(database).get_global_stats() == {"total": 0, "recruiting": 0}
    assert seen and seen[0] != loop_thread


def test_bot_code_never_calls_the_database_on_the_event_loop():
    """The bot's async code must use ``self.db`` (AsyncDatabase), not ``self.database``."""
    import re
    from pathlib import Path

    import bga_turn

    package = Path(bga_turn.__file__).parent
    for name in ("bga/monitor.py", "bga/commands.py"):
        source = (package / name).read_text(encoding="utf-8")
        direct = [line.strip() for line in source.splitlines() if re.search(r"self\.database\.\w+", line)]
        assert direct == [], f"{name}: use 'await self.db.<method>(...)' instead: {direct}"
