from __future__ import annotations

import json
from types import SimpleNamespace

import discord
import pytest

from bga_turn.bga import cards
from bga_turn.discord_io import DiscordRateLimiter
from bga_turn.bga.models import BgaTableSnapshot
from bga_turn.models import LinkedUser

NAMES = {"11": "Alice", "12": "Bob", "13": "Cara"}
LINKS = [LinkedUser("200", "11", "Alice"), LinkedUser("201", "", "Bob")]


def _subscription(**overrides):
    values = {"game_name": "azul", "table_url": "https://boardgamearena.com/1/azul?table=777"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _card(state, *, waiting=(), names=NAMES, snapshot=None, cover=None, note=None, **sub):
    return cards.CardData(
        lifecycle_state=state,
        table_id="777",
        subscription=_subscription(**sub),
        waiting_ids=list(waiting),
        player_names=dict(names),
        snapshot=snapshot,
        cover_image_url=cover,
        error_note=note,
    )


def _render(data, links=LINKS) -> tuple[dict, str]:
    """The container's JSON and all of its text, for assertions."""
    container = cards.build_card(data, links).to_components()[0]
    return container, json.dumps(container, ensure_ascii=False)


def test_recruiting_card_mentions_linked_players_and_counts_seats():
    snapshot = BgaTableSnapshot(
        table_id="777", status="asyncinit", game_name="azul", gameserver="1",
        player_names=NAMES, seats_taken=3, seats_total=4, seats_remaining=1,
    )
    container, text = _render(_card("recruiting", snapshot=snapshot))
    assert container["accent_color"] == discord.Color.gold().value
    assert "<@200> Alice (11)" in text  # matched by BGA ID
    assert "<@201> Bob (12)" in text  # matched by name
    assert "Cara (13)" in text and "<@" not in text.split("Cara")[0].split("\\n")[-1]
    assert "🪑🪑🪑⬜" in text
    assert "bga:link_self" in text  # self-link button


def test_in_progress_card_shows_whose_turn_with_mention():
    container, text = _render(_card("in_progress", waiting=["12", "99"]))
    assert container["accent_color"] == discord.Color.blurple().value
    assert "<@201> Bob (12)" in text
    assert "\\n99" in text  # unknown waiting player still listed by ID
    assert "🎮 Bob" in text and "⏳ Alice" in text


def test_in_progress_card_keeps_join_button_when_cover_takes_header():
    _, text = _render(_card("in_progress", cover="https://cover/x.png"))
    assert "https://cover/x.png" in text
    assert "https://boardgamearena.com/1/azul?table=777" in text


def test_finished_card_lists_winner_and_standings_without_buttons():
    snapshot = BgaTableSnapshot(
        table_id="777", status="finished", game_name="azul", gameserver="1", is_finished=True,
        winner_names=["Bob"], final_standings=["#1 Bob (12) • score 80"],
    )
    container, text = _render(_card("finished", snapshot=snapshot))
    assert container["accent_color"] == discord.Color.dark_green().value
    assert "Bob" in text and "#1 Bob (12)" in text
    assert "bga:link_self" not in text


def test_error_note_is_shown():
    _, text = _render(_card("recruiting", note="⚠️ BGA trouble"))
    assert "⚠️ BGA trouble" in text


def test_signature_ignores_links_but_tracks_content():
    base = _card("in_progress", waiting=["11"])
    assert cards.card_signature(base) == cards.card_signature(_card("in_progress", waiting=["11"]))
    assert cards.card_signature(base) != cards.card_signature(_card("in_progress", waiting=["12"]))
    assert cards.card_signature(base) != cards.card_signature(_card("in_progress", waiting=["11"], note="x"))


def test_players_to_link_includes_unnamed_waiting_players():
    assert _card("in_progress", waiting=["99"]).players_to_link() == {**NAMES, "99": ""}


# ---------------------------------------------------------------------------
# DiscordRateLimiter
# ---------------------------------------------------------------------------


class _RateLimited(discord.HTTPException):
    def __init__(self, retry_after: float) -> None:
        super().__init__(SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited")
        self.retry_after = retry_after


async def test_rate_limited_call_is_retried():
    limiter = DiscordRateLimiter()
    attempts = []

    async def call():
        attempts.append(1)
        if len(attempts) == 1:
            raise _RateLimited(0.0)
        return "ok"

    assert await limiter.call("op", "t", call) == "ok"
    assert len(attempts) == 2


async def test_exhausted_retries_pause_io(monkeypatch):
    monkeypatch.setattr("bga_turn.discord_io.random.uniform", lambda a, b: 0.0)
    limiter = DiscordRateLimiter()

    async def call():
        raise _RateLimited(0.0)

    assert await limiter.call("op", "t", call, max_retries=1) is None
    assert not limiter.io_allowed()
    limiter.reset()
    assert limiter.io_allowed()


async def test_other_errors_propagate():
    limiter = DiscordRateLimiter()

    async def call():
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")

    with pytest.raises(discord.NotFound):
        await limiter.call("op", "t", call)


async def test_sweep_forgets_idle_channels(monkeypatch):
    limiter = DiscordRateLimiter()

    async def call():
        return None

    await limiter.call("op", "t", call, channel_id="1")
    limiter._channel_activity["1"] -= limiter._CHANNEL_STATE_TTL_SECONDS + 1
    limiter.sweep(active_channel_ids=set())
    assert "1" not in limiter._channel_activity
    assert not limiter._channel_requests
