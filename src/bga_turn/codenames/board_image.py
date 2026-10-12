"""Draw a finished Codenames game as a PNG: the board, teams, clues and score.

When a game ends the server shows spectators the whole key, so the final view
has every card's colour. :class:`BoardSnapshot` keeps what the picture needs
as plain JSON (stored with the game, so the image can be redrawn after a
restart or once the room has moved on), and :func:`render_board_png` draws
it. No I/O beyond reading the bundled fonts.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files

from PIL import Image, ImageDraw, ImageFont

from .state import ROLES, TEAMS, GameView

FILENAME = "codenames-board.png"

# Palette: strong fills for guessed cards, pale tints (with a coloured frame)
# for cards nobody guessed.
_BG = (244, 241, 234)
_INK = (33, 33, 33)
_MUTED = (110, 110, 110)
_SOLID = {"red": (208, 64, 58), "blue": (40, 102, 204), "neutral": (214, 196, 158), "black": (34, 34, 34)}
_TINT = {"red": (247, 219, 216), "blue": (219, 229, 248), "neutral": (241, 235, 221), "black": (92, 92, 92)}
_TEXT_ON = {"red": (255, 255, 255), "blue": (255, 255, 255), "neutral": (60, 50, 30), "black": (255, 255, 255)}
_TEXT_ON_TINT = {"red": (120, 30, 26), "blue": (24, 58, 120), "neutral": (90, 78, 52), "black": (255, 255, 255)}
_UNKNOWN = (200, 200, 200)
_TEAM_NAMES = {"red": "Red", "blue": "Blue"}
_ROLE_NAMES = {"spymasters": "Spymaster", "operatives": "Operatives"}

_W = 1500
_PAD = 36
_COLS = 5
_CARD_W, _CARD_H, _GAP = 180, 112, 12
_GRID_W = _COLS * _CARD_W + (_COLS - 1) * _GAP
_PANEL_X = _PAD + _GRID_W + 40
_PANEL_W = _W - _PANEL_X - _PAD
_HEADER_H = 120


@dataclass(slots=True)
class SnapshotGuess:
    word: str
    player: str | None


@dataclass(slots=True)
class SnapshotTurn:
    team: str | None
    word: str
    number: str
    giver: str | None
    guesses: list[SnapshotGuess] = field(default_factory=list)


@dataclass(slots=True)
class BoardSnapshot:
    game_number: int
    winner: str | None
    reason: str | None
    starting_team: str | None
    remaining: dict[str, int]
    # (word, colour or None, revealed) in grid order.
    cards: list[tuple[str, str | None, bool]]
    turns: list[SnapshotTurn]
    # team -> role -> display names
    rosters: dict[str, dict[str, list[str]]]

    @classmethod
    def from_view(cls, view: GameView, game_number: int) -> BoardSnapshot:
        return cls(
            game_number=game_number,
            winner=view.winner_team,
            reason=view.finish_reason,
            starting_team=view.starting_team,
            remaining=dict(view.remaining),
            cards=[(card.word, card.color, card.revealed) for card in view.board],
            turns=[
                SnapshotTurn(
                    team=turn.clue.team,
                    word=turn.clue.word,
                    number=turn.clue.number,
                    giver=turn.clue.giver_name,
                    guesses=[SnapshotGuess(guess.word, guess.player_name) for guess in turn.guesses],
                )
                for turn in view.turns
            ],
            rosters={
                team: {role: [view.player_name(pid) for pid in view.rosters.get(team, {}).get(role, [])] for role in ROLES}
                for team in TEAMS
            },
        )

    def to_dict(self) -> dict:
        return {
            "game_number": self.game_number,
            "winner": self.winner,
            "reason": self.reason,
            "starting_team": self.starting_team,
            "remaining": self.remaining,
            "cards": [list(card) for card in self.cards],
            "turns": [
                {
                    "team": t.team,
                    "word": t.word,
                    "number": t.number,
                    "giver": t.giver,
                    "guesses": [{"word": g.word, "player": g.player} for g in t.guesses],
                }
                for t in self.turns
            ],
            "rosters": self.rosters,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BoardSnapshot:
        return cls(
            game_number=int(data.get("game_number") or 0),
            winner=data.get("winner"),
            reason=data.get("reason"),
            starting_team=data.get("starting_team"),
            remaining={str(k): int(v) for k, v in (data.get("remaining") or {}).items()},
            cards=[(str(c[0]), c[1], bool(c[2])) for c in data.get("cards") or [] if len(c) == 3],
            turns=[
                SnapshotTurn(
                    team=t.get("team"),
                    word=str(t.get("word", "")),
                    number=str(t.get("number", "")),
                    giver=t.get("giver"),
                    guesses=[SnapshotGuess(str(g.get("word", "")), g.get("player")) for g in t.get("guesses") or []],
                )
                for t in data.get("turns") or []
            ],
            rosters={
                team: {role: [str(n) for n in names] for role, names in (roles or {}).items()}
                for team, roles in (data.get("rosters") or {}).items()
            },
        )


@lru_cache(maxsize=None)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    data = files("bga_turn").joinpath("static", "fonts", name).read_bytes()
    return ImageFont.truetype(io.BytesIO(data), size)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, size: int, minimum: int, bold: bool = True):
    while size > minimum and _text_width(draw, text, _font(size, bold)) > max_width:
        size -= 1
    return _font(size, bold)


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    while text and _text_width(draw, text + "…", font) > max_width:
        text = text[:-1]
    return text + "…"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: float) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if line and _text_width(draw, candidate, font) > max_width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return [_ellipsize(draw, line, font, max_width) for line in lines] or [""]


def _totals(snapshot: BoardSnapshot) -> dict[str, int]:
    """Agents per team: from the key when known, else the starting team has 9."""
    counted = {team: sum(1 for _, color, _ in snapshot.cards if color == team) for team in TEAMS}
    if all(counted.values()):
        return counted
    return {team: 9 if team == snapshot.starting_team else 8 for team in TEAMS}


def _result_lines(snapshot: BoardSnapshot) -> tuple[str, str]:
    winner = snapshot.winner
    if winner not in TEAMS:
        return "GAME OVER", ""
    loser = next(team for team in TEAMS if team != winner)
    if snapshot.reason == "assassin":
        detail = f"{_TEAM_NAMES[loser]} found the assassin"
    elif snapshot.remaining.get(winner, 1) == 0:
        detail = f"{_TEAM_NAMES[winner]} found all their agents"
    else:
        detail = ""
    return f"{_TEAM_NAMES[winner].upper()} WINS", detail


def _guess_order(snapshot: BoardSnapshot) -> dict[str, tuple[int, str | None]]:
    """Word -> (guess number, team that guessed it)."""
    order: dict[str, tuple[int, str | None]] = {}
    for turn in snapshot.turns:
        for guess in turn.guesses:
            key = guess.word.casefold()
            if key not in order:
                order[key] = (len(order) + 1, turn.team)
    return order


def _card_colors(snapshot: BoardSnapshot) -> dict[str, str | None]:
    return {word.casefold(): color for word, color, _ in snapshot.cards}


def render_board_png(snapshot: BoardSnapshot) -> bytes:
    # Measure the side panel first: it decides the image height when there are many clues.
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    panel_ops, panel_height = _layout_panel(scratch, snapshot)

    rows = max(1, -(-len(snapshot.cards) // _COLS))
    grid_h = rows * _CARD_H + (rows - 1) * _GAP
    body_top = _PAD + _HEADER_H
    legend_h = 44
    height = body_top + max(grid_h + legend_h, panel_height) + _PAD

    image = Image.new("RGB", (_W, height), _BG)
    draw = ImageDraw.Draw(image)
    _draw_header(draw, snapshot)
    _draw_grid(draw, snapshot, body_top)
    _draw_legend(draw, body_top + grid_h + 18)
    for op in panel_ops:
        op(draw, body_top)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_header(draw: ImageDraw.ImageDraw, snapshot: BoardSnapshot) -> None:
    title, detail = _result_lines(snapshot)
    accent = _SOLID.get(snapshot.winner or "", _INK)
    draw.text((_PAD, _PAD), f"CODENAMES · GAME {snapshot.game_number}", font=_font(20, True), fill=_MUTED)
    draw.text((_PAD, _PAD + 28), title, font=_font(48, True), fill=accent)
    if detail:
        x = _PAD + _text_width(draw, title, _font(48, True)) + 20
        draw.text((x, _PAD + 50), detail, font=_font(24), fill=_INK)

    # Score: agents found per team, right-aligned.
    totals = _totals(snapshot)
    x = _W - _PAD
    for team in reversed(TEAMS):
        found = totals[team] - snapshot.remaining.get(team, 0)
        label = f"{_TEAM_NAMES[team]}  {found}/{totals[team]}"
        font = _font(28, True)
        width = _text_width(draw, label, font) + 36
        draw.rounded_rectangle((x - width, _PAD + 30, x, _PAD + 80), radius=25, fill=_SOLID[team])
        draw.text((x - width / 2, _PAD + 55), label, font=font, fill=(255, 255, 255), anchor="mm")
        x -= width + 14
    draw.text((_W - _PAD, _PAD + 4), "agents found", font=_font(18), fill=_MUTED, anchor="ra")


def _draw_grid(draw: ImageDraw.ImageDraw, snapshot: BoardSnapshot, top: int) -> None:
    order = _guess_order(snapshot)
    for index, (word, color, revealed) in enumerate(snapshot.cards):
        row, col = divmod(index, _COLS)
        x0 = _PAD + col * (_CARD_W + _GAP)
        y0 = top + row * (_CARD_H + _GAP)
        box = (x0, y0, x0 + _CARD_W, y0 + _CARD_H)
        if color not in _SOLID:
            draw.rounded_rectangle(box, radius=12, fill=_UNKNOWN)
            text_fill = _INK
        elif revealed:
            draw.rounded_rectangle(box, radius=12, fill=_SOLID[color])
            text_fill = _TEXT_ON[color]
        else:
            draw.rounded_rectangle(box, radius=12, fill=_TINT[color], outline=_SOLID[color], width=5)
            text_fill = _TEXT_ON_TINT[color]
        if color == "black":
            draw.text((x0 + _CARD_W - 14, y0 + 10), "☠", font=_font(26, True), fill=(255, 255, 255), anchor="ra")

        font = _fit_font(draw, word, _CARD_W - 24, 26, 13)
        draw.text((x0 + _CARD_W / 2, y0 + _CARD_H / 2 + 6), word, font=font, fill=text_fill, anchor="mm")

        guessed = order.get(word.casefold())
        if revealed and guessed is not None:
            number, team = guessed
            badge = (x0 + 8, y0 + 8, x0 + 40, y0 + 40)
            draw.ellipse(badge, fill=(255, 255, 255), outline=_SOLID.get(team or "", _INK), width=3)
            draw.text((x0 + 24, y0 + 24), str(number), font=_font(16, True), fill=_INK, anchor="mm")


def _draw_legend(draw: ImageDraw.ImageDraw, y: int) -> None:
    x = _PAD
    font = _font(18)
    draw.rounded_rectangle((x, y, x + 34, y + 22), radius=5, fill=_SOLID["blue"])
    x += 44
    draw.text((x, y + 11), "guessed (number = order)", font=font, fill=_MUTED, anchor="lm")
    x += _text_width(draw, "guessed (number = order)", font) + 30
    draw.rounded_rectangle((x, y, x + 34, y + 22), radius=5, fill=_TINT["blue"], outline=_SOLID["blue"], width=3)
    x += 44
    draw.text((x, y + 11), "not guessed", font=font, fill=_MUTED, anchor="lm")


def _layout_panel(draw: ImageDraw.ImageDraw, snapshot: BoardSnapshot):
    """Return drawing operations for the side panel and its height."""
    ops = []
    y = 0
    heading = _font(20, True)
    body = _font(20)
    body_bold = _font(20, True)

    def text_op(dx, dy, text, font, fill, anchor="la"):
        ops.append(lambda d, top: d.text((_PANEL_X + dx, top + dy), text, font=font, fill=fill, anchor=anchor))

    for team in TEAMS:
        text_op(0, y, f"{_TEAM_NAMES[team].upper()} TEAM", heading, _SOLID[team])
        y += 30
        for role in ROLES:
            names = snapshot.rosters.get(team, {}).get(role, [])
            label = f"{_ROLE_NAMES[role]}: "
            for i, line in enumerate(_wrap(draw, ", ".join(names) or "—", body, _PANEL_W - _text_width(draw, label, body_bold))):
                if i == 0:
                    text_op(0, y, label, body_bold, _INK)
                text_op(_text_width(draw, label, body_bold), y, line, body, _INK)
                y += 26
        y += 14

    text_op(0, y, "CLUES", heading, _MUTED)
    y += 32
    colors = _card_colors(snapshot)
    chip_font = _font(16, True)
    for turn in snapshot.turns or []:
        team_fill = _SOLID.get(turn.team or "", _INK)
        dot_y = y + 11
        ops.append(
            lambda d, top, dot_y=dot_y, fill=team_fill: d.ellipse(
                (_PANEL_X, top + dot_y - 7, _PANEL_X + 14, top + dot_y + 7), fill=fill
            )
        )
        clue = f"{turn.word} · {turn.number}"
        giver = f"  {turn.giver}" if turn.giver else ""
        clue_text = _ellipsize(draw, clue, body_bold, _PANEL_W - 24)
        text_op(24, y, clue_text, body_bold, _INK)
        giver_x = 24 + _text_width(draw, clue_text, body_bold)
        if giver and giver_x < _PANEL_W - 40:
            text_op(giver_x, y + 2, _ellipsize(draw, giver, _font(17), _PANEL_W - giver_x), _font(17), _MUTED)
        y += 30
        # Guesses as chips coloured by the card they turned over.
        x = 24
        if not turn.guesses:
            text_op(24, y, "no guesses", _font(16), _MUTED)
            y += 26
        for guess in turn.guesses:
            color = colors.get(guess.word.casefold())
            word = _ellipsize(draw, guess.word, chip_font, _PANEL_W - 60)
            width = _text_width(draw, word, chip_font) + 20
            if x + width > _PANEL_W and x > 24:
                x = 24
                y += 30
            fill = _SOLID.get(color or "", _UNKNOWN)
            ink = _TEXT_ON.get(color or "", _INK)
            ops.append(
                lambda d, top, x=x, y=y, width=width, fill=fill, ink=ink, word=word: (
                    d.rounded_rectangle((_PANEL_X + x, top + y, _PANEL_X + x + width, top + y + 24), radius=12, fill=fill),
                    d.text((_PANEL_X + x + width / 2, top + y + 12), word, font=chip_font, fill=ink, anchor="mm"),
                )
            )
            x += width + 8
        if turn.guesses:
            y += 30
        y += 10
    if not snapshot.turns:
        text_op(0, y, "No clues were given.", body, _MUTED)
        y += 30
    return ops, y
