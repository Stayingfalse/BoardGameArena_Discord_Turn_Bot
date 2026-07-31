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
import hashlib
import hmac
import html
import json
import logging
import time
import urllib.parse
from typing import TYPE_CHECKING

from aiohttp import web

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _encode_session(data: dict, secret: str) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    b64 = urllib.parse.quote(payload)
    sig = _sign(b64, secret)
    return f"{b64}.{sig}"


def _decode_session(cookie: str, secret: str) -> dict | None:
    if "." not in cookie:
        return None
    b64, sig = cookie.rsplit(".", 1)
    expected = _sign(b64, secret)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return json.loads(urllib.parse.unquote(b64))
    except Exception:
        return None


def _get_session(request: web.Request) -> dict | None:
    secret: str = request.app["secret_key"]
    if not secret:
        return None
    cookie = request.cookies.get(_COOKIE_NAME)
    if not cookie:
        return None
    return _decode_session(cookie, secret)


def _set_session(response: web.Response, data: dict, secret: str) -> None:
    value = _encode_session(data, secret)
    response.set_cookie(
        _COOKIE_NAME,
        value,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
    )


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


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------

_CSS = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Segoe UI',Arial,sans-serif;background:#1a1a2e;color:#e0e0e0;min-height:100vh}
  a{color:#7289da;text-decoration:none}
  a:hover{text-decoration:underline}
  .nav{background:#0d0d1a;padding:12px 24px;display:flex;align-items:center;gap:16px;border-bottom:1px solid #2c2c54}
  .nav h1{font-size:1.1rem;color:#fff}
  .nav .spacer{flex:1}
  .btn{display:inline-block;padding:8px 18px;border-radius:6px;font-size:.9rem;cursor:pointer;border:none}
  .btn-primary{background:#7289da;color:#fff}
  .btn-primary:hover{background:#5b73c7}
  .btn-danger{background:#ed4245;color:#fff}
  .btn-danger:hover{background:#c23b3e}
  .btn-secondary{background:#4f545c;color:#fff}
  .btn-secondary:hover{background:#686d75}
  .container{max-width:960px;margin:0 auto;padding:32px 24px}
  .hero{text-align:center;padding:64px 24px}
  .hero h2{font-size:2rem;color:#fff;margin-bottom:16px}
  .hero p{font-size:1.05rem;color:#b0b0c0;max-width:600px;margin:0 auto 28px}
  .stats-row{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;margin:24px 0}
  .stat-card{background:#16213e;border:1px solid #2c2c54;border-radius:10px;padding:20px 36px;text-align:center}
  .stat-card .num{font-size:2rem;font-weight:700;color:#7289da}
  .stat-card .label{font-size:.85rem;color:#888;margin-top:4px}
  .card{background:#16213e;border:1px solid #2c2c54;border-radius:10px;padding:20px;margin-bottom:16px}
  .card h3{color:#fff;margin-bottom:8px;font-size:1rem}
  .card-row{display:flex;align-items:center;gap:12px}
  .card-row img{border-radius:50%;width:40px;height:40px}
  .card-stats{font-size:.85rem;color:#888;margin-top:6px}
  .form-group{margin-bottom:18px}
  .form-group label{display:block;font-size:.9rem;color:#b0b0c0;margin-bottom:6px}
  .form-group input[type=text]{width:100%;padding:9px 12px;background:#0d0d1a;border:1px solid #2c2c54;
    border-radius:6px;color:#e0e0e0;font-size:.95rem}
  .form-group input[type=text]:focus{outline:none;border-color:#7289da}
  .toggle-row{display:flex;align-items:center;gap:10px}
  .toggle{position:relative;display:inline-block;width:44px;height:24px}
  .toggle input{opacity:0;width:0;height:0}
  .slider{position:absolute;inset:0;background:#333;border-radius:24px;transition:.2s}
  .slider:before{content:'';position:absolute;height:18px;width:18px;left:3px;bottom:3px;
    background:#fff;border-radius:50%;transition:.2s}
  input:checked+.slider{background:#7289da}
  input:checked+.slider:before{transform:translateX(20px)}
  .alert-success{background:#1e3a2f;border:1px solid #2d6a4f;border-radius:6px;
    padding:10px 16px;margin-bottom:16px;color:#74c69d}
  .breadcrumb{font-size:.85rem;color:#888;margin-bottom:16px}
  .breadcrumb a{color:#7289da}
  .section-title{font-size:1.2rem;color:#fff;margin-bottom:20px}
  hr{border:none;border-top:1px solid #2c2c54;margin:24px 0}
  .features{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-top:32px}
  .feature{background:#16213e;border:1px solid #2c2c54;border-radius:10px;padding:20px}
  .feature h4{color:#fff;margin-bottom:8px}
  .feature p{font-size:.85rem;color:#888}
  .empty{color:#888;font-style:italic;font-size:.9rem}
</style>
"""

_NAV_LOGGED_IN = """
<nav class="nav">
  <h1>🎲 BGA Turn Bot</h1>
  <span class="spacer"></span>
  <a href="/dashboard">Dashboard</a>
  &nbsp;|&nbsp;
  <span style="color:#888;font-size:.9rem">{username}</span>
</nav>
"""

_NAV_ANONYMOUS = """
<nav class="nav">
  <h1>🎲 BGA Turn Bot</h1>
  <span class="spacer"></span>
  <a href="/auth/login" class="btn btn-secondary">Login with Discord</a>
</nav>
"""

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title} — BGA Turn Bot</title>
  {css}
</head>
<body>
{nav}
<div class="container">
{body}
</div>
</body>
</html>"""


def _page(title: str, body: str, *, session: dict | None = None) -> str:
    if session:
        nav = _NAV_LOGGED_IN.format(username=html.escape(session.get("username", "")))
    else:
        nav = _NAV_ANONYMOUS
    return _PAGE.format(title=html.escape(title), css=_CSS, nav=nav, body=body)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _index(request: web.Request) -> web.Response:
    session = _get_session(request)
    database: Database = request.app["database"]
    client_id: str = request.app["client_id"]
    base_url: str = request.app["base_url"]

    stats = await asyncio.to_thread(database.get_global_stats)
    add_bot_url = _build_add_bot_url(client_id, base_url) if client_id else "#"

    body = f"""
<div class="hero">
  <h2>🎲 BGA Turn Bot</h2>
  <p>
    A self-hosted Discord bot that spectates public Board Game Arena tables and
    pings players when it's their turn — no BGA account, no password, no cookie.
  </p>
  {"" if not client_id else f'<a class="btn btn-primary" href="{html.escape(add_bot_url)}">➕ Add to Server</a>'}
  {"" if session else f'&nbsp; <a class="btn btn-secondary" href="/auth/login">Login to manage settings</a>'}
</div>
<div class="stats-row">
  <div class="stat-card">
    <div class="num">{stats["total"]}</div>
    <div class="label">Tables ever watched</div>
  </div>
  <div class="stat-card">
    <div class="num">{stats["recruiting"]}</div>
    <div class="label">Currently recruiting</div>
  </div>
</div>
<div class="features">
  <div class="feature">
    <h4>🔔 Turn notifications</h4>
    <p>Posts and updates a single Discord message per table as the active player changes.</p>
  </div>
  <div class="feature">
    <h4>🔗 Discord ↔ BGA linking</h4>
    <p>Members link their BGA account once and get @mentioned whenever it's their turn.</p>
  </div>
  <div class="feature">
    <h4>👥 Player following</h4>
    <p>Automatically watch every table a member joins, without adding each link by hand.</p>
  </div>
  <div class="feature">
    <h4>⚙️ Per-server settings</h4>
    <p>Recruiting-only mode, forced notification channel, invite message deletion — all per guild.</p>
  </div>
</div>
"""
    return web.Response(
        text=_page("Home", body, session=session),
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

    if not client_id or not secret:
        raise web.HTTPServiceUnavailable(reason="OAuth2 not configured (DISCORD_CLIENT_ID / DASHBOARD_SECRET_KEY missing).")

    state = hmac.new(secret.encode(), str(time.time()).encode(), hashlib.sha256).hexdigest()[:16]
    redirect_uri = f"{base_url}/auth/callback"
    url = _build_oauth2_url(client_id, redirect_uri, state)
    response = web.HTTPFound(location=url)
    response.set_cookie("oauth_state", state, max_age=300, httponly=True, samesite="Lax")
    raise response


async def _auth_callback(request: web.Request) -> web.Response:
    import aiohttp as _aiohttp

    client_id: str = request.app["client_id"]
    client_secret: str = request.app["client_secret"]
    base_url: str = request.app["base_url"]
    secret: str = request.app["secret_key"]

    code = request.rel_url.query.get("code", "")
    state = request.rel_url.query.get("state", "")
    expected_state = request.cookies.get("oauth_state", "")

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

    response = web.HTTPFound(location="/dashboard")
    response.del_cookie("oauth_state")
    _set_session(response, session_data, secret)
    raise response


async def _dashboard_index(request: web.Request) -> web.Response:
    session = _get_session(request)
    if session is None:
        raise web.HTTPFound(location="/auth/login")

    bot: BgaDiscordBot = request.app["bot"]
    database: Database = request.app["database"]

    # Guilds where the user has MANAGE_GUILD and bot is present
    bot_guild_ids = {str(g.id) for g in bot.guilds}
    user_guilds = session.get("guilds", [])

    managed_guilds = [
        g for g in user_guilds
        if (int(g.get("permissions", 0)) & _MANAGE_GUILD) and g["id"] in bot_guild_ids
    ]

    if not managed_guilds:
        body = """
<div class="section-title">Your Servers</div>
<p class="empty">No servers found where you have <strong>Manage Server</strong> permission
and the bot is installed.<br><br>
<a href="/" class="btn btn-primary">Add the bot to a server</a></p>
"""
        return web.Response(
            text=_page("Dashboard", body, session=session),
            content_type="text/html",
        )

    cards = []
    for guild in managed_guilds:
        guild_id = guild["id"]
        stats = await asyncio.to_thread(database.get_guild_stats, guild_id)
        icon_html = ""
        if guild.get("icon"):
            icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{guild['icon']}.png?size=64"
            icon_html = f'<img src="{html.escape(icon_url)}" alt="">'
        cards.append(f"""
<div class="card">
  <div class="card-row">
    {icon_html}
    <div>
      <h3><a href="/dashboard/{html.escape(guild_id)}">{html.escape(guild["name"])}</a></h3>
      <div class="card-stats">
        {stats["recruiting"]} recruiting &nbsp;·&nbsp;
        {stats["total"]} total watched &nbsp;·&nbsp;
        {stats["followed"]} followed players
      </div>
    </div>
  </div>
</div>""")

    body = f'<div class="section-title">Your Servers</div>' + "".join(cards)
    return web.Response(
        text=_page("Dashboard", body, session=session),
        content_type="text/html",
    )


async def _dashboard_guild(request: web.Request) -> web.Response:
    session = _get_session(request)
    if session is None:
        raise web.HTTPFound(location="/auth/login")

    guild_id = request.match_info["guild_id"]
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

    saved_msg = ""
    if request.rel_url.query.get("saved") == "1":
        saved_msg = '<div class="alert-success">✅ Settings saved.</div>'

    ro_checked = "checked" if settings.recruiting_only else ""
    di_checked = "checked" if settings.delete_invite_message else ""
    fc_value = html.escape(settings.forced_channel_id or "")

    body = f"""
<div class="breadcrumb"><a href="/dashboard">Dashboard</a> › {html.escape(guild_name)}</div>
<div class="section-title">{html.escape(guild_name)}</div>
{saved_msg}
<div class="stats-row" style="justify-content:flex-start">
  <div class="stat-card">
    <div class="num">{stats["recruiting"]}</div>
    <div class="label">Recruiting</div>
  </div>
  <div class="stat-card">
    <div class="num">{stats["total"]}</div>
    <div class="label">Total watched</div>
  </div>
  <div class="stat-card">
    <div class="num">{stats["followed"]}</div>
    <div class="label">Followed players</div>
  </div>
</div>
<hr>
<form method="post" action="/dashboard/{html.escape(guild_id)}/settings">
  <div class="form-group">
    <div class="toggle-row">
      <label class="toggle">
        <input type="checkbox" name="recruiting_only" value="1" {ro_checked}>
        <span class="slider"></span>
      </label>
      <span>Recruiting-only mode — remove watch once the game starts</span>
    </div>
  </div>
  <div class="form-group">
    <div class="toggle-row">
      <label class="toggle">
        <input type="checkbox" name="delete_invite_message" value="1" {di_checked}>
        <span class="slider"></span>
      </label>
      <span>Delete the Discord message that triggered auto-watch</span>
    </div>
  </div>
  <div class="form-group">
    <label>Forced notification channel ID <span style="color:#888;font-size:.8rem">(leave blank to post in the channel where the link was shared)</span></label>
    <input type="text" name="forced_channel_id" value="{fc_value}" placeholder="e.g. 1234567890123456789">
  </div>
  <button type="submit" class="btn btn-primary">Save settings</button>
</form>
"""
    return web.Response(
        text=_page(guild_name, body, session=session),
        content_type="text/html",
    )


async def _dashboard_guild_settings_post(request: web.Request) -> web.Response:
    session = _get_session(request)
    if session is None:
        raise web.HTTPFound(location="/auth/login")

    guild_id = request.match_info["guild_id"]
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
    raise web.HTTPFound(location=f"/dashboard/{guild_id}?saved=1")


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
    app["bot"] = bot
    app["database"] = database
    app["base_url"] = base_url
    app["client_id"] = client_id
    app["client_secret"] = client_secret
    app["secret_key"] = secret_key

    app.router.add_get("/", _index)
    app.router.add_get("/stats", _stats_json)
    app.router.add_get("/auth/login", _auth_login)
    app.router.add_get("/auth/callback", _auth_callback)
    app.router.add_get("/dashboard", _dashboard_index)
    app.router.add_get("/dashboard/{guild_id}", _dashboard_guild)
    app.router.add_post("/dashboard/{guild_id}/settings", _dashboard_guild_settings_post)

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
