from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from .bga_client import BgaClient, BgaClientError, BgaNotPublicError
from .database import Database
from .i18n import tr
from .monitor import BgaMonitor
from .models import WatchSubscription
from .utils import build_table_url, format_game_name, parse_public_table_url, parse_table_id

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class WatchRegistrationResult:
    subscription: WatchSubscription
    source: str
    detected_player_names: dict[str, str]
    init_state: str


class BgaCommands(commands.Cog):
    bga = app_commands.Group(name="bga", description=tr("command_group_description"))

    _URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)

    def __init__(
        self,
        database: Database,
        bga_client: BgaClient,
        monitor: BgaMonitor,
        *,
        default_delete_invite_message: bool = False,
    ) -> None:
        self.database = database
        self.bga_client = bga_client
        self.monitor = monitor
        self._default_delete_invite_message = default_delete_invite_message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Log every ``/bga`` command invocation. Never blocks the command."""
        self._log_command_invocation(interaction)
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not message.content or not any(host in message.content.lower() for host in ("boardgamearena.com", "bga.li/")):
            return

        table_references = self._extract_table_references(message.content)
        if not table_references:
            return

        registered_subscriptions: list[int] = []
        for table_reference in table_references:
            try:
                registration = await self._register_watch(
                    guild_id=str(message.guild.id),
                    channel_id=str(message.channel.id),
                    created_by_discord_user_id=str(message.author.id),
                    table_or_url=table_reference,
                )
            except (BgaClientError, BgaNotPublicError, ValueError) as exc:
                LOGGER.debug(
                    tr(
                        "auto_watch_skipped",
                        table_reference=table_reference,
                        channel_id=message.channel.id,
                        error=exc,
                    )
                )
                continue
            registered_subscriptions.append(registration.subscription.subscription_id)

        if registered_subscriptions:
            LOGGER.info(
                tr(
                    "auto_watch_registered",
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                )
            )
            guild_settings = self.database.get_guild_settings(
                str(message.guild.id),
                default_delete_invite_message=self._default_delete_invite_message,
            )
            await self.monitor.refresh_now()
            if guild_settings.delete_invite_message and await self.monitor.wait_for_active_messages(
                registered_subscriptions
            ):
                try:
                    await message.delete()
                    LOGGER.info(
                        tr(
                            "trigger_message_deleted",
                            channel_id=message.channel.id,
                            guild_id=message.guild.id,
                        )
                    )
                except discord.NotFound:
                    pass
                except discord.DiscordException as exc:
                    LOGGER.warning(
                        tr(
                            "trigger_message_delete_failed",
                            channel_id=message.channel.id,
                            error=exc,
                        )
                    )
            elif guild_settings.delete_invite_message:
                LOGGER.info(
                    tr(
                        "trigger_message_delete_skipped_no_replacement",
                        channel_id=message.channel.id,
                        guild_id=message.guild.id,
                    )
                )

    @staticmethod
    def _flatten_command_options(options: list[dict] | None) -> list[str]:
        # Slash options nest subcommand (type 1) / subcommand-group (type 2)
        # payloads, so walk down to the leaf options that carry actual values.
        parts: list[str] = []
        for option in options or []:
            if option.get("type") in (1, 2):
                parts.extend(BgaCommands._flatten_command_options(option.get("options")))
            else:
                parts.append(f"{option.get('name')}={option.get('value')!r}")
        return parts

    @staticmethod
    def _command_name_from_data(data: dict | None) -> str:
        # Rebuild "bga watch" from the raw payload when interaction.command is
        # unavailable, by descending through nested subcommand options.
        names: list[str] = []
        node = data or {}
        while node:
            name = node.get("name")
            if name:
                names.append(str(name))
            options = node.get("options") or []
            node = next((opt for opt in options if opt.get("type") in (1, 2)), None)
        return " ".join(names) or "unknown"

    @classmethod
    def _log_command_invocation(cls, interaction: discord.Interaction) -> None:
        command = interaction.command
        command_name = (
            command.qualified_name
            if command is not None
            else cls._command_name_from_data(interaction.data)
        )
        params = cls._flatten_command_options((interaction.data or {}).get("options"))
        user = interaction.user
        LOGGER.info(
            tr(
                "command_invocation",
                command=command_name,
                user_name=getattr(user, "display_name", str(user)),
                user_id=getattr(user, "id", "unknown"),
                guild_id=interaction.guild_id if interaction.guild_id is not None else "dm",
                channel_id=interaction.channel_id if interaction.channel_id is not None else "n/a",
                params=", ".join(params) or "none",
            )
        )

    @staticmethod
    def _has_manage_permissions(interaction: discord.Interaction) -> bool:
        permissions = interaction.permissions
        return permissions.manage_guild or permissions.administrator

    @staticmethod
    async def _send_ephemeral_embeds(
        interaction: discord.Interaction,
        embeds: list[discord.Embed],
    ) -> None:
        if not embeds:
            embeds = [discord.Embed(description="")]

        batches = [embeds[index : index + 10] for index in range(0, len(embeds), 10)]
        first_batch, *remaining_batches = batches
        if interaction.response.is_done():
            await interaction.followup.send(embeds=first_batch, ephemeral=True)
        else:
            await interaction.response.send_message(embeds=first_batch, ephemeral=True)

        for batch in remaining_batches:
            await interaction.followup.send(embeds=batch, ephemeral=True)

    @bga.command(name="link-member", description=tr("command_link_member_description"))
    @app_commands.describe(
        member=tr("command_link_member_member"),
        bga_player_name=tr("command_link_member_name"),
        bga_player_id=tr("command_link_member_id"),
    )
    async def link_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        bga_player_name: str | None = None,
        bga_player_id: str | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return
        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_link"),
                ephemeral=True,
            )
            return

        candidate_id = (bga_player_id or "").strip()
        candidate_name = (bga_player_name or "").strip()
        if not candidate_id and not candidate_name:
            await interaction.response.send_message(
                tr("error_need_bga_name_or_id"),
                ephemeral=True,
            )
            return
        if candidate_id and not candidate_id.isdigit():
            await interaction.response.send_message(
                tr("error_invalid_bga_player_id"),
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        self.database.upsert_linked_user(
            discord_user_id=str(member.id),
            bga_player_id=candidate_id,
            bga_player_name=candidate_name,
        )
        linked_user = self.database.get_linked_user(str(member.id))
        if linked_user is None:
            raise RuntimeError("Failed to load the linked BGA user after saving it.")
        name_display = linked_user.bga_player_name or tr("link_missing_value_placeholder")
        id_display = linked_user.bga_player_id or tr("link_missing_value_placeholder")
        await interaction.response.send_message(
            tr(
                "link_saved",
                member_mention=member.mention,
                bga_name=name_display,
                bga_id=id_display,
            ),
            ephemeral=True,
        )

    @bga.command(name="unlink-member", description=tr("command_unlink_member_description"))
    @app_commands.describe(member=tr("command_unlink_member_member"))
    async def unlink_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return
        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_unlink"),
                ephemeral=True,
            )
            return

        removed = self.database.remove_linked_user(str(member.id))
        if not removed:
            await interaction.response.send_message(
                tr("unlink_not_found", member_mention=member.mention),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr("unlink_saved", member_mention=member.mention),
            ephemeral=True,
        )

    async def _register_watch(
        self,
        *,
        guild_id: str,
        channel_id: str,
        created_by_discord_user_id: str,
        table_or_url: str,
    ) -> WatchRegistrationResult:
        table_id, table_url, base_url, gameserver, game_name = parse_public_table_url(table_or_url)

        snapshot = None
        state = None
        resolved_table_url = table_url
        resolved_gameserver = gameserver
        resolved_game_name = game_name
        detected_player_names: dict[str, str] = {}
        init_state = tr("watch_init_waiting_event")
        source = "manual_registration"

        if not gameserver or not game_name:
            snapshot = await asyncio.to_thread(
                self.bga_client.fetch_public_table_snapshot, table_id, base_url
            )
            if snapshot.is_finished:
                raise BgaNotPublicError(tr("error_resolve_missing_game_server", table_id=table_id))
            resolved_table_url = snapshot.table_url or build_table_url(table_id)
            resolved_gameserver = snapshot.gameserver
            resolved_game_name = snapshot.game_name
            detected_player_names = dict(snapshot.player_names)
            if snapshot.can_watch_turns:
                table_url = resolved_table_url
                gameserver = resolved_gameserver
                game_name = resolved_game_name
            else:
                init_state = tr("watch_init_waiting_players")
                source = f"tableinfos:{snapshot.status or 'pending'}"

        if gameserver and game_name:
            table_info = self.bga_client.build_public_table_info(
                table_id=table_id,
                table_url=table_url,
                base_url=base_url,
                gameserver=gameserver,
                game_name=game_name,
            )
            state = await self.bga_client.probe_public_table(table_info, known_player_names={})
            resolved_table_url = table_info.table_url
            resolved_gameserver = table_info.gameserver
            resolved_game_name = table_info.game_name
            detected_player_names = dict(state.player_names)
            source = state.source

        subscription = self.database.upsert_watch_subscription(
            table_id=table_id,
            table_url=resolved_table_url or build_table_url(table_id),
            base_url=base_url,
            gameserver=resolved_gameserver,
            guild_id=guild_id,
            channel_id=channel_id,
            created_by_discord_user_id=created_by_discord_user_id,
            game_name=resolved_game_name or None,
        )
        persisted_player_names = dict(subscription.player_names)
        persisted_player_names.update(detected_player_names)
        self.database.update_watch_state(
            subscription_id=subscription.subscription_id,
            last_packet_id=subscription.last_packet_id,
            waiting_ids=[] if state is None else subscription.last_waiting_ids,
            player_names=persisted_player_names,
            is_initialized=subscription.is_initialized if state is not None else False,
            game_name=resolved_game_name or subscription.game_name,
        )
        await asyncio.to_thread(
            self.database.enrich_linked_users_from_players, persisted_player_names
        )
        if state is None:
            subscription = self.database.get_watch_subscription(subscription.subscription_id) or subscription
            return WatchRegistrationResult(
                subscription=subscription,
                source=source,
                detected_player_names=detected_player_names,
                init_state=init_state,
            )

        subscription = self.database.get_watch_subscription(subscription.subscription_id) or subscription
        return WatchRegistrationResult(
            subscription=subscription,
            source=source,
            detected_player_names=detected_player_names,
            init_state=(
                tr("watch_init_active")
                if subscription.is_initialized
                else tr("watch_init_waiting_event")
            ),
        )

    def _extract_table_references(cls, message_content: str) -> list[str]:
        references: list[str] = []
        seen: set[str] = set()
        for match in cls._URL_PATTERN.finditer(message_content):
            candidate = match.group(0).rstrip(".,!?)]}>\"'")
            try:
                table_id = parse_table_id(candidate)
            except ValueError:
                continue
            if table_id in seen:
                continue
            seen.add(table_id)
            references.append(candidate)
        return references

    @bga.command(name="status", description=tr("command_status_description"))
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        subscriptions = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        if not subscriptions:
            await interaction.response.send_message(
                tr("status_none"),
                ephemeral=True,
            )
            return

        embeds = [
            discord.Embed(
                title=f"🩺 {tr('status_header')}",
                description=tr("status_embed_summary", count=len(subscriptions)),
                color=discord.Color.green(),
            )
        ]
        for index, subscription in enumerate(subscriptions, start=1):
            if not subscription.is_initialized and not (subscription.gameserver or "").strip():
                state = tr("status_waiting_for_start")
            elif not subscription.is_initialized:
                state = tr("status_unknown")
            elif subscription.last_waiting_ids:
                linked_users = self.database.get_linked_users_by_bga_ids(
                    subscription.last_waiting_ids
                )
                if linked_users:
                    mentions = ", ".join(f"<@{item.discord_user_id}>" for item in linked_users)
                    state = tr("status_waiting_for", mentions=mentions)
                else:
                    state = tr("status_waiting_no_link")
            else:
                state = tr("status_no_waiting")

            card = discord.Embed(
                title=tr(
                    "status_card_title",
                    table_id=subscription.table_id,
                    game_name=format_game_name(subscription.game_name),
                ),
                color=discord.Color.green(),
            )
            card.add_field(
                name=f"📍 {tr('label_channel')}",
                value=f"<#{subscription.channel_id}>",
                inline=True,
            )
            card.add_field(
                name=f"⏳ {tr('label_waiting_ids')}",
                value=f"`{', '.join(subscription.last_waiting_ids) or tr('value_none')}`",
                inline=True,
            )
            card.add_field(
                name=f"🧠 {tr('label_state')}",
                value=state,
                inline=False,
            )
            card.set_footer(text=tr("status_card_footer", index=index, total=len(subscriptions)))
            embeds.append(card)

        await self._send_ephemeral_embeds(interaction, embeds)

    @bga.command(name="settings", description=tr("command_settings_description"))
    @app_commands.describe(
        recruiting_only=tr("command_settings_recruiting_only"),
        delete_invite_message=tr("command_settings_delete_invite_message"),
        forced_channel=tr("command_settings_forced_channel"),
        clear_forced_channel=tr("command_settings_clear_forced_channel"),
    )
    async def settings_command(
        self,
        interaction: discord.Interaction,
        recruiting_only: bool | None = None,
        delete_invite_message: bool | None = None,
        forced_channel: discord.TextChannel | None = None,
        clear_forced_channel: bool | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return
        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_settings"),
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        current = self.database.get_guild_settings(
            guild_id,
            default_recruiting_only=self.monitor._default_recruiting_only,
            default_delete_invite_message=self._default_delete_invite_message,
            default_forced_channel_id=self.monitor._default_forced_channel_id,
        )

        # No changes requested — show current settings.
        if recruiting_only is None and delete_invite_message is None and forced_channel is None and clear_forced_channel is None:
            channel_display = f"<#{current.forced_channel_id}>" if current.forced_channel_id else tr("value_none")
            await interaction.response.send_message(
                tr(
                    "settings_display",
                    recruiting_only=current.recruiting_only,
                    delete_invite_message=current.delete_invite_message,
                    forced_channel_id=channel_display,
                ),
                ephemeral=True,
            )
            return

        new_recruiting_only = recruiting_only if recruiting_only is not None else current.recruiting_only
        new_delete_invite_message = delete_invite_message if delete_invite_message is not None else current.delete_invite_message
        if clear_forced_channel:
            new_forced_channel_id: str | None = None
        elif forced_channel is not None:
            new_forced_channel_id = str(forced_channel.id)
        else:
            new_forced_channel_id = current.forced_channel_id

        self.database.upsert_guild_settings(
            guild_id=guild_id,
            recruiting_only=new_recruiting_only,
            delete_invite_message=new_delete_invite_message,
            forced_channel_id=new_forced_channel_id,
        )

        channel_display = f"<#{new_forced_channel_id}>" if new_forced_channel_id else tr("value_none")
        await interaction.response.send_message(
            tr(
                "settings_saved",
                recruiting_only=new_recruiting_only,
                delete_invite_message=new_delete_invite_message,
                forced_channel_id=channel_display,
            ),
            ephemeral=True,
        )
