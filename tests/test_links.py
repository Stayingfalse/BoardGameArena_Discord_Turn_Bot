from __future__ import annotations

import pytest

from bga_turn.bga.links import extract_table_links, parse_public_table_url

TABLE = "891538070"
TABLE_URL = f"https://boardgamearena.com/table?table={TABLE}"


@pytest.mark.parametrize(
    "text",
    [
        f"join https://en.boardgamearena.com/table?table={TABLE} please",
        f"||https://boardgamearena.com/table?table={TABLE}||",
        f"**https://boardgamearena.com/table?table={TABLE}**",
        f"`https://boardgamearena.com/table?table={TABLE}`",
        f"<https://boardgamearena.com/table?table={TABLE}>",
        f"boardgamearena.com/table?table={TABLE}",
        f"www.boardgamearena.com/tableview?table={TABLE}.",
        f"https://boardgamearena.com/gamereview?table={TABLE}",
        f"https://boardgamearena.com/archive/replay/250101-1000/?table={TABLE}&player=1",
        f"https://boardgamearena.com/table?nr=true&table={TABLE}&invite=abc",
        f"https://bga.li/t/{TABLE}",
        f"bga.li/t/{TABLE}!",
        f"https://boardgamearena.com/account?redirect=%2Ftable%3Ftable%3D{TABLE}",
        f"https://boardgamearena.com/table?table={TABLE}&amp;nr=true",
        f"https://boardgamearena.com/table/{TABLE}",
    ],
)
def test_link_shapes_resolve_to_the_table(text):
    links = extract_table_links(text)
    assert len(links) == 1
    assert parse_public_table_url(links[0])[0] == TABLE


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"[Azul](https://boardgamearena.com/12/azul?table={TABLE})", f"https://boardgamearena.com/12/azul?table={TABLE}"),
        (f"https://fr.boardgamearena.com/10/seven_wonders/?table={TABLE}#top", f"https://fr.boardgamearena.com/10/seven_wonders?table={TABLE}"),
    ],
)
def test_game_page_links_keep_server_and_game(text, expected):
    assert extract_table_links(text) == [expected]
    table_id, url, _, gameserver, game_name = parse_public_table_url(expected)
    assert (table_id, url, gameserver) == (TABLE, expected, expected.split("/")[3])
    assert game_name


def test_multiple_links_are_deduplicated_in_order():
    text = (
        "https://boardgamearena.com/table?table=1111111 and "
        "https://boardgamearena.com/2/azul?table=2222222 and https://bga.li/t/1111111"
    )
    assert [parse_public_table_url(url)[0] for url in extract_table_links(text)] == ["1111111", "2222222"]


@pytest.mark.parametrize(
    "text",
    [
        "https://boardgamearena.com/gamepanel?game=azul",
        f"https://notboardgamearena.com/table?table={TABLE}",
        "email me@boardgamearena.com",
        "no links here",
    ],
)
def test_non_table_links_are_ignored(text):
    assert extract_table_links(text) == []


def test_non_game_pages_need_resolution():
    assert parse_public_table_url(TABLE_URL) == (TABLE, "", "https://boardgamearena.com", "", "")
