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
_ROLE_EMOJI = {"spymasters": "🧠", "operatives": "🔎"}


@dataclass(frozen=True, slots=True)
class Lineup:
    """The seats a tournament expects for the current game."""

    game: int
    # (team, role, Discord user ID), role being "spymasters" or "operatives".
    seats: tuple[tuple[str, str, str], ...]

    @classmethod
    def from_binding(cls, raw: dict | None) -> Lineup | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("game"), int):
            return None
        seats = []
        for team in TEAMS:
            for role, key in (("spymasters", "spymaster"), ("operatives", "operative")):
                user_id = (raw.get(team) or {}).get(key)
                if not user_id:
                    return None
                seats.append((team, role, str(user_id)))
        return cls(game=raw["game"], seats=tuple(seats))

    @property
    def key(self) -> str:
        return f"{self.game}:" + ",".join("/".join(seat) for seat in self.seats)


@dataclass(frozen=True, slots=True)
class LineupCheck:
    team: str
    role: str
    user_id: str
    # "ok", "wrong_seat" (then *actual* is (team, role)), "spectating" or "unclaimed".
    status: str
    actual: tuple[str, str] | None = None


def check_lineup(view: GameView, lineup: Lineup, links: dict[str, str]) -> tuple[list[LineupCheck], list[tuple[str, str, list[str]]]]:
    """Each expected seat's state, plus seats holding players who shouldn't be there
    (``(team, role, seat IDs)``)."""
    seats_of_user: dict[str, list[str]] = {}  # one user can be in the room on several devices
    for pid, user_id in links.items():
        seats_of_user.setdefault(user_id, []).append(pid)
    role_of_seat = {
        pid: (team, role) for team, roles in view.rosters.items() for role, ids in roles.items() for pid in ids
    }
    checks: list[LineupCheck] = []
    for team, role, user_id in lineup.seats:
        pids = seats_of_user.get(user_id)
        roles = [role_of_seat[pid] for pid in pids or () if pid in role_of_seat]
        if not pids:
            checks.append(LineupCheck(team, role, user_id, "unclaimed"))
        elif (team, role) in roles:
            checks.append(LineupCheck(team, role, user_id, "ok"))
        elif roles:
            checks.append(LineupCheck(team, role, user_id, "wrong_seat", roles[0]))
        else:
            checks.append(LineupCheck(team, role, user_id, "spectating"))
    expected = {(team, role): user_id for team, role, user_id in lineup.seats}
    extras = []
    for team in TEAMS:
        for role in ROLES:
            others = [
                pid for pid in view.rosters.get(team, {}).get(role, []) if links.get(pid) != expected.get((team, role))
            ]
            if others:
                extras.append((team, role, others))
    return checks, extras


def lineup_ready(view: GameView, lineup: Lineup, links: dict[str, str]) -> bool:
    checks, extras = check_lineup(view, lineup, links)
    return not extras and all(check.status == "ok" for check in checks)


def seat_label(team: str, role: str) -> str:
    return f"{TEAM_EMOJI.get(team, '')} {tr(_ROLE_KEYS[role])}"


def build_lineup_ping(lineup: Lineup) -> tuple[str, list[int]]:
    rows = []
    for team in TEAMS:
        rows.append(" · ".join(f"{seat_label(t, r)} <@{u}>" for t, r, u in lineup.seats if t == team))
    user_ids = [int(u) for _, _, u in lineup.seats if u.isdigit()]
    return tr("cn_lineup_ping", game=lineup.game, seats="\n".join(rows)), list(dict.fromkeys(user_ids))


def _lineup_block(view: GameView, lineup: Lineup, links: dict[str, str]) -> str:
    checks, extras = check_lineup(view, lineup, links)
    if not extras and all(check.status == "ok" for check in checks):
        return tr("cn_lineup_ready", game=lineup.game)
    icons = {"ok": "✅", "wrong_seat": "⚠️", "spectating": "⏳", "unclaimed": "⏳"}
    lines = [tr("cn_lineup_title", game=lineup.game)]
    for check in checks:
        line = f"{icons[check.status]} {seat_label(check.team, check.role)}: <@{check.user_id}>"
        if check.status == "wrong_seat" and check.actual:
            line += " · " + tr("cn_lineup_wrong_seat", seat=seat_label(*check.actual))
        elif check.status == "spectating":
            line += " · " + tr("cn_lineup_spectating")
        elif check.status == "unclaimed":
            line += " · " + tr("cn_lineup_unclaimed")
        lines.append(line)
    for team, role, pids in extras:
        names = ", ".join(_player_label(view, pid, links) for pid in pids)
        lines.append(tr("cn_lineup_extra", seat=seat_label(team, role), names=names))
    return "\n".join(lines)


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
    # File name of the final-board image sent with the card, once the game is over.
    board_image: str | None = None
    # Seats a tournament expects for this game (only shown before the game ends).
    lineup: Lineup | None = None
    # Thread the turn pings go to, when not this channel.
    pings_channel_id: str | None = None


def card_signature(data: CardData) -> str:
    """Changes whenever the rendered card would."""
    view = data.view
    parts: list[object] = [
        data.room_id,
        data.game_number,
        sorted(data.links.items()),
        data.invited,
        data.closed_note,
        data.board_image,
        data.lineup,
        data.pings_channel_id,
    ]
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

    # What the card shows depends on the phase: where to sit in the lobby, whose turn it
    # is and the score during the game, the result and the board once it's over.
    if view is not None and view.phase != "lobby":
        items.append(discord.ui.TextDisplay(_score_line(view)))

    if data.board_image:
        items.append(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(f"attachment://{data.board_image}", description=tr("cn_board_image_alt"))
            )
        )

    open_game = data.closed_note is None and view is not None and view.phase != "finished"
    lineup_shown = data.lineup is not None and open_game
    items.append(discord.ui.Separator())
    if view is None:
        if data.closed_note is None:
            items.append(discord.ui.TextDisplay(tr("cn_connecting")))
    elif view.phase == "lobby":
        if lineup_shown:
            items.append(discord.ui.TextDisplay(_lineup_block(view, data.lineup, data.links)))
        else:
            for team in TEAMS:
                items.append(discord.ui.TextDisplay(_team_block(view, team, data.links)))
    else:
        items.append(discord.ui.TextDisplay(_compact_teams(view, data.links)))
        if lineup_shown and not lineup_ready(view, data.lineup, data.links):
            items.append(discord.ui.TextDisplay(_lineup_block(view, data.lineup, data.links)))

    if view is not None and open_game:
        watching = _watching(view, data.links)
        if watching:
            items.append(discord.ui.TextDisplay(tr("cn_watching", count=watching)))

    lobby = view is None or view.phase == "lobby"
    if data.invited and lobby and data.closed_note is None:
        mentions = " ".join(f"<@{user_id}>" for user_id in data.invited)
        items.append(discord.ui.TextDisplay(tr("cn_invited", mentions=mentions)))

    if data.pings_channel_id and data.closed_note is None and (view is None or view.phase != "finished"):
        items.append(discord.ui.TextDisplay(tr("cn_pings_in_thread", thread=f"<#{data.pings_channel_id}>")))

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


def build_turn_ping(
    view: GameView, links: dict[str, str], game_number: int, *, branded: bool = False
) -> tuple[str, list[int]] | None:
    """The ping message for the current turn and the Discord users it mentions,
    or ``None`` when no linked player needs pinging. *branded* says "Codenames"
    in the text, for pings the bot sends as itself rather than as the webhook."""
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
    if branded:
        text = tr("cn_ping_branded", text=text)
    return text, user_ids


def format_wait(seconds: int) -> str:
    """'3 hours', '1 day', '2 days' (rounded down)."""
    hours = max(1, seconds // 3600)
    if hours < 48:
        return tr("cn_wait_hours", count=hours) if hours != 1 else tr("cn_wait_hour")
    return tr("cn_wait_days", count=hours // 24)


def build_reminder_note(waited_seconds: int, deadline: int | None = None) -> str:
    """The line put above a repeated turn ping."""
    note = tr("cn_reminder", waited=format_wait(waited_seconds))
    if deadline:
        note += " " + tr("cn_reminder_deadline", deadline=f"<t:{deadline}:R>")
    return note


def build_ping_view(room_id: str) -> discord.ui.View:
    """An "Open room" link under a turn ping, so players needn't scroll back to the card."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=tr("cn_button_open_room"), url=build_room_url(room_id)))
    return view


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
    team = TEAM_EMOJI.get(view.active_team or "", "")
    names = _acting_names(view, data.links) or team_label(view.active_team)
    if view.active_role == "spymasters":
        return tr("cn_status_clue", team=team, names=names)
    if view.clue is not None:
        line = tr("cn_status_guessing", team=team, names=names, word=_escape(view.clue.word), count=view.clue.number)
        if view.guesses_left is not None:
            line += " · " + tr("cn_guesses_left", count=view.guesses_left)
        return line
    return tr("cn_status_guessing_no_clue", team=team)


def _score_line(view: GameView) -> str:
    line = f"{TEAM_EMOJI['red']} {view.remaining.get('red', 0)} · {TEAM_EMOJI['blue']} {view.remaining.get('blue', 0)}"
    if view.last_guess is not None and view.phase == "in_progress":
        line += " · " + tr(
            "cn_last_guess",
            player=_escape(view.last_guess.player_name or tr("value_unknown")),
            word=_escape(view.last_guess.word),
            card=CARD_EMOJI.get(view.last_guess.card_color or "", "❔"),
        )
    return line


def _compact_teams(view: GameView, links: dict[str, str]) -> str:
    """One line per team, names only: "🔴 🧠 Jojo · 🔎 Morgan"."""
    lines = []
    for team in TEAMS:
        parts = []
        for role in ROLES:
            shown = _one_seat_per_user(view.rosters.get(team, {}).get(role, []), links)
            names = ", ".join(_escape(view.player_name(pid)) for pid in shown) or f"_{tr('cn_role_needed')}_"
            acting = view.active_team == team and view.active_role == role and view.phase == "in_progress"
            parts.append(f"{'👉' if acting else ''}{_ROLE_EMOJI[role]} {names}")
        lines.append(f"{TEAM_EMOJI[team]} " + " · ".join(parts))
    return "\n".join(lines)


def _watching(view: GameView, links: dict[str, str]) -> int:
    """People in the room, not on a team and not linked to a player (players, and their
    other devices, are shown elsewhere on the card)."""
    return sum(1 for pid in view.spectator_ids if pid not in links)


def _acting_names(view: GameView, links: dict[str, str]) -> str:
    return ", ".join(_escape(view.player_name(pid)) for pid in _one_seat_per_user(view.players_to_act(), links))


def _team_block(view: GameView, team: str, links: dict[str, str]) -> str:
    lines = [f"**{team_label(team)}**"]
    for role in ROLES:
        ids = view.rosters.get(team, {}).get(role, [])
        shown = _one_seat_per_user(ids, links)
        names = ", ".join(_player_label(view, pid, links) for pid in shown) if shown else f"_{tr('cn_role_needed')}_"
        acting = view.active_team == team and view.active_role == role and view.phase == "in_progress"
        marker = "👉 " if acting else ""
        lines.append(f"{marker}{tr(_ROLE_KEYS[role])}: {names}")
    return "\n".join(lines)


def _one_seat_per_user(ids: list[str], links: dict[str, str]) -> list[str]:
    """*ids* without a player's other devices: the first seat of each Discord user is kept."""
    seen: set[str] = set()
    kept = []
    for pid in ids:
        user_id = links.get(pid)
        if user_id is None or user_id not in seen:
            kept.append(pid)
        if user_id is not None:
            seen.add(user_id)
    return kept


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
