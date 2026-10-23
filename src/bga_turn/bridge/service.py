"""Tournament bridge: report finished Codenames games to an external tournament bot.

A tournament bot binds a Discord channel (one match thread) to one of its
matches. Players post the codenames.game room link in that thread as usual;
the watch it starts invites the match's players, and every game that
finishes in the room is queued in ``codenames_result_outbox`` and POSTed to
the tournament's callback URLs (tried in order: internal first, public HTTPS
as fallback) until one accepts it. See ``docs/tournament-bridge.md``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

import aiohttp

from ..codenames.links import build_room_url
from ..codenames.models import CodenamesGame, CodenamesWatch
from ..codenames.monitor import CodenamesMonitor
from ..codenames.state import ROLES, TEAMS, GameView
from ..database import AsyncDatabase, Database
from ..utils import utc_now_iso
from .store import CodenamesBinding, OutboxEntry

LOGGER = logging.getLogger(__name__)

EVENT_GAME_FINISHED = "codenames.game_finished"

STATUS_DELIVERED = "delivered"
STATUS_REJECTED = "rejected"
STATUS_ABANDONED = "abandoned"
STATUS_PENDING = "pending"

# Client errors that are worth retrying (timeouts and rate limits); any other
# 4xx means the tournament refused the result and resending will not help.
_RETRYABLE_4XX = {408, 425, 429}


class TournamentBridge:
    def __init__(
        self,
        database: Database,
        monitor: CodenamesMonitor,
        *,
        token: str,
        callback_urls: list[str],
        request_timeout: float = 15.0,
        backoff_base: float = 15.0,
        backoff_max: float = 3600.0,
        max_attempts: int = 200,
        session_factory=aiohttp.ClientSession,
    ) -> None:
        self.db = AsyncDatabase(database)
        self.monitor = monitor
        self.token = token
        self.callback_urls = callback_urls
        self.request_timeout = request_timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_attempts = max_attempts
        self._session_factory = session_factory
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        monitor.game_finished_listeners.append(self.on_game_finished)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._delivery_loop(), name="tournament-bridge-delivery")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    async def bind(
        self,
        channel_id: str,
        *,
        guild_id: str,
        external_ref: str,
        players: list[str],
        player_names: dict[str, str] | None = None,
        lineup: dict | None = None,
        deadline: int | None = None,
    ) -> CodenamesBinding:
        """Bind *channel_id* to a tournament match; rooms already watched there pick it up too."""
        binding = await self.db.upsert_codenames_binding(
            channel_id,
            guild_id=guild_id,
            external_ref=external_ref,
            players=players,
            player_names=player_names,
            lineup=lineup,
            deadline=deadline,
        )
        for watch in await self.watches_in(channel_id, guild_id):
            await self.invite_players(watch, binding)
        return binding

    async def unbind(self, channel_id: str, *, keep_watching: bool = False) -> bool:
        binding = await self.db.get_codenames_binding(channel_id)
        if binding is None:
            return False
        if not keep_watching:
            for watch in await self.watches_in(channel_id, binding.guild_id):
                await self.monitor.unwatch(watch, outcome="cancelled")
        return await self.db.remove_codenames_binding(channel_id)

    async def binding_for(self, channel_id: str) -> CodenamesBinding | None:
        return await self.db.get_codenames_binding(channel_id)

    async def watches_in(self, channel_id: str, guild_id: str) -> list[CodenamesWatch]:
        return [w for w in await self.db.list_codenames_watches_for_guild(guild_id) if w.channel_id == channel_id]

    async def invite_players(self, watch: CodenamesWatch, binding: CodenamesBinding) -> None:
        """List the match's players on the room's card so they can claim their seats."""
        if binding.players:
            await self.db.add_codenames_watch_members(watch.watch_id, binding.players)
        # Redraw even without new players: the line-up may have moved on to the next game.
        await self.monitor.refresh_room(watch.room_id)

    async def describe(self, binding: CodenamesBinding) -> dict:
        """The binding plus what is happening in each of the thread's rooms right now."""
        rooms = [await self._room_status(watch) for watch in await self.watches_in(binding.channel_id, binding.guild_id)]
        results = [
            {"event_id": entry.event_id, "status": entry.status, "attempts": entry.attempts}
            for entry in await self.db.list_bridge_results(binding.channel_id)
        ]
        return {**binding_json(binding), "rooms": rooms, "results": results}

    async def _room_status(self, watch: CodenamesWatch) -> dict:
        game = await self.db.get_latest_codenames_game(watch.watch_id)
        view = self.monitor.latest_view(watch.room_id)
        if view is not None and game is not None and view.match_id != game.match_id:
            view = None  # the room has moved on and this watch hasn't caught up yet
        status: dict = {
            "room_id": watch.room_id,
            "room_url": build_room_url(watch.room_id),
            "connected": self.monitor.is_watching(watch.room_id),
            "game_number": game.game_number if game else None,
            "phase": view.phase if view is not None else (game.lifecycle_state if game else None),
            "active_team": None,
            "active_role": None,
            "waiting_on": [],
            "turn_started_at": None,
            "clue": None,
            "remaining": dict(view.remaining) if view is not None else None,
            "players_in_room": len(view.connected_ids) if view is not None else 0,
        }
        if view is not None and view.phase == "in_progress":
            links = await self.monitor.seat_links(watch.room_id, view)
            status["active_team"] = view.active_team
            status["active_role"] = view.active_role
            waiting: list[dict] = []
            for pid in view.players_to_act():
                user_id = links.get(pid)
                if user_id is None or all(w["discord_user_id"] != user_id for w in waiting):  # one entry per person
                    waiting.append({"name": view.player_name(pid), "discord_user_id": user_id})
            status["waiting_on"] = waiting
            started = (game.state or {}).get("turn_started_at") if game else None
            if isinstance(started, (int, float)):
                status["turn_started_at"] = datetime.fromtimestamp(started, timezone.utc).isoformat()
            if view.clue is not None:
                status["clue"] = {"word": view.clue.word, "number": view.clue.number}
        return status

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    async def on_game_finished(
        self, watch: CodenamesWatch, game: CodenamesGame, view: GameView, links: dict[str, str]
    ) -> None:
        binding = await self.db.get_codenames_binding(watch.channel_id)
        if binding is None or view.winner_team not in TEAMS:
            return
        payload = build_result_payload(binding, watch, game, view, links)
        queued = await self.db.enqueue_bridge_result(
            payload["event_id"], channel_id=binding.channel_id, external_ref=binding.external_ref, payload=payload
        )
        if queued:
            LOGGER.info("Bridge: queued %s for %s", payload["event_id"], binding.external_ref)
            self._wake.set()

    async def redeliver(self, channel_id: str) -> int:
        """Queue every result already reported from *channel_id* to be sent again."""
        count = await self.db.requeue_bridge_results(channel_id)
        if count:
            LOGGER.info("Bridge: re-sending %d result(s) from channel %s", count, channel_id)
            self._wake.set()
        return count

    async def _delivery_loop(self) -> None:
        while True:
            try:
                await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Bridge: delivery pass failed")
            due = await self.db.next_bridge_result_due_at()
            timeout = 300.0 if due is None else max(1.0, due - time.time())
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def deliver_due(self) -> None:
        entries = await self.db.list_due_bridge_results(time.time())
        if not entries:
            return
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        async with self._session_factory(timeout=timeout) as session:
            for entry in entries:
                await self._deliver(session, entry)

    async def _deliver(self, session, entry: OutboxEntry) -> None:
        attempts = entry.attempts + 1
        errors: list[str] = []
        headers = {"Authorization": f"Bearer {self.token}", "Idempotency-Key": entry.event_id}
        for url in self.callback_urls:
            try:
                async with session.post(url, json=entry.payload, headers=headers) as response:
                    body = (await response.text())[:300]
                    if 200 <= response.status < 300:
                        await self.db.update_bridge_result(
                            entry.outbox_id, status=STATUS_DELIVERED, attempts=attempts
                        )
                        LOGGER.info("Bridge: delivered %s via %s", entry.event_id, url)
                        return
                    if 400 <= response.status < 500 and response.status not in _RETRYABLE_4XX:
                        await self.db.update_bridge_result(
                            entry.outbox_id,
                            status=STATUS_REJECTED,
                            attempts=attempts,
                            last_error=f"{url}: HTTP {response.status} {body}",
                        )
                        LOGGER.warning("Bridge: %s rejected by %s (HTTP %s)", entry.event_id, url, response.status)
                        return
                    errors.append(f"{url}: HTTP {response.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        error = "; ".join(errors) or "no callback URLs configured"
        if attempts >= self.max_attempts:
            await self.db.update_bridge_result(
                entry.outbox_id, status=STATUS_ABANDONED, attempts=attempts, last_error=error
            )
            LOGGER.error("Bridge: giving up on %s after %d attempts: %s", entry.event_id, attempts, error)
            return
        delay = min(self.backoff_max, self.backoff_base * 2 ** (attempts - 1)) * (0.75 + random.random() / 2)
        await self.db.update_bridge_result(
            entry.outbox_id,
            status=STATUS_PENDING,
            attempts=attempts,
            next_attempt_at=time.time() + delay,
            last_error=error,
        )
        LOGGER.warning("Bridge: delivery of %s failed (%s); retrying in %.0fs", entry.event_id, error, delay)


def binding_json(binding: CodenamesBinding) -> dict:
    return {
        "channel_id": binding.channel_id,
        "guild_id": binding.guild_id,
        "external_ref": binding.external_ref,
        "players": binding.players,
        "player_names": binding.player_names,
        "lineup": binding.lineup,
        "deadline": binding.deadline,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def build_result_payload(
    binding: CodenamesBinding, watch: CodenamesWatch, game: CodenamesGame, view: GameView, links: dict[str, str]
) -> dict:
    """The JSON body sent to the tournament for one finished game."""
    winner = view.winner_team
    loser = next((team for team in TEAMS if team != winner), None)
    seats = {
        team: {
            role: [
                {
                    "player_id": player_id,
                    "name": view.player_name(player_id),
                    "discord_user_id": links.get(player_id),
                }
                for player_id in view.rosters.get(team, {}).get(role, [])
            ]
            for role in ROLES
        }
        for team in TEAMS
    }
    colors = {card.word.casefold(): card.color for card in view.board}

    def person(player_id: str | None, name: str | None) -> dict:
        return {"player_id": player_id, "name": name, "discord_user_id": links.get(player_id) if player_id else None}

    turns = [
        {
            "team": turn.clue.team,
            "word": turn.clue.word,
            "number": turn.clue.number,
            "giver": person(turn.clue.giver_id, turn.clue.giver_name),
            "guesses": [
                {"word": guess.word, "color": colors.get(guess.word.casefold()), **person(guess.player_id, guess.player_name)}
                for guess in turn.guesses
            ],
        }
        for turn in view.turns
    ]
    return {
        "event": EVENT_GAME_FINISHED,
        "event_id": f"{watch.room_id}:{view.match_id}",
        "external_ref": binding.external_ref,
        "guild_id": binding.guild_id,
        "channel_id": binding.channel_id,
        "room_id": watch.room_id,
        "room_url": build_room_url(watch.room_id),
        "match_id": view.match_id,
        "game_number": game.game_number,
        "winner": winner,
        "finish_reason": view.finish_reason,
        "assassin": view.finish_reason == "assassin",
        "remaining": dict(view.remaining),
        "loser_remaining": view.remaining.get(loser, 0) if loser else None,
        "seats": seats,
        "starting_team": view.starting_team,
        "turns": turns,
        "board": [{"word": c.word, "color": c.color, "revealed": c.revealed} for c in view.board],
        "finished_at": utc_now_iso(),
    }
