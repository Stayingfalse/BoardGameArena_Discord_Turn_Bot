from __future__ import annotations

import dataclasses
import io

import discord
import pytest
from PIL import Image
from test_codenames_monitor import (
    CHANNEL,
    ROOM,
    FakeChannel,
    _monitor,
    _show,
    _watch_and_link,
)
from test_codenames_state import _replay

from bga_turn.codenames.board_image import FILENAME, BoardSnapshot, render_board_png
from bga_turn.codenames.state import BoardCard


@pytest.fixture
def final_view():
    return list(_replay())[-1]


def test_the_final_view_has_the_whole_key_and_every_clue(final_view):
    assert len(final_view.board) == 25
    # Spectators see every colour once the game is over, guessed or not.
    assert all(card.color in {"red", "blue", "neutral", "black"} for card in final_view.board)
    assert sum(card.revealed for card in final_view.board) == 10
    assert [(t.clue.team, t.clue.word, t.clue.giver_name) for t in final_view.turns] == [
        ("red", "CLUE 1", "Bob"),
        ("blue", "ANIMAL", "Carol"),
        ("red", "VESSLE", "Bob"),
        ("blue", "EDABLE", "Carol"),
        ("red", "RISKY CLUE", "Bob"),
    ]
    assert [g.word for g in final_view.turns[0].guesses] == ["BATTERY", "LITTER", "SEED", "WING"]
    assert [g.word for g in final_view.turns[-1].guesses] == ["SCALE"]


def test_history_starts_empty_mid_lobby():
    lobby = next(v for v in _replay() if v.phase == "lobby")
    assert lobby.turns == ()


def test_snapshots_survive_a_round_trip_through_json(final_view):
    snapshot = BoardSnapshot.from_view(final_view, 3)
    assert BoardSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert snapshot.rosters["red"] == {"spymasters": ["Bob"], "operatives": ["Alice"]}
    assert (snapshot.winner, snapshot.reason, snapshot.game_number) == ("blue", "assassin", 3)


def _png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        return image.size


def test_the_board_renders_as_a_png(final_view):
    width, height = _png_size(render_board_png(BoardSnapshot.from_view(final_view, 1)))
    assert width == 1500
    assert 700 < height < 1000


def test_long_histories_grow_the_image_instead_of_clipping(final_view):
    many_turns = final_view.turns * 6
    tall = dataclasses.replace(final_view, turns=many_turns)
    _, short_height = _png_size(render_board_png(BoardSnapshot.from_view(final_view, 1)))
    _, tall_height = _png_size(render_board_png(BoardSnapshot.from_view(tall, 1)))
    assert tall_height > short_height


def test_unknown_colours_and_non_latin_words_still_render(final_view):
    board = (BoardCard("ДИНОЗАВР", None, False), BoardCard("ΑΣΤΕΡΙ", "red", True)) + final_view.board[2:]
    odd = dataclasses.replace(final_view, board=board, turns=(), winner_team=None, finish_reason=None)
    _png_size(render_board_png(BoardSnapshot.from_view(odd, 1)))


# ----------------------------------------------------------------------
# On the Discord card
# ----------------------------------------------------------------------


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel(CHANNEL)


def _gallery_urls(layout: discord.ui.LayoutView) -> list[str]:
    return [
        item.media.url
        for gallery in layout.walk_children()
        if isinstance(gallery, discord.ui.MediaGallery)
        for item in gallery.items
    ]


def _attached(message) -> list[str]:
    return [file.filename for file in message.kwargs.get("files") or []]


async def test_the_finished_card_carries_the_board_image(database, channel):
    monitor = _monitor(database, channel)
    views = await _watch_and_link(monitor, database)
    for view in views[:-1]:
        await _show(monitor, view)
    [card] = channel.cards
    assert _gallery_urls(card.view) == []

    await _show(monitor, views[-1])

    assert _gallery_urls(card.view) == [f"attachment://{FILENAME}"]
    assert _attached(card) == [FILENAME]
    [game] = [database.get_latest_codenames_game(w.watch_id) for w in database.list_codenames_watches(ROOM)]
    assert game.state["board"]["winner"] == "blue"


async def test_the_image_is_kept_after_a_restart_and_a_rematch(database, channel):
    first = _monitor(database, channel)
    views = await _watch_and_link(first, database)
    for view in views:
        await _show(first, view)

    restarted = _monitor(database, channel)
    await restarted.start()
    rematch = dataclasses.replace(views[0], match_id="QuVMIPSqNpU")
    await _show(restarted, rematch)

    game_1, game_2 = channel.cards
    assert _gallery_urls(game_1.view) == [f"attachment://{FILENAME}"]
    assert _gallery_urls(game_2.view) == []


async def test_a_failed_drawing_leaves_the_card_without_an_image(database, channel, monkeypatch):
    def broken(_snapshot):
        raise RuntimeError("no fonts")

    monkeypatch.setattr("bga_turn.codenames.monitor.render_board_png", broken)
    monitor = _monitor(database, channel)
    views = await _watch_and_link(monitor, database)
    for view in views:
        await _show(monitor, view)

    [card] = channel.cards
    assert _gallery_urls(card.view) == []
    assert "Blue wins" in str([item.content for item in card.view.walk_children() if hasattr(item, "content")])
