"""Codenames link detection and the ``/codenames`` commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..database import AsyncDatabase, Database
from ..i18n import tr
from .links import build_room_url, extract_room_ids
from .models import CodenamesWatch
from .monitor import CodenamesMonitor

LOGGER = logging.getLogger(__name__)

_MAX_ROOMS_PER_MESSAGE = 3
_WATCH_REACTION = "🕵️"


class CodenamesCommands(commands.Cog):
    codenames = app_commands.Group(name="codenames", description=tr("cn_command_group_description"))

    def __init__(
        self, database: Database, monitor: CodenamesMonitor, *, default_forced_channel_id: str | None = None
    ) -> None:
        self.db = AsyncDatabase(database)
        self.monitor = monitor
        self._default_forced_channel_id = default_forced_channel_id

    # ------------------------------------------------------------------
    # Link detection
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        room_ids = extract_room_ids(_message_text(message))[:_MAX_ROOMS_PER_MESSAGE]
        if not room_ids:
            return
        channel_id = await self._target_channel_id(message.guild, str(message.channel.id))
        watched = False
        for room_id in room_ids:
            try:
                await self.monitor.watch_room(
                    room_id,
                    guild_id=str(message.guild.id),
                    channel_id=channel_id,
                    created_by_discord_user_id=str(message.author.id),
                )
                watched = True
            except Exception:
                LOGGER.exception("Could not watch Codenames room %s", room_id)
        if watched:
            LOGGER.info("Watching Codenames room(s) %s in guild %s", ", ".join(room_ids), message.guild.id)
            try:
                await message.add_reaction(_WATCH_REACTION)
            except discord.DiscordException:
                pass

    async def _target_channel_id(self, guild: discord.Guild, channel_id: str) -> str:
        """The server's forced notification channel if it is valid, else *channel_id*."""
        settings = await self.db.get_guild_settings(
            str(guild.id), default_forced_channel_id=self._default_forced_channel_id
        )
        forced = settings.forced_channel_id or ""
        if forced.isdecimal() and guild.get_channel_or_thread(int(forced)) is not None:
            return forced
        return channel_id

    # ------------------------------------------------------------------
    # /codenames players
    # ------------------------------------------------------------------

    @codenames.command(name="players", description=tr("cn_command_players_description"))
    @app_commands.describe(room=tr("cn_param_room_description"))
    async def players(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member | None = None,
        player3: discord.Member | None = None,
        player4: discord.Member | None = None,
        player5: discord.Member | None = None,
        room: str | None = None,
    ) -> None:
        watch = await self._resolve_watch(interaction, room)
        if watch is None:
            return
        members = [m for m in (player1, player2, player3, player4, player5) if m is not None and not m.bot]
        user_ids = list(dict.fromkeys(str(m.id) for m in members))
        if not user_ids:
            await interaction.response.send_message(tr("cn_players_none"), ephemeral=True)
            return
        await self.db.add_codenames_watch_members(watch.watch_id, user_ids)
        mentions = " ".join(f"<@{user_id}>" for user_id in user_ids)
        await interaction.response.send_message(
            tr("cn_players_invite", mentions=mentions, url=build_room_url(watch.room_id)),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(u)) for u in user_ids]),
        )
        await self.monitor.refresh_room(watch.room_id)

    # ------------------------------------------------------------------
    # /codenames status
    # ------------------------------------------------------------------

    @codenames.command(name="status", description=tr("cn_command_status_description"))
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(tr("cn_server_only"), ephemeral=True)
            return
        watches = await self.db.list_codenames_watches_for_guild(str(interaction.guild_id))
        lines = [] if watches else [tr("cn_status_none")]
        for watch in watches:
            game = await self.db.get_latest_codenames_game(watch.watch_id)
            if game is None:
                state = tr("cn_status_entry_connecting")
            else:
                state = tr(
                    "cn_status_entry_game",
                    number=game.game_number,
                    state=tr(_LIFECYCLE_KEYS.get(game.lifecycle_state, "cn_lifecycle_lobby")),
                )
            lines.append(
                tr("cn_status_entry", room=watch.room_id, url=build_room_url(watch.room_id),
                   channel=f"<#{watch.channel_id}>", state=state)
            )
        stats = await self.db.get_codenames_stats(str(interaction.guild_id))
        if stats["finished_games"]:
            lines.append(
                tr(
                    "cn_status_summary",
                    games=stats["finished_games"],
                    red=stats["red_wins"],
                    blue=stats["blue_wins"],
                )
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /codenames unwatch
    # ------------------------------------------------------------------

    @codenames.command(name="unwatch", description=tr("cn_command_unwatch_description"))
    @app_commands.describe(room=tr("cn_param_room_description"))
    async def unwatch(self, interaction: discord.Interaction, room: str | None = None) -> None:
        watch = await self._resolve_watch(interaction, room)
        if watch is None:
            return
        permissions = getattr(interaction.user, "guild_permissions", None)
        allowed = str(interaction.user.id) == watch.created_by_discord_user_id or (
            permissions is not None and (permissions.manage_messages or permissions.manage_guild)
        )
        if not allowed:
            await interaction.response.send_message(tr("cn_unwatch_not_allowed"), ephemeral=True)
            return
        await self.monitor.unwatch(watch)
        await interaction.response.send_message(tr("cn_unwatch_done", room=watch.room_id), ephemeral=True)

    async def _resolve_watch(self, interaction: discord.Interaction, room: str | None) -> CodenamesWatch | None:
        """The watch for *room* (a link or ID) in this channel, or the channel's only watch.
        Replies with the reason and returns ``None`` when there isn't exactly one."""
        if interaction.guild_id is None:
            await interaction.response.send_message(tr("cn_server_only"), ephemeral=True)
            return None
        watches = await self.db.list_codenames_watches_for_guild(str(interaction.guild_id))
        if room:
            room_ids = extract_room_ids(room) or extract_room_ids(f"codenames.game/r/{room.strip()}")
            room_id = room_ids[0] if room_ids else room.strip().lower()
            matches = [w for w in watches if w.room_id == room_id]
            here = [w for w in matches if w.channel_id == str(interaction.channel_id)]
            matches = here or matches
        else:
            matches = [w for w in watches if w.channel_id == str(interaction.channel_id)]
        if len(matches) == 1:
            return matches[0]
        key = "cn_watch_ambiguous" if matches else "cn_watch_not_found"
        await interaction.response.send_message(tr(key), ephemeral=True)
        return None


_LIFECYCLE_KEYS = {
    "lobby": "cn_lifecycle_lobby",
    "in_progress": "cn_lifecycle_in_progress",
    "finished": "cn_lifecycle_finished",
}


def _message_text(message: discord.Message) -> str:
    """The message's text plus the text of any forwarded messages it carries."""
    parts = [message.content or ""]
    for snapshot in getattr(message, "message_snapshots", None) or []:
        parts.append(getattr(snapshot, "content", "") or "")
    return "\n".join(parts)
