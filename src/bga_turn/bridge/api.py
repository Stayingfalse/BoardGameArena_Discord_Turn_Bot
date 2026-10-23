"""HTTP API the tournament bot calls to bind its match threads.

Routes live under ``/api/bridge/`` and need ``Authorization: Bearer <token>``.
They run on their own small server (``BRIDGE_PORT``, reached over the Docker
network) and, when the dashboard is enabled, on the dashboard too, so its
public HTTPS address works as a fallback. Every response is JSON.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

from .service import TournamentBridge, binding_json

LOGGER = logging.getLogger(__name__)

PREFIX = "/api/bridge"
_BRIDGE_KEY = web.AppKey("tournament_bridge", TournamentBridge)
_TOKEN_KEY = web.AppKey("tournament_bridge_token", str)
_MAX_PLAYERS = 10
_MAX_REF_LENGTH = 100
_MAX_NAME_LENGTH = 100


def register_bridge_routes(app: web.Application, bridge: TournamentBridge, *, token: str) -> None:
    if not token:
        raise ValueError("The tournament bridge needs a token.")
    app[_BRIDGE_KEY] = bridge
    app[_TOKEN_KEY] = token
    app.router.add_get(f"{PREFIX}/health", _health)
    app.router.add_get(PREFIX + "/codenames/bindings/{channel_id}", _get_binding)
    app.router.add_put(PREFIX + "/codenames/bindings/{channel_id}", _put_binding)
    app.router.add_delete(PREFIX + "/codenames/bindings/{channel_id}", _delete_binding)
    app.router.add_post(PREFIX + "/codenames/bindings/{channel_id}/redeliver", _redeliver)


def create_bridge_app(bridge: TournamentBridge, *, token: str) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    register_bridge_routes(app, bridge, token=token)
    return app


async def start_bridge_server(app: web.Application, *, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    LOGGER.info("Tournament bridge listening on http://%s:%d%s", host, port, PREFIX)
    return runner


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _authorized(request: web.Request) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    return hmac.compare_digest(supplied.strip().encode(), request.app[_TOKEN_KEY].encode())


def _channel_id(request: web.Request) -> str | None:
    channel_id = request.match_info["channel_id"]
    return channel_id if channel_id.isdecimal() else None


async def _health(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _error(401, "unauthorized")
    return web.json_response({"ok": True})


async def _get_binding(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _error(401, "unauthorized")
    channel_id = _channel_id(request)
    if channel_id is None:
        return _error(400, "channel_id must be a Discord snowflake")
    bridge = request.app[_BRIDGE_KEY]
    binding = await bridge.binding_for(channel_id)
    if binding is None:
        return _error(404, "not bound")
    return web.json_response(await bridge.describe(binding))


async def _redeliver(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _error(401, "unauthorized")
    channel_id = _channel_id(request)
    if channel_id is None:
        return _error(400, "channel_id must be a Discord snowflake")
    return web.json_response({"requeued": await request.app[_BRIDGE_KEY].redeliver(channel_id)})


async def _put_binding(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _error(401, "unauthorized")
    channel_id = _channel_id(request)
    if channel_id is None:
        return _error(400, "channel_id must be a Discord snowflake")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "body must be JSON")
    if not isinstance(body, dict):
        return _error(400, "body must be a JSON object")

    guild_id = str(body.get("guild_id") or "")
    external_ref = body.get("external_ref")
    players = body.get("players") or []
    if not guild_id.isdecimal():
        return _error(400, "guild_id must be a Discord snowflake")
    if not isinstance(external_ref, str) or not external_ref.strip() or len(external_ref) > _MAX_REF_LENGTH:
        return _error(400, f"external_ref must be a non-empty string of at most {_MAX_REF_LENGTH} characters")
    if (
        not isinstance(players, list)
        or len(players) > _MAX_PLAYERS
        or not all(isinstance(p, (str, int)) and str(p).isdecimal() for p in players)
    ):
        return _error(400, f"players must be a list of at most {_MAX_PLAYERS} Discord user IDs")

    player_names = body.get("player_names") or {}
    if not isinstance(player_names, dict) or len(player_names) > _MAX_PLAYERS or not all(
        str(k).isdecimal() and isinstance(v, str) and len(v) <= _MAX_NAME_LENGTH for k, v in player_names.items()
    ):
        return _error(400, f"player_names must map up to {_MAX_PLAYERS} Discord user IDs to names")
    lineup = body.get("lineup")
    if lineup is not None:
        lineup = _parse_lineup(lineup)
        if lineup is None:
            return _error(
                400,
                'lineup must be {"game": int, "last_event_id": str|null, '
                '"blue": {"spymaster": id, "operative": id}, "red": {...}} or null',
            )

    deadline = body.get("deadline")
    if deadline is not None and (not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0):
        return _error(400, "deadline must be a unix timestamp in seconds, or null")

    bridge = request.app[_BRIDGE_KEY]
    binding = await bridge.bind(
        channel_id,
        guild_id=guild_id,
        external_ref=external_ref.strip(),
        players=list(dict.fromkeys(str(p) for p in players)),
        player_names={str(k): v for k, v in player_names.items()},
        lineup=lineup,
        deadline=deadline,
    )
    return web.json_response(binding_json(binding))


def _parse_lineup(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    game = raw.get("game")
    last_event_id = raw.get("last_event_id")
    if not isinstance(game, int) or game < 1:
        return None
    if last_event_id is not None and (not isinstance(last_event_id, str) or len(last_event_id) > 200):
        return None
    lineup: dict = {"game": game, "last_event_id": last_event_id}
    for team in ("blue", "red"):
        seats = raw.get(team)
        if not isinstance(seats, dict):
            return None
        lineup[team] = {}
        for role in ("spymaster", "operative"):
            user_id = seats.get(role)
            if not isinstance(user_id, (str, int)) or not str(user_id).isdecimal():
                return None
            lineup[team][role] = str(user_id)
    return lineup


async def _delete_binding(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _error(401, "unauthorized")
    channel_id = _channel_id(request)
    if channel_id is None:
        return _error(400, "channel_id must be a Discord snowflake")
    keep_watching = request.query.get("keep_watching", "").lower() in {"1", "true", "yes"}
    removed = await request.app[_BRIDGE_KEY].unbind(channel_id, keep_watching=keep_watching)
    return web.json_response({"removed": removed})
