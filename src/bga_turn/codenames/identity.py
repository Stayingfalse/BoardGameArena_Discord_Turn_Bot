"""The bot's identity in a Codenames room: credentials and a display name.

The site lets each client make up its own credentials; the server ties a seat
to them. Both are stored per room, so the bot gets the same seat and name back
after a restart.
"""

from __future__ import annotations

import random
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..database import AsyncDatabase
    from .models import CodenamesRoom

# Every name starts with 🤖 so players can tell the bot apart and leave it off
# the teams. The site strips brackets from names, so none are used.
DEFAULT_BOT_NAMES: tuple[str, ...] = (
    "🤖 Agent Turnkeeper",
    "🤖 Fly on the Wall",
    "🤖 Night Owl Observer",
    "🤖 The Scorekeeper",
    "🤖 Silent Butler",
    "🤖 Agent Antenna",
    "🤖 Curious Cat",
    "🤖 Undercover Pigeon",
)

_CONSONANTS = "bdfghjklmnprstvz"
_VOWELS = "aeiou"


def generate_credentials(rng: random.Random | None = None) -> str:
    """Three 8-letter pronounceable words, the same shape the site uses."""
    choice = rng.choice if rng is not None else secrets.choice

    def word() -> str:
        return "".join(choice(_CONSONANTS) + choice(_VOWELS) for _ in range(4))

    return "-".join(word() for _ in range(3))


def pick_bot_name(names: tuple[str, ...] | list[str] | None = None, rng: random.Random | None = None) -> str:
    pool = [name.strip() for name in (names or DEFAULT_BOT_NAMES) if name.strip()] or list(DEFAULT_BOT_NAMES)
    return (rng or random).choice(pool)


def parse_bot_names(raw: str | None) -> tuple[str, ...]:
    """Parse a ``|``-separated list of names (e.g. from ``.env``); empty means the defaults."""
    if not raw:
        return DEFAULT_BOT_NAMES
    names = tuple(name.strip() for name in raw.split("|") if name.strip())
    return names or DEFAULT_BOT_NAMES


async def ensure_room_identity(db: AsyncDatabase, room_id: str, names: tuple[str, ...] | None = None) -> CodenamesRoom:
    """The room's stored bot identity, creating a fresh one for a new room."""
    room = await db.get_codenames_room(room_id)
    if room is not None:
        return room
    return await db.get_or_create_codenames_room(
        room_id, bot_credentials=generate_credentials(), bot_name=pick_bot_name(names)
    )
