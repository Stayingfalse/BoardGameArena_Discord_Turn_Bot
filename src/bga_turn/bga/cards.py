"""Discord table cards: one message per table, rendered from plain data.

Nothing here touches the database, BGA or Discord's API. The monitor gathers a
:class:`CardData` plus the linked Discord users for its players, and these
functions turn that into a Components V2 layout. That keeps rendering easy to
test and lets the monitor skip work when a card's content has not changed.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord
from discord.components import MediaGalleryItem

from ..i18n import tr
from ..models import LinkedUser, WatchSubscription
from .models import BgaTableSnapshot
from ..utils import format_game_name
from .links import build_table_url
from .views import LinkSelfButton

LIFECYCLE_RECRUITING = "recruiting"
LIFECYCLE_IN_PROGRESS = "in_progress"
LIFECYCLE_FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class CardData:
    """Everything a table card shows, except linked Discord users."""

    lifecycle_state: str
    table_id: str
    subscription: WatchSubscription
    waiting_ids: list[str]
    player_names: dict[str, str]
    snapshot: BgaTableSnapshot | None
    cover_image_url: str | None
    error_note: str | None

    def players_to_link(self) -> dict[str, str]:
        """Players whose Discord links the card needs, including unnamed waiting ones."""
        players = dict(self.player_names)
        for player_id in self.waiting_ids:
            players.setdefault(player_id, "")
        return players


def card_signature(data: CardData) -> str:
    """A string that changes whenever the rendered card would change.

    Linked users are deliberately left out, so the monitor can compare
    signatures before paying for a database lookup.
    """
    snapshot = data.snapshot
    signature = (
        data.lifecycle_state,
        data.table_id,
        data.subscription.game_name or "",
        data.subscription.table_url or "",
        tuple(data.waiting_ids),
        tuple(sorted(data.player_names.items())),
        data.cover_image_url or "",
        "" if snapshot is None else snapshot.game_name,
        "" if snapshot is None else snapshot.status,
        "" if snapshot is None else (snapshot.table_url or ""),
        "" if snapshot is None else (snapshot.cover_image_url or ""),
        0 if snapshot is None else snapshot.seats_taken,
        None if snapshot is None else snapshot.seats_total,
        None if snapshot is None else snapshot.seats_remaining,
        () if snapshot is None else tuple(sorted(snapshot.player_avatars.items())),
        () if snapshot is None else tuple(snapshot.winner_names),
        () if snapshot is None else tuple(snapshot.final_standings),
        "" if snapshot is None else (snapshot.finished_at or ""),
        "" if snapshot is None else (snapshot.finish_reason or ""),
        data.error_note or "",
    )
    return repr(signature)


def build_card(data: CardData, linked_users: list[LinkedUser]) -> discord.ui.LayoutView:
    links = _LinkIndex(linked_users)
    if data.lifecycle_state == LIFECYCLE_RECRUITING:
        return _recruiting_card(data, links)
    if data.lifecycle_state == LIFECYCLE_FINISHED:
        return _finished_card(data)
    return _in_progress_card(data, links)


def build_link_status_view(
    text: str,
    *,
    color: discord.Color,
    table_url: str | None = None,
) -> discord.ui.LayoutView:
    """The "link detected / registered / failed" note posted for a shared link."""
    body: discord.ui.Item = discord.ui.TextDisplay(text)
    if table_url:
        body = discord.ui.Section(
            discord.ui.TextDisplay(text),
            accessory=discord.ui.Button(label=tr("button_join_table"), url=table_url),
        )
    return _layout([body], color)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


def _recruiting_card(data: CardData, links: _LinkIndex) -> discord.ui.LayoutView:
    snapshot = data.snapshot
    game_name = format_game_name((snapshot.game_name if snapshot is not None else None) or data.subscription.game_name)
    table_url = data.subscription.table_url or build_table_url(data.table_id)

    header_text = f"🎲  **{game_name}**\n{tr('label_table')}: {data.table_id}"
    items: list[discord.ui.Item] = [
        discord.ui.Section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Button(label=tr("button_join_table"), url=table_url),
        ),
        discord.ui.Separator(),
    ]

    if snapshot is not None and snapshot.cover_image_url:
        items.append(discord.ui.MediaGallery(MediaGalleryItem(snapshot.cover_image_url)))
        items.append(discord.ui.Separator())

    players_value = _player_lines(
        data.player_names,
        {} if snapshot is None else snapshot.player_avatars,
        links,
    )
    items.append(discord.ui.TextDisplay(f"**{tr('label_players_joined')}**\n{players_value or tr('value_none')}"))

    if snapshot is not None and snapshot.seats_total is not None and snapshot.seats_remaining is not None:
        seats_line = f"{seat_icons(snapshot.seats_taken, snapshot.seats_total)}  " + tr(
            "invite_message_seats_value",
            seats_taken=snapshot.seats_taken,
            seats_total=snapshot.seats_total,
            seats_remaining=snapshot.seats_remaining,
        )
    else:
        seats_line = tr("invite_message_seats_unknown", seats_taken=len(data.player_names))
    status = (snapshot.status if snapshot is not None else "") or tr("value_unknown")
    items.append(discord.ui.TextDisplay(f"_{seats_line} · {tr('label_status')}: {status}_"))
    items.append(discord.ui.Separator())
    _append_error_note(items, data.error_note)
    items.append(discord.ui.ActionRow(LinkSelfButton()))
    return _layout(items, discord.Color.gold())


def _in_progress_card(data: CardData, links: _LinkIndex) -> discord.ui.LayoutView:
    game_name = format_game_name(data.subscription.game_name)
    table_url = data.subscription.table_url or build_table_url(data.table_id)
    header_text = f"🎮  **{game_name}** · {tr('lifecycle_in_progress_title')}\n{tr('label_table')}: {data.table_id}"
    join_button = discord.ui.Button(label=tr("button_join_table"), url=table_url)

    items: list[discord.ui.Item] = [
        discord.ui.Section(
            discord.ui.TextDisplay(header_text),
            accessory=discord.ui.Thumbnail(data.cover_image_url) if data.cover_image_url else join_button,
        ),
        discord.ui.Separator(),
    ]

    if data.waiting_ids:
        waiting_players = "\n".join(
            format_waiting_player(player_id, data.player_names, links) for player_id in data.waiting_ids
        )
        items.append(discord.ui.TextDisplay(f"**🎯 {tr('label_current_turn')}**\n{waiting_players or tr('value_none')}"))
        items.append(discord.ui.Separator())

    if data.player_names:
        waiting_set = set(data.waiting_ids)
        all_players_lines = [
            f"🎮 {name}" if player_id in waiting_set else f"⏳ {name}"
            for player_id, name in sorted(data.player_names.items(), key=lambda item: item[1].casefold())
        ]
        items.append(discord.ui.TextDisplay(f"**{tr('label_all_players')}**\n" + "\n".join(all_players_lines)))

    if data.cover_image_url:
        # The thumbnail took the header's accessory slot, so the join button goes here.
        items.append(discord.ui.Section(discord.ui.TextDisplay("​"), accessory=join_button))

    items.append(discord.ui.Separator())
    _append_error_note(items, data.error_note)
    items.append(discord.ui.ActionRow(LinkSelfButton()))
    return _layout(items, discord.Color.blurple())


def _finished_card(data: CardData) -> discord.ui.LayoutView:
    snapshot = data.snapshot
    game_name = format_game_name((snapshot.game_name if snapshot is not None else None) or data.subscription.game_name)
    finished_at = (snapshot.finished_at if snapshot is not None else None) or ""
    cover_image_url = (snapshot.cover_image_url if snapshot is not None else None) or data.cover_image_url
    header_text = (
        f"🏆  **{game_name}** · {tr('lifecycle_finished_title')}\n"
        f"{tr('label_table')}: {data.table_id}"
        + (f" · {finished_at}" if finished_at else "")
    )

    items: list[discord.ui.Item] = []
    if cover_image_url:
        items.append(
            discord.ui.Section(discord.ui.TextDisplay(header_text), accessory=discord.ui.Thumbnail(cover_image_url))
        )
    else:
        items.append(discord.ui.TextDisplay(header_text))
    items.append(discord.ui.Separator())

    if snapshot is not None and snapshot.winner_names:
        items.append(discord.ui.TextDisplay(f"**🥇 {tr('label_winner')}:** {', '.join(snapshot.winner_names)}"))
        items.append(discord.ui.Separator())
    if snapshot is not None and snapshot.final_standings:
        standings_lines = "\n".join(snapshot.final_standings[:10])
        items.append(discord.ui.TextDisplay(f"**{tr('label_final_standings')}**\n{standings_lines}"))
    if snapshot is not None and snapshot.finish_reason:
        items.append(discord.ui.TextDisplay(f"_{tr('label_finish_reason')}: {snapshot.finish_reason}_"))

    if data.error_note:
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(data.error_note))
    return _layout(items, discord.Color.dark_green())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _LinkIndex:
    """Linked Discord users, looked up by BGA player ID or by BGA name."""

    def __init__(self, linked_users: list[LinkedUser]) -> None:
        self.by_bga_id = {user.bga_player_id: user for user in linked_users if user.bga_player_id}
        self.by_name = {user.bga_player_name.casefold(): user for user in linked_users if user.bga_player_name}

    def find(self, player_id: str, player_name: str) -> LinkedUser | None:
        linked = self.by_bga_id.get(player_id)
        if linked is None and player_name.strip():
            linked = self.by_name.get(player_name.strip().casefold())
        return linked


def _player_lines(player_names: dict[str, str], player_avatars: dict[str, str], links: _LinkIndex) -> str:
    lines: list[str] = []
    for player_id, player_name in sorted(player_names.items(), key=lambda item: item[1].casefold()):
        linked = links.find(player_id, player_name)
        mention = f"<@{linked.discord_user_id}> " if linked is not None else ""
        avatar = player_avatars.get(player_id)
        avatar_suffix = f" • [avatar]({avatar})" if avatar else ""
        lines.append(f"{mention}{player_name} ({player_id}){avatar_suffix}")
    return "\n".join(lines)


def format_player_reference(player_id: str, player_names: dict[str, str]) -> str:
    player_name = player_names.get(player_id)
    if player_name and player_name != player_id:
        return f"{player_name} ({player_id})"
    return player_id


def format_waiting_player(player_id: str, player_names: dict[str, str], links: _LinkIndex) -> str:
    linked_user = links.find(player_id, player_names.get(player_id, ""))
    if linked_user is None:
        return format_player_reference(player_id, player_names)
    player_label = linked_user.bga_player_name or player_names.get(player_id, "").strip() or player_id
    player_id_label = linked_user.bga_player_id or player_id
    return f"<@{linked_user.discord_user_id}> {player_label} ({player_id_label})"


def seat_icons(seats_taken: int, seats_total: int) -> str:
    return "🪑" * seats_taken + "⬜" * max(0, seats_total - seats_taken)


def _append_error_note(items: list[discord.ui.Item], error_note: str | None) -> None:
    if error_note:
        items.append(discord.ui.TextDisplay(error_note))
        items.append(discord.ui.Separator())


def _layout(items: list[discord.ui.Item], color: discord.Color) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_color=color))
    return view
