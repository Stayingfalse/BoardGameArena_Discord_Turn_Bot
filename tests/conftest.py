from __future__ import annotations

import secrets
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest

from bga_turn.dashboard import create_dashboard_app
from bga_turn.database import Database

SECRET_KEY = "test-secret"


def make_text_channel(channel_id: int, name: str, guild: "FakeGuild") -> discord.TextChannel:
    """A real ``discord.TextChannel`` instance (for isinstance checks) without a gateway."""
    channel = discord.TextChannel.__new__(discord.TextChannel)
    channel.id = channel_id
    channel.name = name
    channel.guild = guild
    channel.category_id = None
    channel.position = 0
    return channel


class FakeAvatar:
    def with_size(self, size: int) -> "FakeAvatar":
        return self

    @property
    def url(self) -> str:
        return "https://cdn.discordapp.com/embed/avatars/0.png"


class FakeMember:
    def __init__(self, member_id: int, name: str, *, manage_guild: bool = False) -> None:
        self.id = member_id
        self.name = name
        self.display_name = name.title()
        self.bot = False
        self.display_avatar = FakeAvatar()
        self.guild_permissions = SimpleNamespace(manage_guild=manage_guild)


def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")


class FakeGuild:
    def __init__(self, guild_id: int, name: str, members: list[FakeMember]) -> None:
        self.id = guild_id
        self.name = name
        self.icon = None
        self.me = None
        self.member_count = len(members)
        # Simulates the default intents: no member is cached.
        self._members = {member.id: member for member in members}
        self.text_channels: list[discord.TextChannel] = []
        self.query_calls = 0

    def get_member(self, member_id: int) -> FakeMember | None:
        return None

    async def fetch_member(self, member_id: int) -> FakeMember:
        member = self._members.get(member_id)
        if member is None:
            raise not_found()
        return member

    async def query_members(self, query=None, *, limit=5, user_ids=None, cache=True, presences=False):
        self.query_calls += 1
        if user_ids is not None:
            return [self._members[item] for item in user_ids if item in self._members][:limit]
        return [m for m in self._members.values() if m.name.startswith(query.lower())][:limit]

    def get_channel(self, channel_id):
        return next((c for c in self.text_channels if c.id == channel_id), None)

    get_channel_or_thread = get_channel


class FakeBot:
    def __init__(self, guilds: list[FakeGuild]) -> None:
        self.guilds = guilds
        self.monitor = SimpleNamespace(_default_recruiting_only=False, _default_forced_channel_id=None)
        self._default_delete_invite_message = False

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return next((g for g in self.guilds if g.id == guild_id), None)

    def get_channel(self, channel_id: int):
        for guild in self.guilds:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None


# Users: 100 manages guild A; 200 is a plain member of A; 300 manages guild B only.
ADMIN_A = FakeMember(100, "alice", manage_guild=True)
MEMBER_A = FakeMember(200, "bob")
ADMIN_B = FakeMember(300, "carol", manage_guild=True)
GUILD_A_ID = 111111111111111111
GUILD_B_ID = 222222222222222222


@pytest.fixture
def database(tmp_path: Path) -> Database:
    schema = files("bga_turn").joinpath("schema.sql").read_text(encoding="utf-8")
    db = Database(db_path=tmp_path / "test.db", schema_sql=schema)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def guilds() -> tuple[FakeGuild, FakeGuild]:
    guild_a = FakeGuild(GUILD_A_ID, "Guild A", [ADMIN_A, MEMBER_A])
    guild_b = FakeGuild(GUILD_B_ID, "Guild B", [ADMIN_B])
    guild_a.text_channels = [make_text_channel(1001, "general", guild_a)]
    guild_b.text_channels = [make_text_channel(2001, "b-general", guild_b)]
    return guild_a, guild_b


@pytest.fixture
def bot(guilds) -> FakeBot:
    return FakeBot(list(guilds))


@pytest.fixture
async def client(aiohttp_client, bot, database):
    app = create_dashboard_app(
        bot=bot,
        database=database,
        base_url="http://localhost:8080",
        client_id="123",
        client_secret="secret",
        secret_key=SECRET_KEY,
        global_admin_ids=frozenset({"999"}),
    )
    return await aiohttp_client(app)


def login(client, database: Database, user_id: int, guild_ids: list[int] | None = None) -> str:
    """Create a session for *user_id* and attach its cookie to *client*; return the CSRF token."""
    import hashlib
    import hmac

    session_id = secrets.token_urlsafe(32)
    database.create_dashboard_session(
        session_id=session_id,
        user_id=str(user_id),
        username=f"user{user_id}",
        avatar=None,
        guilds=[
            {"id": str(guild_id), "name": f"G{guild_id}", "icon": "", "permissions": str(1 << 5)}
            for guild_id in (guild_ids or [])
        ],
        ttl_seconds=3600,
    )
    client.session.cookie_jar.update_cookies({"bga_session": session_id})
    return hmac.new(SECRET_KEY.encode(), session_id.encode(), hashlib.sha256).hexdigest()
