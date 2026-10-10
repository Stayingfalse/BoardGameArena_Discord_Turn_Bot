from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .i18n import tr

BASE_URL = "https://boardgamearena.com"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_table_id(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError(tr("error_empty_table_value"))
    if candidate.isdigit():
        return candidate

    parsed = urlparse(candidate)
    if parsed.scheme and parsed.netloc:
        short_link_match = re.search(r"^/t/(\d+)/?$", parsed.path, re.IGNORECASE)
        if parsed.netloc.lower() in {"bga.li", "www.bga.li"} and short_link_match:
            return short_link_match.group(1)
        table_values = parse_qs(parsed.query).get("table")
        if table_values and table_values[0].isdigit():
            return table_values[0]

    match = re.search(r"(?:table=|/t/)(\d+)", candidate, re.IGNORECASE)
    if match:
        return match.group(1)

    raise ValueError(tr("error_invalid_table_id"))


# Any boardgamearena.com (any subdomain) or bga.li link, with or without a scheme.
_BGA_LINK_PATTERN = re.compile(
    r"(?<![\w.@-])(?:https?://)?(?:[a-z0-9-]+\.)*(?:boardgamearena\.com|bga\.li)(?::\d+)?(?:[/?#][^\s<>]*)?",
    re.IGNORECASE,
)
# Characters Discord markdown or prose commonly leaves stuck to the end of a link.
_LINK_TRAILING_CHARS = ".,;:!?)]}>\"'`*_~|"
_TABLE_ID_PATTERNS = (
    re.compile(r"[?&#;]table(?:_?id)?=(\d{4,})", re.IGNORECASE),
    re.compile(r"/(?:t|table)/(\d{4,})(?=[/?#&]|$)", re.IGNORECASE),
)
_GAME_PAGE_PATH = re.compile(r"^/(\d+)/([a-z0-9_]+)/?$", re.IGNORECASE)


def canonical_table_url(raw_link: str) -> str | None:
    """Return a canonical URL for the BGA table in *raw_link*, or ``None``.

    Accepts every link shape seen in the wild: game pages (``/1/azul?table=``),
    ``/table?table=``, ``/tableview?table=``, replay/review/archive pages, short
    ``bga.li/t/<id>`` links, language subdomains, missing schemes, ``&amp;`` and
    URL-encoded parameters (e.g. a table link inside a login ``redirect=``).
    Game-page links keep their game server and name so no extra lookup is
    needed; anything else becomes ``/table?table=<id>`` and is resolved later.
    """
    link = raw_link.strip().rstrip(_LINK_TRAILING_CHARS)
    if not link:
        return None
    if "://" not in link:
        link = "https://" + link

    decoded = link.replace("&amp;", "&")
    table_id = None
    for _ in range(3):
        for pattern in _TABLE_ID_PATTERNS:
            match = pattern.search(decoded)
            if match is not None:
                table_id = match.group(1)
                break
        if table_id is not None:
            break
        unquoted = unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    if table_id is None:
        return None

    parsed = urlparse(link)
    host = (parsed.hostname or "").lower()
    base_url = f"https://{host}" if host.endswith("boardgamearena.com") else BASE_URL
    game_page = _GAME_PAGE_PATH.match(parsed.path)
    if game_page is not None:
        return f"{base_url}/{game_page.group(1)}/{game_page.group(2)}?table={table_id}"
    return f"{base_url}/table?table={table_id}"


def extract_table_links(text: str) -> list[str]:
    """Return canonical URLs for every distinct BGA table linked in *text*, in order."""
    links: list[str] = []
    seen: set[str] = set()
    for match in _BGA_LINK_PATTERN.finditer(text or ""):
        url = canonical_table_url(match.group(0))
        if url is None:
            continue
        table_id = parse_qs(urlparse(url).query)["table"][0]
        if table_id in seen:
            continue
        seen.add(table_id)
        links.append(url)
    return links


def parse_public_table_url(value: str) -> tuple[str, str, str, str, str]:
    candidate = value.strip()
    if not candidate:
        raise ValueError(tr("error_empty_table_url"))

    # Bare numeric table id: the game server / game name are unknown and must be
    # resolved anonymously by BgaClient. An empty normalized URL + empty
    # gameserver/game_name signal "resolution required" to the caller.
    if candidate.isdigit():
        return candidate, "", BASE_URL, "", ""

    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(tr("error_watch_requires_full_public_url"))

    if parsed.netloc.lower() in {"bga.li", "www.bga.li"}:
        short_link_match = re.search(r"^/t/(\d+)/?$", parsed.path, re.IGNORECASE)
        if short_link_match:
            return short_link_match.group(1), "", BASE_URL, "", ""

    table_values = parse_qs(parsed.query).get("table")
    if not table_values or not table_values[0].isdigit():
        raise ValueError(tr("error_url_missing_table_param"))
    table_id = table_values[0]

    base_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    if "boardgamearena.com" not in parsed.netloc.lower():
        base_url = BASE_URL

    # Only game pages ("/<server>/<game>?table=") name the game server; every
    # other page (`table`, `tableview`, replays, ...) is resolved anonymously
    # (see BgaClient.fetch_public_table_snapshot).
    game_page = _GAME_PAGE_PATH.match(parsed.path)
    if game_page is None:
        return table_id, "", base_url, "", ""
    gameserver, game_name = game_page.group(1), game_page.group(2)

    normalized_url = f"{base_url}/{gameserver}/{game_name}?table={table_id}"
    return table_id, normalized_url, base_url, gameserver, game_name


def build_table_url(table_id: str) -> str:
    return f"{BASE_URL}/table?table={table_id}"


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def json_loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def json_loads_dict(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key_str = str(key)
        item_str = str(item).strip()
        if key_str and item_str:
            result[key_str] = item_str
    return result


def format_game_name(game_name: str | None) -> str:
    if not game_name:
        return tr("value_unknown").capitalize()
    return re.sub(r"[_-]+", " ", game_name).strip().title()
