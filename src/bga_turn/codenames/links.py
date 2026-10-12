"""codenames.game room URL parsing."""

from __future__ import annotations

import re

BASE_URL = "https://codenames.game"

# https://codenames.game/r/juzaf-hitos, with or without scheme or "www.".
_ROOM_LINK_PATTERN = re.compile(
    r"(?<![\w.@-])(?:https?://)?(?:www\.)?codenames\.game/r/(?P<room>[a-z0-9]+(?:-[a-z0-9]+)*)(?![\w-])",
    re.IGNORECASE,
)


def extract_room_ids(text: str) -> list[str]:
    """Room IDs of every codenames.game room link in *text*, in order, without duplicates."""
    seen: dict[str, None] = {}
    for match in _ROOM_LINK_PATTERN.finditer(text or ""):
        seen.setdefault(match.group("room").lower(), None)
    return list(seen)


def build_room_url(room_id: str) -> str:
    return f"{BASE_URL}/r/{room_id}"
