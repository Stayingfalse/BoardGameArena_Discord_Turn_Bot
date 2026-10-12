"""Follow a Codenames match from socket events and summarise it.

:class:`MatchTracker` holds the boardgame.io state for one match, applies
``sync``/``patch``/``matchData`` events, and builds a :class:`GameView`: what
the Discord card shows and who to ping. No I/O.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .jsonpatch import JsonPatchError, apply_patch

TEAMS = ("red", "blue")
ROLES = ("spymasters", "operatives")


@dataclass(frozen=True, slots=True)
class Clue:
    word: str
    number: str
    team: str | None
    giver_name: str | None


@dataclass(frozen=True, slots=True)
class Guess:
    word: str
    card_color: str | None
    team: str | None
    player_name: str | None


@dataclass(frozen=True, slots=True)
class BoardCard:
    word: str
    # "red", "blue", "neutral" or "black"; ``None`` while hidden from spectators.
    color: str | None
    revealed: bool


@dataclass(slots=True)
class ClueTurn:
    """One clue and the guesses made on it, in order."""

    clue: Clue
    guesses: list[Guess] = field(default_factory=list)


@dataclass(slots=True)
class GameView:
    match_id: str
    phase: str  # "lobby", "in_progress" or "finished"
    active_team: str | None
    active_role: str | None
    # team -> role -> player IDs
    rosters: dict[str, dict[str, list[str]]]
    player_names: dict[str, str]
    remaining: dict[str, int]
    starting_team: str | None
    clue: Clue | None
    last_guess: Guess | None
    guesses_left: int | None
    winner_team: str | None
    finish_reason: str | None
    bot_player_id: str | None = None
    spectator_ids: list[str] = field(default_factory=list)
    # Players (not the bot) with the room open right now.
    connected_ids: list[str] = field(default_factory=list)
    # boardgame.io's state counter: changes with every move.
    state_id: int | None = None
    # The 25 cards in grid order. When the game ends the server shows
    # spectators the whole key, so every card has its colour then.
    board: tuple[BoardCard, ...] = ()
    # Every clue of the match so far, with its guesses.
    turns: tuple[ClueTurn, ...] = ()

    @property
    def turn_key(self) -> str | None:
        """``"<team>:<role>"`` while a game is running, else ``None``. Pings fire when it changes."""
        if self.phase != "in_progress" or not self.active_team or not self.active_role:
            return None
        return f"{self.active_team}:{self.active_role}"

    def players_to_act(self) -> list[str]:
        """Player IDs whose move it is: the active team's spymasters, or its operatives."""
        if self.turn_key is None:
            return []
        return list(self.rosters.get(self.active_team or "", {}).get(self.active_role or "", []))

    def player_name(self, player_id: str) -> str:
        return self.player_names.get(player_id) or f"Player {player_id}"


class MatchTracker:
    """State of one match as seen by a spectator."""

    def __init__(self, match_id: str, bot_player_id: str | None = None) -> None:
        self.match_id = match_id
        self.bot_player_id = bot_player_id
        self.state: dict[str, Any] | None = None
        self.player_names: dict[str, str] = {}
        self.connected_ids: set[str] = set()
        self._clue: Clue | None = None
        self._last_guess: Guess | None = None
        self._turns: list[ClueTurn] = []

    @property
    def state_id(self) -> int | None:
        return None if self.state is None else self.state.get("_stateID")

    def apply_sync(self, payload: dict[str, Any]) -> None:
        """Replace everything with a full ``sync`` payload."""
        self.state = copy.deepcopy(payload.get("state") or {})
        self._clue = None
        self._last_guess = None
        self._turns = []
        self._read_log(payload.get("log") or [])
        self.apply_match_data(payload.get("filteredMetadata") or [])

    def apply_patch(self, prev_state_id: int, state_id: int, operations: list[dict], log_entries: list[dict]) -> bool:
        """Apply a ``patch`` event. Returns ``False`` when it doesn't follow on
        from our state (or fails to apply): the caller should re-``sync``."""
        if self.state is None or self.state_id != prev_state_id:
            return False
        try:
            patched = apply_patch(copy.deepcopy(self.state), operations)
        except JsonPatchError:
            return False
        if not isinstance(patched, dict) or patched.get("_stateID") != state_id:
            return False
        self.state = patched
        self._read_log(log_entries)
        return True

    def apply_match_data(self, players: list[dict[str, Any]]) -> None:
        for player in players:
            player_id = str(player.get("id"))
            name = player.get("name")
            if name:
                self.player_names[player_id] = str(name)
            if player.get("isConnected"):
                self.connected_ids.add(player_id)
            else:
                self.connected_ids.discard(player_id)

    def _read_log(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            payload = (entry.get("action") or {}).get("payload") or {}
            move = payload.get("type")
            args = payload.get("args") or []
            metadata = entry.get("metadata") or {}
            if move == "giveClue" and args and isinstance(args[0], dict):
                giver = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
                self._clue = Clue(
                    word=str(args[0].get("word", "")),
                    number=str(args[0].get("number", "")),
                    team=giver.get("team") or metadata.get("playerTeam"),
                    giver_name=giver.get("name") or metadata.get("playerName"),
                )
                self._turns.append(ClueTurn(self._clue))
            elif move == "guessCard" and args:
                guesser = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
                self._last_guess = Guess(
                    word=str(args[0]),
                    card_color=metadata.get("cardColor"),
                    team=guesser.get("team") or metadata.get("playerTeam"),
                    player_name=guesser.get("name") or metadata.get("playerName"),
                )
                if self._turns:
                    self._turns[-1].guesses.append(self._last_guess)
            elif move == "startMatch":
                self._clue = None
                self._last_guess = None
                self._turns = []

    def view(self) -> GameView | None:
        if self.state is None:
            return None
        g = self.state.get("G") or {}
        ctx = self.state.get("ctx") or {}

        rosters: dict[str, dict[str, list[str]]] = {}
        on_team: set[str] = set()
        for team in TEAMS:
            rosters[team] = {}
            roles = (g.get("teams") or {}).get(team) or {}
            for role in ROLES:
                ids = sorted(
                    (key.removeprefix("p#") for key, present in (roles.get(role) or {}).items() if present),
                    key=_id_sort_key,
                )
                rosters[team][role] = ids
                on_team.update(ids)

        gameover = g.get("gameover") or ctx.get("gameover")
        if gameover:
            phase = "finished"
        elif g.get("cardsDealed") and ctx.get("phase") == "game":
            phase = "in_progress"
        else:
            phase = "lobby"

        remaining = {team: 0 for team in TEAMS}
        for card in g.get("coverCards") or []:
            if card.get("color") in remaining and card.get("location") != "grid":
                remaining[card["color"]] += 1

        guess_limit = g.get("guessLimit")
        in_progress = phase == "in_progress"
        clue_is_live = in_progress and g.get("activeRole") == "operatives"
        spectators = sorted(
            (pid for pid in self.player_names if pid not in on_team and pid != self.bot_player_id),
            key=_id_sort_key,
        )
        return GameView(
            match_id=self.match_id,
            phase=phase,
            active_team=g.get("activeTeam") if in_progress else None,
            active_role=g.get("activeRole") if in_progress else None,
            rosters=rosters,
            player_names=dict(self.player_names),
            remaining=remaining,
            starting_team=g.get("startingTeam"),
            clue=self._clue if clue_is_live else None,
            last_guess=self._last_guess if phase != "lobby" else None,
            guesses_left=guess_limit if clue_is_live and isinstance(guess_limit, int) and guess_limit >= 0 else None,
            winner_team=(gameover or {}).get("team") if isinstance(gameover, dict) else None,
            finish_reason=(gameover or {}).get("reason") if isinstance(gameover, dict) else None,
            bot_player_id=self.bot_player_id,
            spectator_ids=spectators,
            connected_ids=sorted((pid for pid in self.connected_ids if pid != self.bot_player_id), key=_id_sort_key),
            state_id=self.state_id,
            board=tuple(
                BoardCard(
                    word=str(card.get("word", "")),
                    color=card.get("color"),
                    revealed=bool(card.get("revealed")),
                )
                for card in g.get("grid") or []
                if isinstance(card, dict)
            ),
            turns=tuple(ClueTurn(turn.clue, list(turn.guesses)) for turn in self._turns),
        )


def _id_sort_key(player_id: str) -> tuple[int, str]:
    return (int(player_id), "") if player_id.isdigit() else (1 << 30, player_id)
