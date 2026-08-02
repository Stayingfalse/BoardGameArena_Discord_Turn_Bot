"""Onboarding web dashboard for BGA Turn Bot.

Serves on port 8080 (configurable via DASHBOARD_PORT).

Routes
------
GET  /                          Landing page with global stats and "Add to Server" button.
GET  /stats                     JSON: {total, recruiting} counts.
GET  /auth/login                Start Discord OAuth2 (user-only: identify + guilds).
GET  /auth/callback             Finish OAuth2, set session cookie, redirect to /dashboard.
GET  /dashboard                 Admin index: guilds where user has MANAGE_GUILD.
GET  /dashboard/{guild_id}      Per-guild stats + settings form.
POST /dashboard/{guild_id}/settings  Save per-guild settings.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from .app import BgaDiscordBot
    from .database import Database

LOGGER = logging.getLogger(__name__)

# Discord permission flag for MANAGE_GUILD
_MANAGE_GUILD = 1 << 5

_DISCORD_API = "https://discord.com/api/v10"
_OAUTH2_TOKEN_URL = "https://discord.com/api/v10/oauth2/token"
_OAUTH2_AUTH_URL = "https://discord.com/oauth2/authorize"

_COOKIE_NAME = "bga_session"
_COOKIE_MAX_AGE = 86400  # 1 day
_AUTH_MAX_ATTEMPTS = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_session_cookie(response: web.Response, session_id: str, *, secure: bool) -> None:
    response.set_cookie(
        _COOKIE_NAME,
        session_id,
        path="/",
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=secure,
    )


def _get_session(request: web.Request) -> tuple[dict[str, object] | None, str | None]:
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if not cookie:
        return None, "missing_session_cookie"
    if len(cookie) < 24:
        return None, "invalid_session_cookie"
    database: Database = request.app["database"]
    session = database.get_dashboard_session(cookie)
    if session is None:
        return None, "session_missing_or_expired"
    return session, None


def _safe_login_attempt(raw: str) -> int:
    try:
        return max(0, min(10, int(raw)))
    except (TypeError, ValueError):
        return 0


def _sanitize_next_path(raw: str) -> str:
    candidate = (raw or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/dashboard"
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/dashboard"
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))


def _append_query_value(path: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlsplit(path)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(item_key, item_value) for item_key, item_value in query if item_key != key]
    query.append((key, value))
    return urllib.parse.urlunsplit(("", "", parsed.path, urllib.parse.urlencode(query), ""))


def _auth_error_page(
    request: web.Request,
    *,
    session_reason: str,
    next_path: str,
    login_attempts: int,
    session: dict | None = None,
) -> str:
    reason_text = {
        "missing_session_cookie": "No dashboard session cookie was received.",
        "invalid_session_cookie": "Dashboard session cookie format is invalid.",
        "session_missing_or_expired": "Dashboard session cookie does not match a valid server session.",
    }.get(session_reason, "Dashboard authentication state is invalid.")
    return _render_template(
        request,
        "auth_error.html",
        title="Dashboard Login Error",
        session=session,
        reason_text=reason_text,
        next_path=next_path,
        login_attempts=login_attempts,
        quoted_next_path=urllib.parse.quote(next_path, safe="/?=&"),
    )


def _raise_auth_redirect_or_error(request: web.Request, *, session_reason: str) -> None:
    login_attempt = _safe_login_attempt(request.rel_url.query.get("login_attempt", "0"))
    auth_result = request.rel_url.query.get("auth_result") == "1"
    next_path = _sanitize_next_path(request.path_qs)
    if auth_result or login_attempt >= _AUTH_MAX_ATTEMPTS:
        raise web.HTTPUnauthorized(
            text=_auth_error_page(
                request,
                session_reason=session_reason,
                next_path=next_path,
                login_attempts=login_attempt,
            ),
            content_type="text/html",
        )
    next_attempt = login_attempt + 1
    login_target = (
        "/auth/login?"
        + urllib.parse.urlencode(
            {
                "next": next_path,
                "login_attempt": str(next_attempt),
            }
        )
    )
    raise web.HTTPFound(location=login_target)


def _build_add_bot_url(client_id: str, base_url: str) -> str:
    permissions = (
        (1 << 10)  # view_channel
        | (1 << 11)  # send_messages
        | (1 << 14)  # embed_links
        | (1 << 16)  # read_message_history
        | (1 << 13)  # manage_messages
    )
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": "bot applications.commands",
            "permissions": permissions,
        }
    )
    return f"https://discord.com/oauth2/authorize?{params}"


def _build_oauth2_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
    )
    return f"{_OAUTH2_AUTH_URL}?{params}"


def _render_template(
    request: web.Request,
    template_name: str,
    *,
    title: str,
    session: dict | None = None,
    **context: object,
) -> str:
    env: Environment = request.app["jinja_env"]
    template = env.get_template(template_name)
    return template.render(title=title, session=session, **context)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _index(request: web.Request) -> web.Response:
    session, _ = _get_session(request)
    database: Database = request.app["database"]
    client_id: str = request.app["client_id"]
    base_url: str = request.app["base_url"]

    stats = await asyncio.to_thread(database.get_global_stats)
    add_bot_url = _build_add_bot_url(client_id, base_url) if client_id else "#"

    return web.Response(
        text=_render_template(
            request,
            "home.html",
            title="Home",
            session=session,
            client_id=client_id,
            add_bot_url=add_bot_url,
            stats=stats,
        ),
        content_type="text/html",
    )


async def _stats_json(request: web.Request) -> web.Response:
    database: Database = request.app["database"]
    stats = await asyncio.to_thread(database.get_global_stats)
    return web.json_response(stats)


async def _auth_login(request: web.Request) -> web.Response:
    client_id: str = request.app["client_id"]
    base_url: str = request.app["base_url"]
    secret: str = request.app["secret_key"]
    secure_cookie: bool = request.app["cookie_secure"]

    if not client_id or not secret:
        raise web.HTTPServiceUnavailable(reason="OAuth2 not configured (DISCORD_CLIENT_ID / DASHBOARD_SECRET_KEY missing).")

    state = secrets.token_urlsafe(24)
    next_path = _sanitize_next_path(request.rel_url.query.get("next", "/dashboard"))
    login_attempt = _safe_login_attempt(request.rel_url.query.get("login_attempt", "0"))
    redirect_uri = f"{base_url}/auth/callback"
    url = _build_oauth2_url(client_id, redirect_uri, state)
    response = web.HTTPFound(location=url)
    response.set_cookie(
        "oauth_state",
        state,
        path="/",
        max_age=300,
        httponly=True,
        samesite="Lax",
        secure=secure_cookie,
    )
    response.set_cookie(
        "oauth_next",
        next_path,
        path="/",
        max_age=300,
        httponly=True,
        samesite="Lax",
        secure=secure_cookie,
    )
    response.set_cookie(
        "oauth_login_attempt",
        str(login_attempt),
        path="/",
        max_age=300,
        httponly=True,
        samesite="Lax",
        secure=secure_cookie,
    )
    raise response


async def _auth_callback(request: web.Request) -> web.Response:
    import aiohttp as _aiohttp

    client_id: str = request.app["client_id"]
    client_secret: str = request.app["client_secret"]
    base_url: str = request.app["base_url"]
    database: Database = request.app["database"]
    secure_cookie: bool = request.app["cookie_secure"]

    code = request.rel_url.query.get("code", "")
    state = request.rel_url.query.get("state", "")
    expected_state = request.cookies.get("oauth_state", "")
    next_path = _sanitize_next_path(request.cookies.get("oauth_next", "/dashboard"))
    login_attempt = _safe_login_attempt(request.cookies.get("oauth_login_attempt", "0"))

    if not code or not state or state != expected_state:
        raise web.HTTPBadRequest(reason="Invalid OAuth2 state or missing code.")

    redirect_uri = f"{base_url}/auth/callback"

    async with _aiohttp.ClientSession() as session:
        # Exchange code for token
        async with session.post(
            _OAUTH2_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as resp:
            if resp.status != 200:
                raise web.HTTPUnauthorized(reason="Token exchange failed.")
            token_data = await resp.json()

        access_token = token_data.get("access_token", "")
        auth_headers = {"Authorization": "Bearer " + access_token}

        # Fetch user identity
        async with session.get(f"{_DISCORD_API}/users/@me", headers=auth_headers) as resp:
            if resp.status != 200:
                raise web.HTTPUnauthorized(reason="Failed to fetch user identity.")
            user_data = await resp.json()

        # Fetch user's guilds
        async with session.get(f"{_DISCORD_API}/users/@me/guilds", headers=auth_headers) as resp:
            guilds = await resp.json() if resp.status == 200 else []

    session_data = {
        "user_id": user_data["id"],
        "username": f"{user_data['username']}",
        "avatar": user_data.get("avatar"),
        "guilds": [
            {"id": g["id"], "name": g["name"], "icon": g.get("icon"), "permissions": g.get("permissions", 0)}
            for g in guilds
            if isinstance(g, dict)
        ],
    }

    session_id = secrets.token_urlsafe(32)
    await asyncio.to_thread(
        database.create_dashboard_session,
        session_id=session_id,
        user_id=str(session_data["user_id"]),
        username=str(session_data["username"]),
        avatar=(str(session_data["avatar"]) if session_data["avatar"] else None),
        guilds=[
            {
                "id": str(g.get("id", "")),
                "name": str(g.get("name", "")),
                "icon": str(g.get("icon", "")),
                "permissions": str(g.get("permissions", "0")),
            }
            for g in session_data["guilds"]
            if isinstance(g, dict) and str(g.get("id", "")).strip()
        ],
        ttl_seconds=_COOKIE_MAX_AGE,
    )
    stored_session = await asyncio.to_thread(database.get_dashboard_session, session_id)
    if stored_session is None:
        raise web.HTTPInternalServerError(reason="Session could not be persisted.")

    redirect_target = _append_query_value(next_path, "auth_result", "1")
    response = web.HTTPFound(location=redirect_target)
    response.del_cookie("oauth_state", path="/", secure=secure_cookie)
    response.del_cookie("oauth_next", path="/", secure=secure_cookie)
    response.del_cookie("oauth_login_attempt", path="/", secure=secure_cookie)
    _set_session_cookie(response, session_id, secure=secure_cookie)
    raise response


async def _dashboard_index(request: web.Request) -> web.Response:
    session, session_reason = _get_session(request)
    if session is None:
        _raise_auth_redirect_or_error(request, session_reason=session_reason or "missing_session_cookie")

    bot: BgaDiscordBot = request.app["bot"]
    database: Database = request.app["database"]
    client_id: str = request.app["client_id"]
    base_url: str = request.app["base_url"]

    # Guilds where the user has MANAGE_GUILD and bot is present
    bot_guild_ids = {str(g.id) for g in bot.guilds}
    user_guilds = session.get("guilds", [])

    managed_guilds = [
        g for g in user_guilds
        if (int(g.get("permissions", 0)) & _MANAGE_GUILD) and g["id"] in bot_guild_ids
    ]

    # Guilds where the user has MANAGE_GUILD but the bot is NOT yet installed
    unenrolled_managed_guilds = [
        g for g in user_guilds
        if (int(g.get("permissions", 0)) & _MANAGE_GUILD) and g["id"] not in bot_guild_ids
    ]

    add_bot_url = _build_add_bot_url(client_id, base_url) if client_id else "#"

    managed_guild_cards: list[dict[str, object]] = []
    for guild in managed_guilds:
        guild_id = str(guild["id"])
        stats = await asyncio.to_thread(database.get_guild_stats, guild_id)
        icon_url = None
        if guild.get("icon"):
            icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{guild['icon']}.png?size=64"
        managed_guild_cards.append(
            {
                "id": guild_id,
                "name": str(guild["name"]),
                "icon_url": icon_url,
                "stats": stats,
            }
        )

    return web.Response(
        text=_render_template(
            request,
            "dashboard_index.html",
            title="Dashboard",
            session=session,
            managed_guilds=managed_guild_cards,
            add_bot_url=add_bot_url,
            show_add_another=bool(unenrolled_managed_guilds),
        ),
        content_type="text/html",
    )


async def _dashboard_guild(request: web.Request) -> web.Response:
    session, session_reason = _get_session(request)
    if session is None:
        _raise_auth_redirect_or_error(request, session_reason=session_reason or "missing_session_cookie")

    guild_id = request.match_info["guild_id"]
    if not guild_id.isdigit():
        raise web.HTTPBadRequest(reason="Invalid guild ID.")
    if not _session_manages_guild(session, guild_id):
        raise web.HTTPForbidden(reason="You do not manage this server.")

    bot: BgaDiscordBot = request.app["bot"]
    database: Database = request.app["database"]

    discord_guild = discord_guild = bot.get_guild(int(guild_id))
    guild_name = discord_guild.name if discord_guild else guild_id

    stats = await asyncio.to_thread(database.get_guild_stats, guild_id)
    settings = await asyncio.to_thread(
        database.get_guild_settings,
        guild_id,
        default_recruiting_only=bot.monitor._default_recruiting_only,
        default_delete_invite_message=bot._default_delete_invite_message,
        default_forced_channel_id=bot.monitor._default_forced_channel_id,
    )

    return web.Response(
        text=_render_template(
            request,
            "dashboard_guild.html",
            title=guild_name,
            session=session,
            guild_id=guild_id,
            guild_name=guild_name,
            stats=stats,
            settings=settings,
            saved=(request.rel_url.query.get("saved") == "1"),
        ),
        content_type="text/html",
    )


async def _dashboard_guild_settings_post(request: web.Request) -> web.Response:
    session, session_reason = _get_session(request)
    if session is None:
        _raise_auth_redirect_or_error(request, session_reason=session_reason or "missing_session_cookie")

    guild_id = request.match_info["guild_id"]
    # Validate guild_id is a Discord snowflake before using in a redirect.
    if not guild_id.isdigit():
        raise web.HTTPBadRequest(reason="Invalid guild ID.")
    if not _session_manages_guild(session, guild_id):
        raise web.HTTPForbidden(reason="You do not manage this server.")

    database: Database = request.app["database"]
    data = await request.post()

    recruiting_only = "recruiting_only" in data
    delete_invite_message = "delete_invite_message" in data
    forced_channel_id = (data.get("forced_channel_id") or "").strip() or None

    await asyncio.to_thread(
        database.upsert_guild_settings,
        guild_id,
        recruiting_only=recruiting_only,
        delete_invite_message=delete_invite_message,
        forced_channel_id=forced_channel_id,
    )
    raise web.HTTPFound(location=f"/dashboard/{int(guild_id)}?saved=1")


def _session_manages_guild(session: dict, guild_id: str) -> bool:
    for guild in session.get("guilds", []):
        if guild.get("id") == guild_id:
            return bool(int(guild.get("permissions", 0)) & _MANAGE_GUILD)
    return False


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_dashboard_app(
    *,
    bot: "BgaDiscordBot",
    database: "Database",
    base_url: str,
    client_id: str,
    client_secret: str,
    secret_key: str,
) -> web.Application:
    app = web.Application()
    package_root = Path(__file__).resolve().parent
    app["jinja_env"] = Environment(
        loader=FileSystemLoader(str(package_root / "templates")),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    )
    app["bot"] = bot
    app["database"] = database
    app["base_url"] = base_url
    app["client_id"] = client_id
    app["client_secret"] = client_secret
    app["secret_key"] = secret_key
    app["cookie_secure"] = urllib.parse.urlparse(base_url).scheme.lower() == "https"

    app.router.add_get("/", _index)
    app.router.add_get("/stats", _stats_json)
    app.router.add_get("/auth/login", _auth_login)
    app.router.add_get("/auth/callback", _auth_callback)
    app.router.add_get("/dashboard", _dashboard_index)
    app.router.add_get("/dashboard/{guild_id}", _dashboard_guild)
    app.router.add_post("/dashboard/{guild_id}/settings", _dashboard_guild_settings_post)
    app.router.add_static("/static/", str(package_root / "static"))

    return app


async def run_dashboard(app: web.Application, *, port: int = 8080) -> None:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    LOGGER.info("Dashboard listening on http://0.0.0.0:%d", port)
    # Keep running until cancelled
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
