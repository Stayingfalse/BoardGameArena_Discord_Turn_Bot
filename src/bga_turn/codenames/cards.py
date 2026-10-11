"""Discord cards and turn pings for Codenames games, rendered from plain data.

One card per game ("Game N" of a room). Cards are sent with mentions disabled:
Discord only notifies on new messages, so turn pings are separate short
messages (see :func:`build_turn_ping`). No I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import discord

from ..i18n import tr
from .links import build_room_url
from .state import ROLES, TEAMS, GameView

TEAM_EMOJI = {"red": "🔴", "blue": "🔵"}
CARD_EMOJI = {"red": "🟥", "blue": "🟦", "neutral": "⬜", "black": "⬛"}
TEAM_COLOR = {"red": discord.Color.red(), "blue": discord.Color.blue()}
_TEAM_KEYS = {"red": "cn_team_red", "blue": "cn_team_blue"}
_ROLE_KEYS = {"spymasters": "cn_role_spymasters", "operatives": "cn_role_operatives"}


@dataclass(frozen=True, slots=True)
class CardData:
    room_id: str
    game_number: int
    view: GameView | None
    # In-room seat -> Discord user ID, for seats someone has claimed.
    links: dict[str, str] = field(default_factory=dict)
    # Discord users asked to play who haven't claimed a seat yet.
    invited: tuple[str, ...] = ()
    # Shown when the bot stopped watching (room closed, idle, unsupported...).
    closed_note: str | None = None


def card_signature(data: CardData) -> str:
    """Changes whenever the rendered card would."""
    view = data.view
    parts: list[object] = [data.room_id, data.game_number, sorted(data.links.items()), data.invited, data.closed_note]
    if view is not None:
        parts += [
            view.match_id,
            view.phase,
            view.active_team,
            view.active_role,
            sorted((team, role, tuple(ids)) for team, roles in view.rosters.items() for role, ids in roles.items()),
            sorted((pid, view.player_name(pid)) for pid in _visible_player_ids(view)),
            sorted(view.remaining.items()),
            view.clue,
            view.last_guess,
            view.guesses_left,
            view.winner_team,
            view.finish_reason,
        ]
    return repr(parts)


def team_label(team: str | None) -> str:
    if team not in TEAMS:
        return tr("value_unknown")
    return f"{TEAM_EMOJI[team]} {tr(_TEAM_KEYS[team])}"


def build_card(data: CardData) -> discord.ui.LayoutView:
    from .views import ClaimSeatButton  # views imports the database; keep cards import-light

    view = data.view
    header = f"🕵️ **{tr('cn_title')}** · {tr('cn_game_number', number=data.game_number)}\n{_status_line(data)}"
    items: list[discord.ui.Item] = [discord.ui.TextDisplay(header)]

    if view is not None and view.phase != "lobby":
        score = tr(
            "cn_score",
            red=f"{TEAM_EMOJI['red']} {view.remaining.get('red', 0)}",
            blue=f"{TEAM_EMOJI['blue']} {view.remaining.get('blue', 0)}",
        )
        lines = [score]
        if view.last_guess is not None and view.phase == "in_progress":
            lines.append(
                tr(
                    "cn_last_guess",
                    player=_escape(view.last_guess.player_name or tr("value_unknown")),
                    word=_escape(view.last_guess.word),
                    card=CARD_EMOJI.get(view.last_guess.card_color or "", "❔"),
                )
            )
        items.append(discord.ui.TextDisplay("\n".join(lines)))

    items.append(discord.ui.Separator())
    if view is not None:
        for team in TEAMS:
            items.append(discord.ui.TextDisplay(_team_block(view, team, data.links)))
        others = [pid for pid in view.spectator_ids]
        if others:
            names = ", ".join(_player_label(view, pid, data.links) for pid in others)
            items.append(discord.ui.TextDisplay(f"👀 {tr('cn_label_not_on_team')}: {names}"))
    elif data.closed_note is None:
        items.append(discord.ui.TextDisplay(tr("cn_connecting")))

    if data.invited and data.closed_note is None and (view is None or view.phase != "finished"):
        mentions = " ".join(f"<@{user_id}>" for user_id in data.invited)
        items.append(discord.ui.TextDisplay(tr("cn_invited", mentions=mentions)))

    if data.closed_note:
        items.append(discord.ui.Separator())
        items.append(discord.ui.TextDisplay(data.closed_note))

    buttons: list[discord.ui.Item] = [discord.ui.Button(label=tr("cn_button_open_room"), url=build_room_url(data.room_id))]
    if data.closed_note is None and (view is None or view.phase != "finished"):
        buttons.append(ClaimSeatButton())
    items.append(discord.ui.Separator())
    items.append(discord.ui.ActionRow(*buttons))

    layout = discord.ui.LayoutView(timeout=None)
    layout.add_item(discord.ui.Container(*items, accent_color=_accent(view)))
    return layout


def build_turn_ping(view: GameView, links: dict[str, str], game_number: int) -> tuple[str, list[int]] | None:
    """The ping message for the current turn and the Discord users it mentions,
    or ``None`` when no linked player needs pinging."""
    user_ids: list[int] = []
    for player_id in view.players_to_act():
        discord_user_id = links.get(player_id)
        if discord_user_id and discord_user_id.isdigit() and int(discord_user_id) not in user_ids:
            user_ids.append(int(discord_user_id))
    if not user_ids:
        return None
    mentions = " ".join(f"<@{user_id}>" for user_id in user_ids)
    team = team_label(view.active_team)
    if view.active_role == "spymasters":
        text = tr("cn_ping_clue", mentions=mentions, team=team, number=game_number)
    elif view.clue is not None:
        text = tr(
            "cn_ping_guess",
            mentions=mentions,
            team=team,
            number=game_number,
            word=_escape(view.clue.word),
            count=view.clue.number,
        )
    else:
        text = tr("cn_ping_guess_no_clue", mentions=mentions, team=team, number=game_number)
    return text, user_ids


def _status_line(data: CardData) -> str:
    view = data.view
    if view is None:
        return tr("cn_status_over") if data.closed_note else tr("cn_status_connecting")
    if view.phase == "lobby":
        return tr("cn_status_lobby")
    if view.phase == "finished":
        line = tr("cn_status_won", team=team_label(view.winner_team)) if view.winner_team else tr("cn_status_over")
        if view.finish_reason == "assassin":
            loser = next((team for team in TEAMS if team != view.winner_team), None)
            line += "\n" + tr("cn_reason_assassin", team=team_label(loser))
        return line
    team = team_label(view.active_team)
    if view.active_role == "spymasters":
        return tr("cn_status_clue", team=team)
    if view.clue is not None:
        line = tr("cn_status_guessing", team=team, word=_escape(view.clue.word), count=view.clue.number)
        if view.guesses_left is not None:
            line += " · " + tr("cn_guesses_left", count=view.guesses_left)
        return line
    return tr("cn_status_guessing_no_clue", team=team)


def _team_block(view: GameView, team: str, links: dict[str, str]) -> str:
    lines = [f"**{team_label(team)}**"]
    for role in ROLES:
        ids = view.rosters.get(team, {}).get(role, [])
        names = ", ".join(_player_label(view, pid, links) for pid in ids) if ids else f"_{tr('cn_role_needed')}_"
        acting = view.active_team == team and view.active_role == role and view.phase == "in_progress"
        marker = "👉 " if acting else ""
        lines.append(f"{marker}{tr(_ROLE_KEYS[role])}: {names}")
    return "\n".join(lines)


def _player_label(view: GameView, player_id: str, links: dict[str, str]) -> str:
    label = _escape(view.player_name(player_id))
    discord_user_id = links.get(player_id)
    return f"{label} (<@{discord_user_id}>)" if discord_user_id else label


def _visible_player_ids(view: GameView) -> list[str]:
    ids = {pid for roles in view.rosters.values() for members in roles.values() for pid in members}
    return sorted(ids | set(view.spectator_ids))


def _accent(view: GameView | None) -> discord.Color:
    if view is None or view.phase == "lobby":
        return discord.Color.light_grey()
    if view.phase == "finished":
        return TEAM_COLOR.get(view.winner_team or "", discord.Color.dark_grey())
    return TEAM_COLOR.get(view.active_team or "", discord.Color.blurple())


def _escape(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))
