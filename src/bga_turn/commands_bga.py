from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from .bga_client import BgaClient, BgaClientError, BgaNotPublicError, BgaPlayerNotFoundError
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

    _HELP_SEPARATOR = "⎯" * 24
    # Discord caps an embed description at 4096 characters (vs 2000 for content).
    _EMBED_DESCRIPTION_LIMIT = 4096
    _URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)

    def __init__(self, database: Database, bga_client: BgaClient, monitor: BgaMonitor, *, delete_invite_message: bool = False) -> None:
        self.database = database
        self.bga_client = bga_client
        self.monitor = monitor
        self._delete_invite_message = delete_invite_message

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

        registered_any = False
        for table_reference in table_references:
            try:
                await self._register_watch(
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
            registered_any = True

        if registered_any:
            LOGGER.info(
                tr(
                    "auto_watch_registered",
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                )
            )
            if self._delete_invite_message:
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
            await self.monitor.refresh_now()

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
    def _truncate_text(value: str, max_length: int) -> str:
        if max_length <= 0:
            return ""
        if len(value) <= max_length:
            return value
        if max_length == 1:
            return "…"
        return value[: max_length - 1].rstrip(", ") + "…"

    @classmethod
    def _format_bounded_list(cls, items: list[str], empty_text: str, max_length: int) -> str:
        if max_length <= 0:
            return ""
        if not items:
            return cls._truncate_text(empty_text, max_length)

        included: list[str] = []
        total_count = len(items)
        for index, item in enumerate(items):
            candidate_items = included + [item]
            candidate = ", ".join(candidate_items)
            remaining_count = total_count - index - 1
            if remaining_count > 0:
                suffix = tr("watch_detected_more", count=remaining_count)
                candidate = f"{candidate}, {suffix}"
            if len(candidate) <= max_length:
                included.append(item)
                continue
            if not included:
                return cls._truncate_text(item, max_length)
            break

        remaining_count = total_count - len(included)
        if remaining_count <= 0:
            return ", ".join(included)

        suffix = tr("watch_detected_more", count=remaining_count)
        while included:
            candidate = f'{", ".join(included)}, {suffix}'
            if len(candidate) <= max_length:
                return candidate
            included.pop()

        return cls._truncate_text(suffix, max_length)

    @classmethod
    def _split_message_lines(cls, header: str, lines: list[str], max_length: int = 2000) -> list[str]:
        current_chunk = cls._truncate_text(header, max_length)
        chunks: list[str] = []

        for line in lines:
            candidate = f"{current_chunk}\n{line}" if current_chunk else line
            if len(candidate) <= max_length:
                current_chunk = candidate
                continue

            if current_chunk:
                chunks.append(current_chunk)

            if len(line) <= max_length:
                current_chunk = line
            else:
                chunks.append(cls._truncate_text(line, max_length))
                current_chunk = ""

        if current_chunk:
            chunks.append(current_chunk)
        return chunks or [cls._truncate_text(header, max_length)]

    @staticmethod
    async def _send_ephemeral_chunks(
        interaction: discord.Interaction,
        chunks: list[str],
    ) -> None:
        if not chunks:
            chunks = [""]

        first_chunk, *remaining_chunks = chunks
        if interaction.response.is_done():
            await interaction.followup.send(first_chunk, ephemeral=True)
        else:
            await interaction.response.send_message(first_chunk, ephemeral=True)

        for chunk in remaining_chunks:
            await interaction.followup.send(chunk, ephemeral=True)

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

    @bga.command(name="help", description=tr("command_help_description"))
    async def help_command(self, interaction: discord.Interaction) -> None:
        """Post the full help as a single dismissible (ephemeral) embed.

        An embed rather than plain content: the help does not fit in a 2000-character
        message and would be split in two, whereas an embed description holds 4096.
        Sections are still handed to `_split_message_lines` whole, so if the text ever
        outgrows even that, it breaks between sections instead of mid-sentence.
        """
        sections = [
            tr("help_section_intro"),
            tr("help_section_watch"),
            tr("help_section_follow"),
            tr("help_section_link"),
            tr("help_section_other"),
            tr("help_section_permissions"),
            tr("help_footer"),
        ]
        blocks = [sections[0]] + [f"{self._HELP_SEPARATOR}\n{section}" for section in sections[1:]]
        chunks = self._split_message_lines(
            tr("help_header"),
            blocks,
            max_length=self._EMBED_DESCRIPTION_LIMIT,
        )

        first_chunk, *remaining_chunks = chunks
        await interaction.response.send_message(
            embed=discord.Embed(description=first_chunk, color=discord.Color.blurple()),
            ephemeral=True,
        )
        for chunk in remaining_chunks:
            await interaction.followup.send(
                embed=discord.Embed(description=chunk, color=discord.Color.blurple()),
                ephemeral=True,
            )

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
            guild_id=guild_id,
            discord_user_id=str(member.id),
            bga_player_id=candidate_id,
            bga_player_name=candidate_name,
        )
        linked_user = self.database.get_linked_user(guild_id, str(member.id))
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

        removed = self.database.remove_linked_user(str(interaction.guild_id), str(member.id))
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

    @bga.command(name="linked", description=tr("command_linked_description"))
    async def linked(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        linked_users = self.database.list_linked_users_for_guild(str(interaction.guild_id))
        if not linked_users:
            await interaction.response.send_message(
                tr("linked_none"),
                ephemeral=True,
            )
            return

        lines = [
            tr(
                "linked_line",
                discord_user_id=item.discord_user_id,
                bga_player_name=item.bga_player_name or tr("value_unknown"),
                bga_player_id=item.bga_player_id or tr("value_unknown"),
            )
            for item in linked_users
        ]
        await self._send_ephemeral_chunks(
            interaction,
            self._split_message_lines(tr("linked_header"), lines),
        )

    @bga.command(name="watch", description=tr("command_watch_description"))
    @app_commands.describe(table_or_url=tr("command_watch_target"))
    async def watch(self, interaction: discord.Interaction, table_or_url: str) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                tr("error_command_server_channel_only"),
                ephemeral=True,
            )
            return

        try:
            table_id, table_url, base_url, gameserver, game_name = parse_public_table_url(table_or_url)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result = await self._register_watch(
                guild_id=str(interaction.guild_id),
                channel_id=str(interaction.channel_id),
                created_by_discord_user_id=str(interaction.user.id),
                table_or_url=table_or_url,
            )
        except BgaNotPublicError as exc:
            await interaction.followup.send(
                tr("error_watch_not_public", table_id=table_id, error=exc),
                ephemeral=True,
            )
            return
        except BgaClientError as exc:
            await interaction.followup.send(
                tr("error_watch_verify_failed", table_id=table_id, error=exc),
                ephemeral=True,
            )
            return
        message_kwargs = {
            "game_label": tr("label_game"),
            "game_name": format_game_name(result.subscription.game_name),
            "table_label": tr("label_table"),
            "table_id": result.subscription.table_id,
            "channel_label": tr("label_channel"),
            "channel_id": interaction.channel_id,
            "public_source_label": tr("label_public_source_initial"),
            "source": result.source,
            "players_detected_label": tr("label_players_detected_currently"),
            "url_label": tr("label_url"),
            "table_url": result.subscription.table_url or build_table_url(result.subscription.table_id),
            "init_state_label": tr("label_init_state"),
            "init_state": result.init_state,
        }
        message_overhead = len(tr("watch_registered", players="", **message_kwargs))
        max_players_length = max(0, 2000 - message_overhead)
        detected_players = await self._format_detected_players(
            guild_id=str(interaction.guild_id),
            player_names=result.detected_player_names,
        )
        detected_players_text = self._format_bounded_list(
            detected_players,
            tr("watch_detected_none"),
            max_players_length,
        )
        message_content = tr(
            "watch_registered",
            players=detected_players_text,
            **message_kwargs,
        )

        await interaction.followup.send(message_content, ephemeral=True)
        await self.monitor.refresh_now()

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
            self.database.enrich_linked_users_from_players, guild_id, persisted_player_names
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

    async def _format_detected_players(
        self,
        *,
        guild_id: str,
        player_names: dict[str, str],
    ) -> list[str]:
        linked_users = await asyncio.to_thread(
            self.database.get_linked_users_for_players, guild_id, player_names
        )
        linked_by_bga_id = {item.bga_player_id: item for item in linked_users if item.bga_player_id}
        linked_by_name = {
            item.bga_player_name.casefold(): item
            for item in linked_users
            if item.bga_player_name
        }
        detected_players = []
        for player_id, player_name in sorted(player_names.items()):
            linked_user = linked_by_bga_id.get(player_id)
            if linked_user is None and player_name:
                linked_user = linked_by_name.get(player_name.casefold())
            if linked_user is not None:
                detected_players.append(
                    f"<@{linked_user.discord_user_id}> {player_name} ({player_id})"
                )
            else:
                detected_players.append(f"{player_name} ({player_id})")
        return detected_players

    @classmethod
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

    @bga.command(name="follow-tables", description=tr("command_follow_tables_description"))
    @app_commands.describe(member=tr("command_follow_tables_member"))
    async def follow_tables(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                tr("error_command_server_channel_only"),
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        channel_id = str(interaction.channel_id)
        followed_discord_user_id = str(member.id)

        # Turning the follow off is checked before the BGA link so that a member
        # unlinked in the meantime can still be un-followed.
        if self.database.is_player_followed(
            guild_id=guild_id,
            discord_user_id=followed_discord_user_id,
            channel_id=channel_id,
        ):
            self.database.toggle_followed_player(
                guild_id=guild_id,
                discord_user_id=followed_discord_user_id,
                channel_id=channel_id,
                created_by_discord_user_id=str(interaction.user.id),
            )
            await interaction.response.send_message(
                tr(
                    "follow_tables_disabled",
                    member_mention=member.mention,
                    channel_id=interaction.channel_id,
                ),
                ephemeral=True,
            )
            return

        linked_user = self.database.get_linked_user(guild_id, followed_discord_user_id)
        if linked_user is None:
            await interaction.response.send_message(
                tr("error_follow_member_not_linked", member_mention=member.mention),
                ephemeral=True,
            )
            return

        bga_player_id = (linked_user.bga_player_id or "").strip()
        if not bga_player_id:
            await interaction.response.send_message(
                tr(
                    "error_follow_member_without_id",
                    member_mention=member.mention,
                    bga_name=linked_user.bga_player_name or tr("value_unknown"),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Scan before enabling: a lookup failure must not leave behind a follow that
        # silently does nothing.
        try:
            result = await self.monitor.sync_followed_player(
                guild_id=guild_id,
                discord_user_id=followed_discord_user_id,
                channel_id=channel_id,
                bga_player_id=bga_player_id,
                created_by_discord_user_id=str(interaction.user.id),
            )
        except BgaPlayerNotFoundError:
            await interaction.followup.send(
                tr(
                    "error_follow_unknown_player",
                    member_mention=member.mention,
                    bga_player_id=bga_player_id,
                ),
                ephemeral=True,
            )
            return
        except BgaClientError as exc:
            await interaction.followup.send(
                tr("error_follow_lookup_failed", member_mention=member.mention, error=exc),
                ephemeral=True,
            )
            return

        self.database.toggle_followed_player(
            guild_id=guild_id,
            discord_user_id=followed_discord_user_id,
            channel_id=channel_id,
            created_by_discord_user_id=str(interaction.user.id),
        )

        header = tr(
            "follow_tables_enabled",
            member_mention=member.mention,
            bga_name=result.player_name or linked_user.bga_player_name or tr("value_unknown"),
            bga_id=bga_player_id,
            channel_id=interaction.channel_id,
        )
        lines: list[str] = []
        if result.added:
            lines.append(tr("follow_tables_added_header", count=len(result.added)))
            lines.extend(
                tr(
                    "follow_tables_added_line",
                    table_id=table.table_id,
                    game_name=format_game_name(table.game_name),
                )
                for table in result.added
            )
        else:
            lines.append(tr("follow_tables_added_none"))
        if result.already_watched:
            lines.append(
                tr(
                    "follow_tables_already_watched",
                    count=len(result.already_watched),
                    table_ids=", ".join(f"`{table.table_id}`" for table in result.already_watched),
                )
            )
        lines.append(tr("follow_tables_toggle_hint"))

        await self._send_ephemeral_chunks(
            interaction,
            self._split_message_lines(header, lines),
        )
        await self.monitor.refresh_now()

    @bga.command(name="unwatch", description=tr("command_unwatch_description"))
    @app_commands.describe(table_or_url=tr("command_unwatch_target"))
    async def unwatch(self, interaction: discord.Interaction, table_or_url: str) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                tr("error_command_server_channel_only"),
                ephemeral=True,
            )
            return

        try:
            table_id = parse_table_id(table_or_url)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        removed = self.database.remove_watch_subscription(
            table_id=table_id,
            guild_id=str(interaction.guild_id),
            channel_id=str(interaction.channel_id),
        )
        if not removed:
            await interaction.response.send_message(
                tr("unwatch_not_found", table_id=table_id),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr("unwatch_removed", table_id=table_id),
            ephemeral=True,
        )
        await self.monitor.refresh_now()

    @bga.command(name="unwatch-all", description=tr("command_unwatch_all_description"))
    async def unwatch_all(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        if not self._has_manage_permissions(interaction):
            await interaction.response.send_message(
                tr("error_manage_server_required_unwatch_all"),
                ephemeral=True,
            )
            return

        removed_count = self.database.remove_all_watch_subscriptions_for_guild(str(interaction.guild_id))
        if removed_count == 0:
            await interaction.response.send_message(
                tr("unwatch_all_none"),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            tr("unwatch_all_removed", removed_count=removed_count),
            ephemeral=True,
        )
        await self.monitor.refresh_now()

    @bga.command(name="watchlist", description=tr("command_watchlist_description"))
    async def watchlist(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("error_command_server_only"),
                ephemeral=True,
            )
            return

        subscriptions = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        if not subscriptions:
            await interaction.response.send_message(
                tr("watchlist_none"),
                ephemeral=True,
            )
            return

        embeds = [
            discord.Embed(
                title=f"📚 {tr('watchlist_header')}",
                description=tr("watchlist_embed_summary", count=len(subscriptions)),
                color=discord.Color.blurple(),
            )
        ]
        for index, subscription in enumerate(subscriptions, start=1):
            public_url = subscription.table_url or build_table_url(subscription.table_id)
            state = (
                tr("watch_state_initialized")
                if subscription.is_initialized
                else tr("watch_state_waiting_players")
                if not (subscription.gameserver or "").strip()
                else tr("watch_state_waiting_first_event")
            )
            card = discord.Embed(
                title=tr(
                    "watchlist_card_title",
                    table_id=subscription.table_id,
                    game_name=format_game_name(subscription.game_name),
                ),
                color=discord.Color.blurple(),
            )
            card.add_field(
                name=f"📍 {tr('label_channel')}",
                value=f"<#{subscription.channel_id}>",
                inline=True,
            )
            card.add_field(
                name=f"⚙️ {tr('label_state')}",
                value=state,
                inline=True,
            )
            card.add_field(
                name=f"🔗 {tr('label_url')}",
                value=public_url,
                inline=False,
            )
            card.set_footer(text=tr("watchlist_card_footer", index=index, total=len(subscriptions)))
            embeds.append(card)

        await self._send_ephemeral_embeds(interaction, embeds)

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
                    subscription.guild_id, subscription.last_waiting_ids
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
