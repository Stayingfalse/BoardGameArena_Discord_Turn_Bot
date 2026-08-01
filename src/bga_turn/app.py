from __future__ import annotations

import asyncio
import logging
import os
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

import discord
from discord.ext import commands
from dotenv import load_dotenv

WORKDIR = Path.cwd()
load_dotenv(dotenv_path=WORKDIR / ".env", encoding="utf-8-sig")

from .bga_client import BgaClient
from .commands_bga import BgaCommands
from .database import Database
from .i18n import tr
from .monitor import BgaMonitor, LinkSelfPersistentView


def setup_logging() -> None:
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def env_flag(name: str, *, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _validate_dashboard_config(
    *,
    dashboard_base_url: str,
    client_id: str,
    client_secret: str,
    dashboard_secret_key: str,
) -> str:
    missing = [
        name
        for name, value in (
            ("DISCORD_CLIENT_ID", client_id),
            ("DISCORD_CLIENT_SECRET", client_secret),
            ("DASHBOARD_SECRET_KEY", dashboard_secret_key),
            ("DASHBOARD_BASE_URL", dashboard_base_url),
        )
        if not value.strip()
    ]
    if missing:
        raise RuntimeError(
            "Dashboard enabled but missing required environment variables: "
            + ", ".join(missing)
        )

    parsed = urlparse(dashboard_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("DASHBOARD_BASE_URL must be an absolute http(s) URL with host.")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise RuntimeError("DASHBOARD_BASE_URL must not include path, params, query, or fragment.")

    host = (parsed.hostname or "").lower()
    is_local_http = parsed.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not is_local_http:
        raise RuntimeError(
            "DASHBOARD_BASE_URL must use HTTPS for non-local deployments so auth cookies are secure."
        )

    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


class BgaDiscordBot(commands.Bot):
    def __init__(
        self,
        *,
        database: Database,
        bga_client: BgaClient,
        poll_seconds: int,
        dev_guild_id: int | None,
        clear_global_commands: bool,
        default_recruiting_only: bool = False,
        default_delete_invite_message: bool = False,
        default_forced_channel_id: str | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.database = database
        self.bga_client = bga_client
        self.dev_guild_id = dev_guild_id
        self.clear_global_commands = clear_global_commands
        self.monitor = BgaMonitor(
            self,
            database,
            bga_client,
            poll_seconds,
            default_recruiting_only=default_recruiting_only,
            default_forced_channel_id=default_forced_channel_id,
        )
        self._default_delete_invite_message = default_delete_invite_message
        self.logger = logging.getLogger(__name__)
        self._startup_completed = False

    async def setup_hook(self) -> None:
        await self.add_cog(
            BgaCommands(
                self.database,
                self.bga_client,
                self.monitor,
                default_delete_invite_message=self._default_delete_invite_message,
            )
        )
        # Register a persistent view so the self-link button on existing messages
        # continues to work after a bot restart.
        self.add_view(LinkSelfPersistentView())

    async def _clear_global_commands(self) -> int:
        deleted_count = 0
        for command in await self.tree.fetch_commands():
            await command.delete()
            deleted_count += 1
        return deleted_count

    async def on_ready(self) -> None:
        if self._startup_completed:
            return

        if self.clear_global_commands:
            if self.dev_guild_id is None:
                self.logger.info(tr("global_cleanup_skipped_no_guild"))
            else:
                deleted_count = await self._clear_global_commands()
                self.logger.info(tr("global_cleanup_done", count=deleted_count))

        if self.dev_guild_id is not None:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self.logger.info(tr("guild_sync", guild_id=self.dev_guild_id, count=len(synced)))
        else:
            synced = await self.tree.sync()
            self.logger.info(tr("global_sync", count=len(synced)))

        self._log_invite_url()
        self.monitor.start()
        self._startup_completed = True

    async def close(self) -> None:
        self.monitor.stop()
        self.database.close()
        await super().close()

    def _log_invite_url(self) -> None:
        application_id = self.application_id or getattr(self.user, "id", None)
        if application_id is None:
            return
        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            read_message_history=True,
            manage_messages=True,
        )
        invite_url = (
            "https://discord.com/oauth2/authorize"
            f"?client_id={application_id}"
            "&scope=bot%20applications.commands"
            f"&permissions={permissions.value}"
        )
        self.logger.info(tr("bot_invite_url", invite_url=invite_url))


async def _run_bot(
    bot: BgaDiscordBot,
    token: str,
    *,
    dashboard_enabled: bool,
    dashboard_port: int,
    dashboard_base_url: str,
    client_id: str,
    client_secret: str,
    dashboard_secret_key: str,
) -> None:
    logger = logging.getLogger(__name__)
    if dashboard_enabled:
        try:
            from .dashboard import create_dashboard_app, run_dashboard
        except ImportError as exc:
            logger.warning("Dashboard disabled: failed to import aiohttp (%s).", exc)
            dashboard_enabled = False

    if dashboard_enabled:
        app = create_dashboard_app(  # type: ignore[possibly-undefined]
            bot=bot,
            database=bot.database,
            base_url=dashboard_base_url,
            client_id=client_id,
            client_secret=client_secret,
            secret_key=dashboard_secret_key,
        )
        dashboard_task = asyncio.create_task(
            run_dashboard(app, port=dashboard_port),  # type: ignore[possibly-undefined]
            name="dashboard",
        )
        logger.info("Dashboard started on port %d.", dashboard_port)
    else:
        dashboard_task = None

    try:
        async with bot:
            await bot.start(token)
    finally:
        if dashboard_task is not None:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    setup_logging()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(tr("missing_discord_token"))

    db_path = Path(os.getenv("BGA_DB_PATH", "bga_bot.db")).expanduser()
    if not db_path.is_absolute():
        db_path = WORKDIR / db_path
    schema_sql = files("bga_turn").joinpath("schema.sql").read_text(encoding="utf-8")
    poll_seconds = int(os.getenv("BGA_POLL_SECONDS", "15"))
    dev_guild_id = os.getenv("DISCORD_GUILD_ID")
    clear_global_commands = env_flag("DISCORD_CLEAR_GLOBAL_COMMANDS")
    enable_tableinfos_fallback = env_flag("BGA_ENABLE_TABLEINFOS_FALLBACK")
    default_recruiting_only = env_flag("BGA_RECRUITING_ONLY")
    default_delete_invite_message = env_flag("BGA_DELETE_INVITE_MESSAGE")
    default_forced_channel_id = os.getenv("BGA_FORCED_CHANNEL_ID") or None
    websocket_url = os.getenv("BGA_WS_URL", "wss://ws-x1.boardgamearena.com/connection/websocket")

    dashboard_enabled = env_flag("DASHBOARD_ENABLED")
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "8080"))
    dashboard_base_url = os.getenv("DASHBOARD_BASE_URL", "http://localhost:8080").rstrip("/")
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET", "")
    dashboard_secret_key = os.getenv("DASHBOARD_SECRET_KEY", "")
    if dashboard_enabled:
        dashboard_base_url = _validate_dashboard_config(
            dashboard_base_url=dashboard_base_url,
            client_id=client_id,
            client_secret=client_secret,
            dashboard_secret_key=dashboard_secret_key,
        )

    database = Database(db_path=db_path, schema_sql=schema_sql)
    database.initialize()
    bga_client = BgaClient(
        timeout=30,
        websocket_url=websocket_url,
        enable_tableinfos_fallback=enable_tableinfos_fallback,
    )

    bot = BgaDiscordBot(
        database=database,
        bga_client=bga_client,
        poll_seconds=poll_seconds,
        dev_guild_id=int(dev_guild_id) if dev_guild_id else None,
        clear_global_commands=clear_global_commands,
        default_recruiting_only=default_recruiting_only,
        default_delete_invite_message=default_delete_invite_message,
        default_forced_channel_id=default_forced_channel_id,
    )
    asyncio.run(
        _run_bot(
            bot,
            token,
            dashboard_enabled=dashboard_enabled,
            dashboard_port=dashboard_port,
            dashboard_base_url=dashboard_base_url,
            client_id=client_id,
            client_secret=client_secret,
            dashboard_secret_key=dashboard_secret_key,
        )
    )


if __name__ == "__main__":
    main()
