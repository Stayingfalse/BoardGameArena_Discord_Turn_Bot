"""Socket.IO connection to one codenames.game room, as a spectator.

Join flow (see ``docs/codenames-protocol.md``): connect to the lobby, emit
``sync("default", ...)`` with our room ID and credentials; the lobby answers
``updateServerURL`` with the game server holding the room, where the same
``sync`` yields ``createdMatch`` (match ID and our seat) and then the state.

Every (re)connect starts from the lobby with ``"default"``. That one path
resolves the current match after a restart, a dropped connection or a "play
again", so there is no separate recovery logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp
import socketio

from .state import GameView, MatchTracker

log = logging.getLogger(__name__)

LOBBY_URL = "https://server-discord.gke.codenames.game"
NAMESPACE = "/cno2"  # classic Codenames; Duet is "/cnduet"

# Why a room connection ended for good.
CLOSE_STOPPED = "stopped"
CLOSE_EXPIRED = "expired"
CLOSE_UNSUPPORTED = "unsupported"
CLOSE_FAILED = "failed"

ViewCallback = Callable[[GameView], Awaitable[None]]
ClosedCallback = Callable[[str], Awaitable[None]]


class CodenamesRoomClient:
    """Keeps one room connection alive and reports every state change."""

    def __init__(
        self,
        room_id: str,
        *,
        credentials: str,
        bot_name: str,
        on_view: ViewCallback,
        on_closed: ClosedCallback | None = None,
        lobby_url: str = LOBBY_URL,
        locale: str = "en",
        join_timeout: float = 30.0,
        max_failures: int = 8,
        backoff_base: float = 2.0,
        backoff_max: float = 300.0,
    ) -> None:
        self.room_id = room_id
        self.credentials = credentials
        self.bot_name = bot_name
        self.lobby_url = lobby_url.rstrip("/")
        self.locale = locale
        self.join_timeout = join_timeout
        self.max_failures = max_failures
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._on_view = on_view
        self._on_closed = on_closed
        self.tracker: MatchTracker | None = None
        self.player_id: str | None = None
        self._server_url = self.lobby_url
        self._move_to: str | None = None
        self._close_reason: str | None = None
        self._stopping = asyncio.Event()
        self._joined = asyncio.Event()
        self._connection_ended = asyncio.Event()
        self._sio: socketio.AsyncClient | None = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name=f"codenames-room-{self.room_id}")
        return self._task

    async def stop(self) -> None:
        self._close(CLOSE_STOPPED)
        if self._task is not None and self._task is not asyncio.current_task():
            await asyncio.gather(self._task, return_exceptions=True)

    async def run(self) -> None:
        failures = 0
        try:
            while self._close_reason is None:
                joined = await self._run_connection()
                if self._close_reason is not None:
                    break
                if self._move_to is not None:
                    # Lobby redirect to the room's game server: go straight there.
                    self._server_url, self._move_to = self._move_to, None
                    continue
                self._server_url = self.lobby_url
                failures = 0 if joined else failures + 1
                if failures >= self.max_failures:
                    self._close_reason = CLOSE_FAILED
                    break
                delay = min(self.backoff_max, self.backoff_base * 2 ** max(failures - 1, 0))
                delay *= 0.5 + random.random()
                log.info("Codenames room %s: reconnecting in %.0fs", self.room_id, delay)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            reason = self._close_reason or CLOSE_STOPPED
            log.info("Codenames room %s: closed (%s)", self.room_id, reason)
            if self._on_closed is not None:
                await self._on_closed(reason)

    def _close(self, reason: str) -> None:
        self._close_reason = self._close_reason or reason
        self._stopping.set()
        self._connection_ended.set()

    async def _run_connection(self) -> bool:
        """Connect once and stay until the connection ends; return whether we got into the room."""
        self._joined.clear()
        self._connection_ended.clear()
        parsed = urlparse(self._server_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        socket_path = f"{parsed.path.rstrip('/')}/socket.io"
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        async with aiohttp.ClientSession(trust_env=True) as http:
            sio = socketio.AsyncClient(
                reconnection=False,
                http_session=http,
                websocket_extra_options={"proxy": proxy} if proxy else {},
            )
            self._sio = sio
            self._register_handlers(sio)
            try:
                await sio.connect(origin, namespaces=[NAMESPACE], transports=["websocket"], socketio_path=socket_path)
                try:
                    await asyncio.wait_for(self._joined.wait(), timeout=self.join_timeout)
                except asyncio.TimeoutError:
                    if self._move_to is None:
                        log.warning("Codenames room %s: no state from %s", self.room_id, self._server_url)
                        return False
                if self._move_to is None:
                    await self._connection_ended.wait()
                return self._joined.is_set()
            except (socketio.exceptions.ConnectionError, aiohttp.ClientError, OSError) as error:
                log.warning("Codenames room %s: connection to %s failed: %s", self.room_id, self._server_url, error)
                return False
            finally:
                self._sio = None
                if sio.connected:
                    await sio.disconnect()

    # ------------------------------------------------------------------
    # Socket events
    # ------------------------------------------------------------------

    def _register_handlers(self, sio: socketio.AsyncClient) -> None:
        sio.on("connect", namespace=NAMESPACE, handler=self._on_connect)
        sio.on("disconnect", namespace=NAMESPACE, handler=self._on_disconnect)
        sio.on("*", namespace=NAMESPACE, handler=self._on_event)

    async def _on_connect(self) -> None:
        await self._request_sync("default", None)

    async def _on_disconnect(self, *_args) -> None:
        self._connection_ended.set()

    async def _request_sync(self, match_id: str, player_id: str | None) -> None:
        if self._sio is None:
            return
        info = {
            "playerName": self.bot_name,
            "playerImage": None,
            "playerLocale": self.locale,
            "roomID": self.room_id,
            "credentials": self.credentials,
        }
        await self._sio.emit("sync", (match_id, player_id, self.credentials, info), namespace=NAMESPACE)

    def _redirect(self, server_url: str | None) -> bool:
        """Leave this connection for *server_url* if it is another server."""
        if not server_url or server_url.rstrip("/") == self._server_url.rstrip("/"):
            return False
        self._move_to = server_url.rstrip("/")
        self._connection_ended.set()
        self._joined.set()
        return True

    async def _on_event(self, event: str, *args) -> None:
        try:
            await self._handle_event(event, args)
        except Exception:
            log.exception("Codenames room %s: error handling %r", self.room_id, event)

    async def _handle_event(self, event: str, args: tuple) -> None:
        if event == "updateServerURL":
            self._redirect(args[0] if args else None)
        elif event == "createdMatch":
            _room, match_id, player_id, _creds, server_url = (list(args) + [None] * 5)[:5]
            if self._redirect(server_url):
                return
            self.player_id = str(player_id) if player_id is not None else None
            if self.tracker is None or self.tracker.match_id != match_id:
                self.tracker = MatchTracker(str(match_id), bot_player_id=self.player_id)
            else:
                self.tracker.bot_player_id = self.player_id
            await self._request_sync(str(match_id), self.player_id)
        elif event == "sync":
            match_id, payload = args[0], args[1] if len(args) > 1 else {}
            if self.tracker is None or self.tracker.match_id != match_id:
                return
            if self._redirect((payload or {}).get("serverURL")):
                return
            self.tracker.apply_sync(payload or {})
            self._joined.set()
            await self._emit_view()
        elif event == "patch":
            match_id, prev_id, state_id, operations, log_entries = (list(args) + [None] * 5)[:5]
            if self.tracker is None or self.tracker.match_id != match_id:
                return
            if self.tracker.apply_patch(prev_id, state_id, operations or [], log_entries or []):
                await self._emit_view()
            else:
                log.info("Codenames room %s: state out of step, resyncing", self.room_id)
                await self._request_sync(self.tracker.match_id, self.player_id)
        elif event == "update":
            # Full state push (boardgame.io); resync to get log and metadata too.
            if self.tracker is not None and args and args[0] == self.tracker.match_id:
                await self._request_sync(self.tracker.match_id, self.player_id)
        elif event == "matchData":
            if self.tracker is not None and args and args[0] == self.tracker.match_id:
                self.tracker.apply_match_data(args[1] if len(args) > 1 else [])
                await self._emit_view()
        elif event == "playNextMatch":
            # "Play again": the room has a new match. Resolve it from the room.
            await self._request_sync("default", None)
        elif event == "room-timeout":
            self._close(CLOSE_EXPIRED)
        elif event == "wrongNamespace":
            # The room runs another game (Duet), which isn't supported yet.
            self._close(CLOSE_UNSUPPORTED)
        elif event == "sync-error":
            error = args[0] if args else {}
            log.warning("Codenames room %s: sync error %s", self.room_id, error)
            self._connection_ended.set()

    async def _emit_view(self) -> None:
        view = self.tracker.view() if self.tracker is not None else None
        if view is not None:
            await self._on_view(view)
