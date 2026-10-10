"""Onboarding web dashboard for BGA Turn Bot.

Serves on port 8080 (configurable via DASHBOARD_PORT).

Routes
------
GET  /                          Landing page with global stats and "Add to Server" button.
GET  /stats                     JSON: {total, recruiting} counts.
GET  /global/stats              Global stats page.
GET  /auth/login                Start Discord OAuth2 (user-only: identify + guilds).
GET  /auth/callback             Finish OAuth2, set session cookie, redirect to /dashboard.
POST /auth/logout               End the dashboard session.
GET  /admin/servers             Global admins: every server the bot is in.
GET  /dashboard                 Servers the user manages.
GET  /dashboard/{guild_id}      Per-guild overview + settings form.
GET  /dashboard/{guild_id}/stats   Per-guild stats.
GET  /dashboard/{guild_id}/tables  Per-guild watched table lifecycle view.
GET  /dashboard/{guild_id}/members Per-guild member linking view.
GET  /dashboard/{guild_id}/members/search?q= Query guild members for linking UI.
POST /dashboard/{guild_id}/settings  Save per-guild settings.
POST /dashboard/{guild_id}/tables/{subscription_id}/unwatch  Stop monitoring one table.
POST /dashboard/{guild_id}/members/link Link a guild member to a BGA account.
POST /dashboard/{guild_id}/members/{discord_user_id}/unlink Remove an existing user link.

Access to a server's pages is checked live against Discord (with a short cache),
so losing Manage Server takes effect within a minute rather than at session expiry.
Every POST carries a CSRF token derived from DASHBOARD_SECRET_KEY and the session.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

import aiohttp
import discord
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .utils import build_table_url, format_game_name

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
_RECENT_FINISHED_LIMIT = 10
_PERMISSION_CACHE_SECONDS = 60.0
_MEMBER_SEARCH_LIMIT = 25
_OAUTH_TIMEOUT = aiohttp.ClientTimeout(total=15)

_SNOWFLAKE_PATTERN = re.compile(r"[0-9]{1,20}")
# Final standings are stored as display strings: "#1 Name (12345) • score 72".
_STANDING_PATTERN = re.compile(r"^(?:#\d+|-) (?P<name>.+) \((?P<id>[^()]*)\) • score ")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://cdn.discordapp.com https://x.boardgamearena.net; "
        "connect-src 'self'; "
        "form-action 'self' https://discord.com; "
        "frame-ancestors 'none'; "
        "base-uri 'none'"
    ),
}

# Routes that answer JSON; errors there stay JSON instead of an HTML error page.
_JSON_PATHS = ("/stats",)
_JSON_SUFFIXES = ("/members/search",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_snowflake(raw: object) -> str | None:
    """Return *raw* as a Discord ID string, or ``None`` if it is not one.

    Uses an ASCII-only pattern: ``str.isdigit`` also accepts characters such as
    ``"²"`` that ``int()`` then rejects.
    """
    value = str(raw or "").strip()
    return value if _SNOWFLAKE_PATTERN.fullmatch(value) else None


def _set_session_cookie(response: web.StreamResponse, session_id: str, *, secure: bool) -> None:
    response.set_cookie(
        _COOKIE_NAME,
        session_id,
        path="/",
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=secure,
    )


async def _get_session(request: web.Request) -> tuple[dict[str, object] | None, str | None]:
    cached = request.get("dashboard_session")
    if cached is not None:
        return cached, None
    cookie = request.cookies.get(_COOKIE_NAME, "")
    if not cookie:
        return None, "missing_session_cookie"
    if len(cookie) < 24:
        return None, "invalid_session_cookie"
    database: Database = request.app["database"]
    session = await asyncio.to_thread(database.get_dashboard_session, cookie)
    if session is None:
        return None, "session_missing_or_expired"
    request["dashboard_session"] = session
    return session, None


def _csrf_token(app: web.Application, session: dict[str, object]) -> str:
    secret = str(app["secret_key"]).encode("utf-8")
    return hmac.new(secret, str(session["session_id"]).encode("utf-8"), hashlib.sha256).hexdigest()


async def _require_csrf(request: web.Request, session: dict[str, object]) -> None:
    data = await request.post()
    submitted = str(data.get("csrf_token") or "")
    if not submitted or not hmac.compare_digest(submitted, _csrf_token(request.app, session)):
        raise web.HTTPForbidden(reason="Your session form token is invalid. Reload the page and try again.")


def _safe_login_attempt(raw: str) -> int:
    try:
        return max(0, min(10, int(raw)))
    except (TypeError, ValueError):
        return 0


def _sanitize_next_path(raw: str) -> str:
    candidate = (raw or "").strip()
    if not candidate or not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
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


def _resolve_channel_label(discord_guild: discord.Guild | None, channel_id: str) -> str:
    if discord_guild is None or not _parse_snowflake(channel_id):
        return channel_id
    channel = discord_guild.get_channel_or_thread(int(channel_id))
    if channel is None:
        return channel_id
    return f"#{channel.name}"


def _member_payload(member: discord.Member) -> dict[str, str]:
    username = str(getattr(member, "name", ""))
    avatar = getattr(member, "display_avatar", None)
    return {
        "id": str(member.id),
        "display_name": str(getattr(member, "display_name", username) or username),
        "username": username,
        "avatar_url": str(avatar.with_size(32).url) if avatar is not None else "",
    }


def _guild_icon_url(guild_id: str, icon_hash: str | None) -> str | None:
    if not icon_hash:
        return None
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png?size=64"


def _user_avatar_url(session: dict[str, object] | None) -> str | None:
    if not session or not session.get("avatar"):
        return None
    return f"https://cdn.discordapp.com/avatars/{session['user_id']}/{session['avatar']}.png?size=64"


def _parse_iso(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _relative_time(value: object) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "—"
    seconds = int((datetime.now(timezone.utc) - parsed).total_seconds())
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if seconds >= size:
            amount = f"{seconds // size}{unit}"
            return f"in {amount}" if future else f"{amount} ago"
    return "just now"


def _absolute_time(value: object) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return ""
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _build_add_bot_url(client_id: str, guild_id: str | None = None) -> str:
    permissions = (
        (1 << 10)  # view_channel
        | (1 << 11)  # send_messages
        | (1 << 14)  # embed_links
        | (1 << 16)  # read_message_history
        | (1 << 13)  # manage_messages
    )
    params: dict[str, object] = {
        "client_id": client_id,
        "scope": "bot applications.commands",
        "permissions": permissions,
    }
    if guild_id:
        params["guild_id"] = guild_id
        params["disable_guild_select"] = "true"
    return f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"


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
    is_global_admin = _is_global_admin(session, request.app) if session is not None else False
    return template.render(
        title=title,
        session=session,
        is_global_admin=is_global_admin,
        csrf_token=_csrf_token(request.app, session) if session is not None else "",
        user_avatar_url=_user_avatar_url(session),
        current_path=request.path,
        **context,
    )


def _html(text: str, *, status: int = 200) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/html")


def _session_manages_guild(session: dict, guild_id: str, app: web.Application | None = None) -> bool:
    """Login-time permission snapshot. Used only to list servers, never to grant access."""
    if app is not None and _is_global_admin(session, app):
        return True
    for guild in session.get("guilds", []):
        if guild.get("id") == guild_id:
            try:
                return bool(int(guild.get("permissions", 0)) & _MANAGE_GUILD)
            except (TypeError, ValueError):
                return False
    return False


def _is_global_admin(session: dict | None, app: web.Application) -> bool:
    if not session:
        return False
    admin_ids: frozenset[str] = app.get("global_admin_ids", frozenset())
    return str(session.get("user_id", "")) in admin_ids


async def _fetch_guild_member(guild: discord.Guild, user_id: str) -> discord.Member | None:
    """Return *user_id* as a member of *guild*, or ``None`` if they are not in it.

    Does not need the privileged members intent: falls back to a REST lookup when
    the member is not cached. Raises ``HTTPServiceUnavailable`` if Discord cannot
    be reached, so callers never mistake an outage for "not a member".
    """
    member = guild.get_member(int(user_id))
    if member is not None:
        return member
    try:
        return await guild.fetch_member(int(user_id))
    except discord.NotFound:
        return None
    except discord.HTTPException as exc:
        LOGGER.warning("Member lookup failed for user %s in guild %s: %s", user_id, guild.id, exc)
        raise web.HTTPServiceUnavailable(reason="Discord is not responding right now. Try again in a moment.") from exc


async def _user_can_manage(app: web.Application, guild: discord.Guild, user_id: str) -> bool:
    cache: dict[tuple[int, str], tuple[float, bool]] = app["permission_cache"]
    key = (guild.id, user_id)
    now = time.monotonic()
    cached = cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]
    member = await _fetch_guild_member(guild, user_id)
    allowed = member is not None and member.guild_permissions.manage_guild
    if len(cache) > 2048:
        for stale_key in [item_key for item_key, (expires, _) in cache.items() if expires <= now]:
            del cache[stale_key]
    cache[key] = (now + _PERMISSION_CACHE_SECONDS, allowed)
    return allowed


async def _resolve_guild_members(guild: discord.Guild, user_ids: list[str]) -> dict[str, discord.Member]:
    """Resolve which of *user_ids* are members of *guild*, without the members intent."""
    found: dict[str, discord.Member] = {}
    missing: list[int] = []
    for user_id in user_ids:
        if not _parse_snowflake(user_id):
            continue
        member = guild.get_member(int(user_id))
        if member is not None:
            found[user_id] = member
        else:
            missing.append(int(user_id))
    for start in range(0, len(missing), 100):
        chunk = missing[start:start + 100]
        members = await guild.query_members(user_ids=chunk, limit=len(chunk), cache=False)
        for member in members:
            found[str(member.id)] = member
    return found


def _auth_error_page(
    request: web.Request,
    *,
    session_reason: str,
    next_path: str,
    login_attempts: int,
) -> str:
    reason_text = {
        "missing_session_cookie": "Your browser did not send the dashboard login cookie.",
        "invalid_session_cookie": "The dashboard login cookie is malformed.",
        "session_missing_or_expired": "Your dashboard login has expired.",
    }.get(session_reason, "Dashboard authentication state is invalid.")
    return _render_template(
        request,
        "auth_error.html",
        title="Login problem",
        reason_text=reason_text,
        next_path=next_path,
        login_attempts=login_attempts,
        quoted_next_path=urllib.parse.quote(next_path, safe="/?=&"),
    )


def _raise_auth_redirect_or_error(request: web.Request, *, session_reason: str) -> None:
    login_attempt = _safe_login_attempt(request.rel_url.query.get("login_attempt", "0"))
    auth_result = request.rel_url.query.get("auth_result") == "1"
    next_path = _sanitize_next_path(request.path_qs if request.method == "GET" else "/dashboard")
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
    login_target = "/auth/login?" + urllib.parse.urlencode(
        {"next": next_path, "login_attempt": str(login_attempt + 1)}
    )
    raise web.HTTPFound(location=login_target)


# ---------------------------------------------------------------------------
# Route guards
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GuildContext:
    session: dict[str, object]
    guild_id: str
    guild: discord.Guild


async def _require_session(request: web.Request) -> dict[str, object]:
    session, reason = await _get_session(request)
    if session is None:
        _raise_auth_redirect_or_error(request, session_reason=reason or "missing_session_cookie")
    assert session is not None
    if request.method == "POST":
        await _require_csrf(request, session)
    return session


def login_required(
    handler: Callable[[web.Request, dict[str, object]], Awaitable[web.StreamResponse]],
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        return await handler(request, await _require_session(request))

    return wrapper


def guild_route(
    handler: Callable[[web.Request, GuildContext], Awaitable[web.StreamResponse]],
) -> Callable[[web.Request], Awaitable[web.StreamResponse]]:
    """Require a session (plus CSRF on POST) and live Manage Server on the guild."""

    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        session = await _require_session(request)
        guild_id = _parse_snowflake(request.match_info["guild_id"])
        if guild_id is None:
            raise web.HTTPNotFound(reason="That is not a valid server ID.")
        bot: BgaDiscordBot = request.app["bot"]
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            raise web.HTTPNotFound(reason="The bot is not in this server.")
        if not _is_global_admin(session, request.app):
            if not await _user_can_manage(request.app, guild, str(session["user_id"])):
                raise web.HTTPForbidden(reason="You need the Manage Server permission on this server.")
        return await handler(request, GuildContext(session=session, guild_id=guild_id, guild=guild))

    return wrapper


def _guild_page_context(ctx: GuildContext, active_tab: str) -> dict[str, object]:
    return {
        "guild_id": ctx.guild_id,
        "guild_name": ctx.guild.name,
        "guild_icon_url": _guild_icon_url(ctx.guild_id, ctx.guild.icon.key if ctx.guild.icon else None),
        "active_tab": active_tab,
    }


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


async def _index(request: web.Request) -> web.Response:
    session, _ = await _get_session(request)
    database: Database = request.app["database"]
    client_id: str = request.app["client_id"]

    stats = await asyncio.to_thread(database.get_global_extended_stats)
    add_bot_url = _build_add_bot_url(client_id) if client_id else ""

    return _html(
        _render_template(
            request,
            "home.html",
            title="Home",
            session=session,
            add_bot_url=add_bot_url,
            stats=stats,
        )
    )


async def _stats_json(request: web.Request) -> web.Response:
    database: Database = request.app["database"]
    stats = await asyncio.to_thread(database.get_global_stats)
    return web.json_response(stats)


async def _global_stats(request: web.Request) -> web.Response:
    session, _ = await _get_session(request)
    database: Database = request.app["database"]
    stats = await asyncio.to_thread(database.get_global_extended_stats)
    return _html(
        _render_template(
            request,
            "dashboard_stats.html",
            title="Global stats",
            session=session,
            stats=stats,
            is_global=True,
        )
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


async def _auth_login(request: web.Request) -> web.Response:
    client_id: str = request.app["client_id"]
    base_url: str = request.app["base_url"]
    secure_cookie: bool = request.app["cookie_secure"]

    if not client_id:
        raise web.HTTPServiceUnavailable(reason="Discord login is not configured on this bot.")

    state = secrets.token_urlsafe(24)
    next_path = _sanitize_next_path(request.rel_url.query.get("next", "/dashboard"))
    login_attempt = _safe_login_attempt(request.rel_url.query.get("login_attempt", "0"))
    response = web.HTTPFound(location=_build_oauth2_url(client_id, f"{base_url}/auth/callback", state))
    for name, value in (
        ("oauth_state", state),
        ("oauth_next", next_path),
        ("oauth_login_attempt", str(login_attempt)),
    ):
        response.set_cookie(name, value, path="/", max_age=300, httponly=True, samesite="Lax", secure=secure_cookie)
    raise response


async def _auth_callback(request: web.Request) -> web.Response:
    client_id: str = request.app["client_id"]
    client_secret: str = request.app["client_secret"]
    base_url: str = request.app["base_url"]
    database: Database = request.app["database"]
    secure_cookie: bool = request.app["cookie_secure"]

    if request.rel_url.query.get("error"):
        # The user pressed "Cancel" on Discord's consent screen.
        raise web.HTTPFound(location="/")

    code = request.rel_url.query.get("code", "")
    state = request.rel_url.query.get("state", "")
    expected_state = request.cookies.get("oauth_state", "")
    next_path = _sanitize_next_path(request.cookies.get("oauth_next", "/dashboard"))

    if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
        raise web.HTTPBadRequest(reason="This login link has expired. Please log in again.")

    try:
        async with aiohttp.ClientSession(timeout=_OAUTH_TIMEOUT) as http:
            async with http.post(
                _OAUTH2_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": f"{base_url}/auth/callback",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status != 200:
                    LOGGER.warning("Discord OAuth2 token exchange failed with HTTP %s.", resp.status)
                    raise web.HTTPUnauthorized(reason="Discord rejected the login. Please try again.")
                token_data = await resp.json()

            auth_headers = {"Authorization": "Bearer " + str(token_data.get("access_token", ""))}
            async with http.get(f"{_DISCORD_API}/users/@me", headers=auth_headers) as resp:
                if resp.status != 200:
                    raise web.HTTPUnauthorized(reason="Could not read your Discord profile.")
                user_data = await resp.json()

            async with http.get(f"{_DISCORD_API}/users/@me/guilds", headers=auth_headers) as resp:
                guilds = await resp.json() if resp.status == 200 else []
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOGGER.warning("Discord OAuth2 request failed: %s", exc)
        raise web.HTTPBadGateway(reason="Could not reach Discord. Please try again.") from exc

    user_id = _parse_snowflake(user_data.get("id") if isinstance(user_data, dict) else None)
    if user_id is None:
        raise web.HTTPUnauthorized(reason="Discord returned an unexpected profile.")

    session_id = secrets.token_urlsafe(32)
    await asyncio.to_thread(
        database.create_dashboard_session,
        session_id=session_id,
        user_id=user_id,
        username=str(user_data.get("global_name") or user_data.get("username") or user_id),
        avatar=(str(user_data["avatar"]) if user_data.get("avatar") else None),
        guilds=[
            {
                "id": str(g.get("id", "")),
                "name": str(g.get("name", "")),
                "icon": str(g.get("icon") or ""),
                "permissions": str(g.get("permissions", "0")),
            }
            for g in (guilds if isinstance(guilds, list) else [])
            if isinstance(g, dict) and _parse_snowflake(g.get("id"))
        ],
        ttl_seconds=_COOKIE_MAX_AGE,
    )

    response = web.HTTPFound(location=_append_query_value(next_path, "auth_result", "1"))
    for name in ("oauth_state", "oauth_next", "oauth_login_attempt"):
        response.del_cookie(name, path="/", secure=secure_cookie)
    _set_session_cookie(response, session_id, secure=secure_cookie)
    raise response


@login_required
async def _auth_logout(request: web.Request, session: dict[str, object]) -> web.Response:
    database: Database = request.app["database"]
    await asyncio.to_thread(database.delete_dashboard_session, str(session["session_id"]))
    response = web.HTTPFound(location="/")
    response.del_cookie(_COOKIE_NAME, path="/", secure=request.app["cookie_secure"])
    raise response


# ---------------------------------------------------------------------------
# Server lists
# ---------------------------------------------------------------------------


def _guild_card(guild_id: str, name: str, icon_hash: str | None, stats: dict[str, int] | None) -> dict[str, object]:
    return {
        "id": guild_id,
        "name": name,
        "icon_url": _guild_icon_url(guild_id, icon_hash),
        "initial": (name[:1] or "?").upper(),
        "stats": stats or {"total": 0, "recruiting": 0},
    }


@login_required
async def _dashboard_index(request: web.Request, session: dict[str, object]) -> web.Response:
    bot: BgaDiscordBot = request.app["bot"]
    database: Database = request.app["database"]
    client_id: str = request.app["client_id"]

    all_stats = await asyncio.to_thread(database.get_all_guild_stats)
    bot_guilds = {str(g.id): g for g in bot.guilds}

    managed: list[dict[str, object]] = []
    installable: list[dict[str, object]] = []
    for guild in session.get("guilds", []):
        guild_id = str(guild["id"])
        if not _session_manages_guild(session, guild_id):
            continue
        name = str(guild.get("name") or guild_id)
        card = _guild_card(guild_id, name, str(guild.get("icon") or "") or None, all_stats.get(guild_id))
        if guild_id in bot_guilds:
            managed.append(card)
        elif client_id:
            card["add_url"] = _build_add_bot_url(client_id, guild_id)
            installable.append(card)

    managed.sort(key=lambda item: str(item["name"]).casefold())
    installable.sort(key=lambda item: str(item["name"]).casefold())

    return _html(
        _render_template(
            request,
            "dashboard_index.html",
            title="Your servers",
            session=session,
            managed_guilds=managed,
            installable_guilds=installable,
            add_bot_url=_build_add_bot_url(client_id) if client_id else "",
        )
    )


@login_required
async def _admin_servers(request: web.Request, session: dict[str, object]) -> web.Response:
    if not _is_global_admin(session, request.app):
        raise web.HTTPForbidden(reason="Only global admins can see every server.")

    bot: BgaDiscordBot = request.app["bot"]
    database: Database = request.app["database"]
    all_stats = await asyncio.to_thread(database.get_all_guild_stats)

    guild_cards = []
    for discord_guild in sorted(bot.guilds, key=lambda g: g.name.casefold()):
        guild_id = str(discord_guild.id)
        card = _guild_card(
            guild_id,
            discord_guild.name,
            discord_guild.icon.key if discord_guild.icon else None,
            all_stats.get(guild_id),
        )
        card["member_count"] = discord_guild.member_count
        guild_cards.append(card)

    return _html(
        _render_template(
            request,
            "admin_servers.html",
            title="All servers",
            session=session,
            guild_cards=guild_cards,
        )
    )


# ---------------------------------------------------------------------------
# Per-guild pages
# ---------------------------------------------------------------------------


def _channel_options(guild: discord.Guild, selected_id: str | None) -> tuple[list[dict[str, object]], bool]:
    """Return the guild's text channels for the forced-channel picker.

    The second value is False when *selected_id* is set but is not one of them.
    """
    me = guild.me
    options = []
    selected_found = not selected_id
    for channel in sorted(guild.text_channels, key=lambda item: (item.position, item.id)):
        can_post = True
        if me is not None:
            permissions = channel.permissions_for(me)
            can_post = permissions.view_channel and permissions.send_messages
        is_selected = str(channel.id) == selected_id
        selected_found = selected_found or is_selected
        options.append(
            {
                "id": str(channel.id),
                "name": channel.name,
                "category": channel.category.name if channel.category else "",
                "can_post": can_post,
                "selected": is_selected,
            }
        )
    return options, selected_found


@guild_route
async def _dashboard_guild(request: web.Request, ctx: GuildContext) -> web.Response:
    bot: BgaDiscordBot = request.app["bot"]
    database: Database = request.app["database"]

    stats = await asyncio.to_thread(database.get_guild_stats, ctx.guild_id)
    settings = await asyncio.to_thread(
        database.get_guild_settings,
        ctx.guild_id,
        default_recruiting_only=bot.monitor._default_recruiting_only,
        default_delete_invite_message=bot._default_delete_invite_message,
        default_forced_channel_id=bot.monitor._default_forced_channel_id,
    )
    channel_options, forced_channel_valid = _channel_options(ctx.guild, settings.forced_channel_id)

    return _html(
        _render_template(
            request,
            "dashboard_guild.html",
            title=ctx.guild.name,
            session=ctx.session,
            stats=stats,
            settings=settings,
            channel_options=channel_options,
            forced_channel_valid=forced_channel_valid,
            saved=(request.rel_url.query.get("saved") == "1"),
            error=request.rel_url.query.get("error", ""),
            **_guild_page_context(ctx, "overview"),
        )
    )


@guild_route
async def _dashboard_guild_settings_post(request: web.Request, ctx: GuildContext) -> web.Response:
    database: Database = request.app["database"]
    data = await request.post()

    forced_channel_id: str | None = None
    raw_channel = str(data.get("forced_channel_id") or "").strip()
    if raw_channel:
        channel_id = _parse_snowflake(raw_channel)
        channel = ctx.guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            raise web.HTTPFound(location=f"/dashboard/{ctx.guild_id}?error=channel")
        forced_channel_id = channel_id

    await asyncio.to_thread(
        database.upsert_guild_settings,
        ctx.guild_id,
        recruiting_only="recruiting_only" in data,
        delete_invite_message="delete_invite_message" in data,
        forced_channel_id=forced_channel_id,
    )
    raise web.HTTPFound(location=f"/dashboard/{ctx.guild_id}?saved=1")


@guild_route
async def _dashboard_guild_tables(request: web.Request, ctx: GuildContext) -> web.Response:
    database: Database = request.app["database"]
    discord_guild = ctx.guild

    subscriptions = await asyncio.to_thread(database.list_watch_subscriptions_for_guild, ctx.guild_id)
    recent_finished = await asyncio.to_thread(
        database.list_recent_game_history_for_guild,
        ctx.guild_id,
        limit=_RECENT_FINISHED_LIMIT,
    )

    creator_ids = sorted(
        {item.created_by_discord_user_id for item in subscriptions}
        | {item.created_by_discord_user_id for item in recent_finished}
    )
    try:
        creators = await _resolve_guild_members(discord_guild, creator_ids)
    except (asyncio.TimeoutError, discord.DiscordException) as exc:
        LOGGER.warning("Could not resolve table creators for guild %s: %s", ctx.guild_id, exc)
        creators = {}

    def _creator_name(user_id: str) -> str:
        member = creators.get(user_id)
        return member.display_name if member is not None else "a former member"

    recruiting_cards: list[dict[str, object]] = []
    in_progress_cards: list[dict[str, object]] = []
    for subscription in subscriptions:
        seated_players = sorted(
            [name for name in subscription.seated_player_names.values() if name.strip()],
            key=lambda name: name.casefold(),
        )
        seats_total = subscription.seats_total
        seats_taken = (
            seats_total - subscription.seats_remaining
            if seats_total is not None and subscription.seats_remaining is not None
            else len(seated_players)
        )
        card = {
            "subscription_id": subscription.subscription_id,
            "table_id": subscription.table_id,
            "table_url": subscription.table_url or build_table_url(subscription.table_id),
            "game_name": format_game_name(subscription.game_name),
            "channel": _resolve_channel_label(discord_guild, subscription.channel_id),
            "created_by": _creator_name(subscription.created_by_discord_user_id),
            "started_at": subscription.recruiting_started_at,
            "seats_total": seats_total,
            "seats_taken": seats_taken,
            "seats_percent": (
                min(100, round(100 * seats_taken / seats_total)) if seats_total else 0
            ),
            "seated_players": seated_players,
            "waiting_players": [
                subscription.player_names.get(player_id, player_id)
                for player_id in subscription.last_waiting_ids
            ],
        }
        if subscription.lifecycle_state == "recruiting":
            recruiting_cards.append(card)
        else:
            in_progress_cards.append(card)

    finished_cards = [
        {
            "table_id": item.table_id,
            "table_url": build_table_url(item.table_id),
            "game_name": format_game_name(item.game_name),
            "channel": _resolve_channel_label(discord_guild, item.channel_id),
            "created_by": _creator_name(item.created_by_discord_user_id),
            "outcome": item.outcome,
            "winner_names": item.winner_names,
            "final_standings": item.final_standings,
            "finished_at": item.finished_at,
        }
        for item in recent_finished
    ]

    return _html(
        _render_template(
            request,
            "dashboard_tables.html",
            title=f"{ctx.guild.name} · Tables",
            session=ctx.session,
            removed=request.rel_url.query.get("removed", ""),
            recruiting_cards=recruiting_cards,
            in_progress_cards=in_progress_cards,
            finished_cards=finished_cards,
            **_guild_page_context(ctx, "tables"),
        )
    )


@guild_route
async def _dashboard_guild_unwatch_post(request: web.Request, ctx: GuildContext) -> web.Response:
    subscription_id_raw = _parse_snowflake(request.match_info["subscription_id"])
    if subscription_id_raw is None:
        raise web.HTTPNotFound(reason="That table is not being watched.")

    database: Database = request.app["database"]
    subscription = await asyncio.to_thread(database.get_watch_subscription, int(subscription_id_raw))
    if subscription is None or subscription.guild_id != ctx.guild_id:
        raise web.HTTPFound(location=f"/dashboard/{ctx.guild_id}/tables?removed=0")

    removed = await asyncio.to_thread(
        database.remove_watch_subscription,
        table_id=subscription.table_id,
        guild_id=ctx.guild_id,
        channel_id=subscription.channel_id,
        outcome="unwatched",
    )
    raise web.HTTPFound(location=f"/dashboard/{ctx.guild_id}/tables?removed={'1' if removed else '0'}")


@guild_route
async def _dashboard_guild_members(request: web.Request, ctx: GuildContext) -> web.Response:
    database: Database = request.app["database"]

    linked_users = await asyncio.to_thread(database.list_linked_users)
    subscriptions = await asyncio.to_thread(database.list_watch_subscriptions_for_guild, ctx.guild_id)
    history_rows = await asyncio.to_thread(database.list_game_history_for_guild, ctx.guild_id, limit=1000)

    members_error = False
    try:
        members_by_id = await _resolve_guild_members(ctx.guild, [item.discord_user_id for item in linked_users])
    except (asyncio.TimeoutError, discord.DiscordException) as exc:
        LOGGER.warning("Could not resolve linked members for guild %s: %s", ctx.guild_id, exc)
        members_by_id = {}
        members_error = True

    linked_cards = []
    linked_by_id: set[str] = set()
    linked_by_name: set[str] = set()
    for linked in linked_users:
        member = members_by_id.get(linked.discord_user_id)
        if member is None:
            continue
        linked_cards.append(
            {
                "discord_user_id": linked.discord_user_id,
                "discord_display_name": member.display_name,
                "discord_username": member.name,
                "avatar_url": str(member.display_avatar.with_size(32).url),
                "bga_player_id": linked.bga_player_id,
                "bga_player_name": linked.bga_player_name,
            }
        )
        if linked.bga_player_id:
            linked_by_id.add(linked.bga_player_id)
        if linked.bga_player_name:
            linked_by_name.add(linked.bga_player_name.casefold())
    linked_cards.sort(key=lambda item: (str(item["discord_display_name"]).casefold(), str(item["bga_player_name"]).casefold()))

    unmatched_candidates: dict[str, dict[str, str]] = {}

    def _append_unmatched_candidate(player_id: str, player_name: str) -> None:
        normalized_id = player_id.strip()
        normalized_name = player_name.strip()
        if not normalized_id and not normalized_name:
            return
        if normalized_id and normalized_id in linked_by_id:
            return
        if normalized_name and normalized_name.casefold() in linked_by_name:
            return
        key = normalized_name.casefold() or normalized_id
        existing = unmatched_candidates.get(key)
        if existing is not None:
            existing["bga_player_id"] = existing["bga_player_id"] or normalized_id
            return
        unmatched_candidates[key] = {
            "bga_player_id": normalized_id,
            "bga_player_name": normalized_name or normalized_id,
        }

    for subscription in subscriptions:
        for player_id, player_name in subscription.player_names.items():
            _append_unmatched_candidate(player_id, player_name)
        for player_id, player_name in subscription.seated_player_names.items():
            _append_unmatched_candidate(player_id, player_name)
    for history in history_rows:
        for standing in history.final_standings:
            match = _STANDING_PATTERN.match(standing)
            if match is not None:
                player_id = match.group("id")
                _append_unmatched_candidate(player_id if player_id.isdecimal() else "", match.group("name"))
        for name in history.winner_names:
            _append_unmatched_candidate("", name)

    unmatched_cards = sorted(
        unmatched_candidates.values(),
        key=lambda item: (item["bga_player_name"].casefold(), item["bga_player_id"]),
    )

    return _html(
        _render_template(
            request,
            "dashboard_members.html",
            title=f"{ctx.guild.name} · Members",
            session=ctx.session,
            linked_cards=linked_cards,
            unmatched_cards=unmatched_cards,
            members_error=members_error,
            link_saved=(request.rel_url.query.get("linked") == "1"),
            unlinked=(request.rel_url.query.get("unlinked") == "1"),
            error=request.rel_url.query.get("error", ""),
            **_guild_page_context(ctx, "members"),
        )
    )


@guild_route
async def _dashboard_guild_members_search(request: web.Request, ctx: GuildContext) -> web.Response:
    query = (request.rel_url.query.get("q", "") or "").strip()[:100]
    if not query:
        return web.json_response({"members": []})

    try:
        if _parse_snowflake(query) and len(query) >= 15:
            member = await _fetch_guild_member(ctx.guild, query)
            members = [member] if member is not None else []
        else:
            # Prefix search over username/nickname; works without the members intent.
            members = await ctx.guild.query_members(query=query, limit=_MEMBER_SEARCH_LIMIT, cache=False)
    except (asyncio.TimeoutError, discord.DiscordException) as exc:
        LOGGER.warning("Member search failed for guild %s: %s", ctx.guild_id, exc)
        return web.json_response({"members": [], "error": "Discord did not answer. Try again."}, status=503)

    members = [member for member in members if not member.bot]
    return web.json_response({"members": [_member_payload(item) for item in members]})


@guild_route
async def _dashboard_guild_members_link_post(request: web.Request, ctx: GuildContext) -> web.Response:
    data = await request.post()
    discord_user_id = _parse_snowflake(data.get("discord_user_id"))
    bga_player_id = str(data.get("bga_player_id") or "").strip()[:32]
    bga_player_name = str(data.get("bga_player_name") or "").strip()[:100]
    members_url = f"/dashboard/{ctx.guild_id}/members"
    if discord_user_id is None:
        raise web.HTTPFound(location=f"{members_url}?error=member")
    if not bga_player_name and not bga_player_id:
        raise web.HTTPFound(location=f"{members_url}?error=bga")
    if bga_player_id and not (bga_player_id.isascii() and bga_player_id.isdecimal()):
        raise web.HTTPFound(location=f"{members_url}?error=bga_id")
    # Links are global, so only allow linking people who are actually in this server.
    if await _fetch_guild_member(ctx.guild, discord_user_id) is None:
        raise web.HTTPFound(location=f"{members_url}?error=not_member")

    database: Database = request.app["database"]
    await asyncio.to_thread(
        database.upsert_linked_user,
        discord_user_id,
        bga_player_id,
        bga_player_name,
    )
    raise web.HTTPFound(location=f"{members_url}?linked=1")


@guild_route
async def _dashboard_guild_members_unlink_post(request: web.Request, ctx: GuildContext) -> web.Response:
    discord_user_id = _parse_snowflake(request.match_info["discord_user_id"])
    members_url = f"/dashboard/{ctx.guild_id}/members"
    if discord_user_id is None:
        raise web.HTTPFound(location=f"{members_url}?error=member")
    # Links are global, so a server admin may only remove links of their own members.
    if await _fetch_guild_member(ctx.guild, discord_user_id) is None:
        raise web.HTTPFound(location=f"{members_url}?error=not_member")

    database: Database = request.app["database"]
    await asyncio.to_thread(database.remove_linked_user, discord_user_id)
    raise web.HTTPFound(location=f"{members_url}?unlinked=1")


@guild_route
async def _dashboard_guild_stats(request: web.Request, ctx: GuildContext) -> web.Response:
    database: Database = request.app["database"]
    stats = await asyncio.to_thread(database.get_guild_extended_stats, ctx.guild_id)
    return _html(
        _render_template(
            request,
            "dashboard_stats.html",
            title=f"{ctx.guild.name} · Stats",
            session=ctx.session,
            stats=stats,
            is_global=False,
            **_guild_page_context(ctx, "stats"),
        )
    )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def _wants_json(request: web.Request) -> bool:
    return request.path in _JSON_PATHS or request.path.endswith(_JSON_SUFFIXES)


_ERROR_TITLES = {
    400: "Bad request",
    401: "Login required",
    403: "Not allowed",
    404: "Not found",
    502: "Discord unavailable",
    503: "Temporarily unavailable",
}


async def _render_error(request: web.Request, status: int, message: str) -> web.Response:
    if _wants_json(request):
        return web.json_response({"error": message}, status=status)
    session, _ = await _get_session(request)
    title = _ERROR_TITLES.get(status, "Something went wrong")
    return _html(
        _render_template(request, "error.html", title=title, session=session, status=status, message=message),
        status=status,
    )


@web.middleware
async def _error_and_headers_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        if exc.status < 400 or exc.content_type == "text/html":
            exc.headers.update(_SECURITY_HEADERS)
            raise
        response = await _render_error(request, exc.status, exc.reason)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("Unhandled dashboard error on %s %s", request.method, request.path)
        response = await _render_error(request, 500, "An unexpected error occurred. It has been logged.")
    response.headers.update(_SECURITY_HEADERS)
    return response


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
    global_admin_ids: frozenset[str] | None = None,
) -> web.Application:
    if not secret_key:
        raise ValueError("DASHBOARD_SECRET_KEY is required to sign dashboard forms.")
    app = web.Application(middlewares=[_error_and_headers_middleware])
    package_root = Path(__file__).resolve().parent
    jinja_env = Environment(
        loader=FileSystemLoader(str(package_root / "templates")),
        autoescape=select_autoescape(enabled_extensions=("html",), default_for_string=True),
    )
    jinja_env.filters["reltime"] = _relative_time
    jinja_env.filters["abstime"] = _absolute_time
    app["jinja_env"] = jinja_env
    app["bot"] = bot
    app["database"] = database
    app["base_url"] = base_url
    app["client_id"] = client_id
    app["client_secret"] = client_secret
    app["secret_key"] = secret_key
    app["cookie_secure"] = urllib.parse.urlparse(base_url).scheme.lower() == "https"
    app["global_admin_ids"] = global_admin_ids if global_admin_ids is not None else frozenset()
    app["permission_cache"] = {}

    app.router.add_get("/", _index)
    app.router.add_get("/stats", _stats_json)
    app.router.add_get("/global/stats", _global_stats)
    app.router.add_get("/auth/login", _auth_login)
    app.router.add_get("/auth/callback", _auth_callback)
    app.router.add_post("/auth/logout", _auth_logout)
    app.router.add_get("/admin/servers", _admin_servers)
    app.router.add_get("/dashboard", _dashboard_index)
    app.router.add_get("/dashboard/{guild_id}", _dashboard_guild)
    app.router.add_get("/dashboard/{guild_id}/stats", _dashboard_guild_stats)
    app.router.add_get("/dashboard/{guild_id}/tables", _dashboard_guild_tables)
    app.router.add_get("/dashboard/{guild_id}/members", _dashboard_guild_members)
    app.router.add_get("/dashboard/{guild_id}/members/search", _dashboard_guild_members_search)
    app.router.add_post("/dashboard/{guild_id}/settings", _dashboard_guild_settings_post)
    app.router.add_post("/dashboard/{guild_id}/tables/{subscription_id}/unwatch", _dashboard_guild_unwatch_post)
    app.router.add_post("/dashboard/{guild_id}/members/link", _dashboard_guild_members_link_post)
    app.router.add_post("/dashboard/{guild_id}/members/{discord_user_id}/unlink", _dashboard_guild_members_unlink_post)
    app.router.add_static("/static/", str(package_root / "static"))

    return app


async def start_dashboard(app: web.Application, *, host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Bind the dashboard and return its runner; raises immediately if the port is taken."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    try:
        await site.start()
    except OSError:
        await runner.cleanup()
        raise
    LOGGER.info("Dashboard listening on http://%s:%d", host, port)
    return runner
