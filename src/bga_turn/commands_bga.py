from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

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
    replaced_existing_watch: bool = False


class StatsPageButton(discord.ui.Button["StatsLayoutView"]):
    def __init__(self, page_key: str, label: str, *, active: bool) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            disabled=active,
        )
        self.page_key = page_key

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is None:
            await interaction.response.send_message(tr("stats_view_expired"), ephemeral=True)
            return
        await self.view.show_page(interaction, self.page_key)


class StatsLayoutView(discord.ui.LayoutView):
    _PAGE_LABELS = {
        "overview": "stats_page_overview",
        "leaders": "stats_page_leaders",
        "activity": "stats_page_activity",
    }
    _PAGE_COLORS = {
        "overview": discord.Color.blurple(),
        "leaders": discord.Color.gold(),
        "activity": discord.Color.dark_green(),
    }

    def __init__(
        self,
        *,
        cog: "BgaCommands",
        guild: discord.Guild | None,
        stats: dict[str, object],
        is_global: bool,
        page: str = "overview",
    ) -> None:
        super().__init__(timeout=300)
        self._cog = cog
        self._guild = guild
        self._stats = stats
        self._is_global = is_global
        self._page = page if page in self._PAGE_LABELS else "overview"
        self._build()

    async def show_page(self, interaction: discord.Interaction, page_key: str) -> None:
        await interaction.response.edit_message(
            view=StatsLayoutView(
                cog=self._cog,
                guild=self._guild,
                stats=self._stats,
                is_global=self._is_global,
                page=page_key,
            )
        )

    def _build(self) -> None:
        title = (
            tr("stats_title_global")
            if self._is_global
            else tr(
                "stats_title_guild",
                guild_name=getattr(self._guild, "name", tr("stats_this_server")),
            )
        )
        items: list[discord.ui.Item] = [
            discord.ui.TextDisplay(
                f"## {title}\n_{self._cog._build_stats_scope_summary(self._stats, is_global=self._is_global)}_"
            ),
            discord.ui.Separator(),
        ]

        if self._page == "overview":
            items.extend(self._build_overview_items())
        elif self._page == "leaders":
            items.extend(self._build_leaders_items())
        else:
            items.extend(self._build_activity_items())

        items.append(discord.ui.Separator())
        items.append(
            discord.ui.ActionRow(
                *[
                    StatsPageButton(
                        page_key=page_key,
                        label=tr(label_key),
                        active=page_key == self._page,
                    )
                    for page_key, label_key in self._PAGE_LABELS.items()
                ]
            )
        )
        self.add_item(discord.ui.Container(*items, accent_color=self._PAGE_COLORS[self._page]))

    def _build_overview_items(self) -> list[discord.ui.Item]:
        return [
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_quick_numbers"),
                    [
                        f"🎲 {tr('stats_metric_games_tracked')}: **{self._cog._stat_int(self._stats, 'total_games')}**",
                        f"🟢 {tr('stats_metric_live_tables')}: **{self._cog._stat_int(self._stats, 'currently_watching')}**",
                        f"📣 {tr('stats_metric_recruiting_now')}: **{self._cog._stat_int(self._stats, 'currently_recruiting')}**",
                        f"🔗 {tr('stats_metric_linked_accounts')}: **{self._cog._stat_int(self._stats, 'members_linked')}**",
                        f"👀 {tr('stats_metric_followed_members')}: **{self._cog._stat_int(self._stats, 'followed_members')}**",
                        f"💬 {tr('stats_metric_channels')}: **{self._cog._stat_int(self._stats, 'tracked_channels')}**",
                        f"⏱ {tr('stats_metric_avg_recruiting')}: **{self._cog._format_minutes(self._stats.get('avg_recruiting_minutes'))}**",
                        f"⌛ {tr('stats_metric_avg_game')}: **{self._cog._format_hours(self._stats.get('avg_game_hours'))}**",
                        f"👥 {tr('stats_metric_avg_players')}: **{self._cog._format_decimal(self._stats.get('avg_players_per_game'))}**",
                    ],
                )
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_highlights"),
                    self._cog._build_highlight_lines(
                        self._stats, guild=self._guild, is_global=self._is_global
                    ),
                )
            ),
        ]

    def _build_leaders_items(self) -> list[discord.ui.Item]:
        return [
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_top_games"),
                    self._cog._format_game_rankings(self._stats),
                )
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_top_players"),
                    self._cog._format_player_rankings(self._stats),
                )
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_top_recruiters"),
                    self._cog._format_recruiter_rankings(
                        self._stats, guild=self._guild, is_global=self._is_global
                    ),
                )
            ),
        ]

    def _build_activity_items(self) -> list[discord.ui.Item]:
        section_lines = [
            f"✅ {tr('stats_metric_finished_games')}: **{self._cog._stat_int(self._stats, 'finished_games')}**",
            f"🛑 {tr('stats_metric_cancelled_games')}: **{self._cog._stat_int(self._stats, 'cancelled_games')}**",
            f"🧹 {tr('stats_metric_unwatched_games')}: **{self._cog._stat_int(self._stats, 'unwatched_games')}**",
            f"🧑‍🤝‍🧑 {tr('stats_metric_unique_players')}: **{self._cog._stat_int(self._stats, 'unique_players')}**",
            f"📡 {tr('stats_metric_live_channels')}: **{self._cog._stat_int(self._stats, 'live_channels')}**",
            f"📣 {tr('stats_metric_recruiters')}: **{self._cog._stat_int(self._stats, 'recruiter_count')}**",
        ]
        if self._is_global:
            section_lines.extend(
                [
                    f"🛖 {tr('stats_metric_guilds')}: **{self._cog._stat_int(self._stats, 'guild_count')}**",
                    f"📍 {tr('stats_metric_forced_channels')}: **{self._cog._stat_int(self._stats, 'forced_channels')}**",
                ]
            )
        return [
            discord.ui.TextDisplay(self._cog._join_stat_lines(tr("stats_section_activity"), section_lines)),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_recent_pace"),
                    self._cog._build_recent_pace_lines(self._stats),
                )
            ),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                self._cog._join_stat_lines(
                    tr("stats_section_top_channels"),
                    self._cog._format_channel_rankings(
                        self._stats, guild=self._guild, is_global=self._is_global
                    ),
                )
            ),
        ]


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
        subscriptions_to_repost: dict[int, WatchSubscription] = {}
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
            if registration.replaced_existing_watch:
                subscriptions_to_repost[registration.subscription.subscription_id] = registration.subscription

        if registered_subscriptions:
            for subscription in subscriptions_to_repost.values():
                await self.monitor.reset_tracked_message(subscription)
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
                deleted = await self.monitor.delete_discord_message(
                    message, operation="delete_trigger_message"
                )
                if deleted:
                    LOGGER.info(
                        tr(
                            "trigger_message_deleted",
                            channel_id=message.channel.id,
                            guild_id=message.guild.id,
                        )
                    )
                else:
                    LOGGER.warning(
                        tr(
                            "trigger_message_delete_failed",
                            channel_id=message.channel.id,
                            error="rate-limit or Discord error",
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

    async def _interaction_send_with_retry(
        self,
        interaction: discord.Interaction,
        operation: str,
        coro_factory,
    ) -> None:
        await self.monitor._discord_call_with_retry(
            operation=operation,
            table_id=f"interaction:{interaction.guild_id or 'dm'}",
            coro_factory=coro_factory,
            channel_id=str(interaction.channel_id) if interaction.channel_id is not None else None,
            operation_kind="send",
        )

    async def _send_ephemeral_embeds(
        self,
        interaction: discord.Interaction,
        embeds: list[discord.Embed],
    ) -> None:
        if not embeds:
            embeds = [discord.Embed(description="")]

        batches = [embeds[index : index + 10] for index in range(0, len(embeds), 10)]
        first_batch, *remaining_batches = batches
        if interaction.response.is_done():
            await self._interaction_send_with_retry(
                interaction,
                "interaction_followup_embeds",
                lambda: interaction.followup.send(embeds=first_batch, ephemeral=True),
            )
        else:
            await self._interaction_send_with_retry(
                interaction,
                "interaction_response_embeds",
                lambda: interaction.response.send_message(embeds=first_batch, ephemeral=True),
            )

        for batch in remaining_batches:
            await self._interaction_send_with_retry(
                interaction,
                "interaction_followup_embeds",
                lambda current_batch=batch: interaction.followup.send(
                    embeds=current_batch, ephemeral=True
                ),
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
            await self._interaction_send_with_retry(
                interaction,
                "link_member_server_only",
                lambda: interaction.response.send_message(
                    tr("error_command_server_only"),
                    ephemeral=True,
                ),
            )
            return
        if not self._has_manage_permissions(interaction):
            await self._interaction_send_with_retry(
                interaction,
                "link_member_no_permission",
                lambda: interaction.response.send_message(
                    tr("error_manage_server_required_link"),
                    ephemeral=True,
                ),
            )
            return

        candidate_id = (bga_player_id or "").strip()
        candidate_name = (bga_player_name or "").strip()
        if not candidate_id and not candidate_name:
            await self._interaction_send_with_retry(
                interaction,
                "link_member_missing_input",
                lambda: interaction.response.send_message(
                    tr("error_need_bga_name_or_id"),
                    ephemeral=True,
                ),
            )
            return
        if candidate_id and not candidate_id.isdigit():
            await self._interaction_send_with_retry(
                interaction,
                "link_member_invalid_id",
                lambda: interaction.response.send_message(
                    tr("error_invalid_bga_player_id"),
                    ephemeral=True,
                ),
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
        await self._interaction_send_with_retry(
            interaction,
            "link_member_saved",
            lambda: interaction.response.send_message(
                tr(
                    "link_saved",
                    member_mention=member.mention,
                    bga_name=name_display,
                    bga_id=id_display,
                ),
                ephemeral=True,
            ),
        )

    @bga.command(name="unlink-member", description=tr("command_unlink_member_description"))
    @app_commands.describe(member=tr("command_unlink_member_member"))
    async def unlink_member(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if interaction.guild_id is None:
            await self._interaction_send_with_retry(
                interaction,
                "unlink_member_server_only",
                lambda: interaction.response.send_message(
                    tr("error_command_server_only"),
                    ephemeral=True,
                ),
            )
            return
        if not self._has_manage_permissions(interaction):
            await self._interaction_send_with_retry(
                interaction,
                "unlink_member_no_permission",
                lambda: interaction.response.send_message(
                    tr("error_manage_server_required_unlink"),
                    ephemeral=True,
                ),
            )
            return

        removed = self.database.remove_linked_user(str(member.id))
        if not removed:
            await self._interaction_send_with_retry(
                interaction,
                "unlink_member_not_found",
                lambda: interaction.response.send_message(
                    tr("unlink_not_found", member_mention=member.mention),
                    ephemeral=True,
                ),
            )
            return

        await self._interaction_send_with_retry(
            interaction,
            "unlink_member_saved",
            lambda: interaction.response.send_message(
                tr("unlink_saved", member_mention=member.mention),
                ephemeral=True,
            ),
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
        existing_subscription = self.database.get_watch_subscription_by_scope(
            table_id=table_id,
            guild_id=guild_id,
            channel_id=channel_id,
        )

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
            seated_player_names=(
                dict(snapshot.player_names)
                if snapshot is not None and snapshot.player_names
                else dict(persisted_player_names)
            ),
            seats_total=snapshot.seats_total if snapshot is not None else subscription.seats_total,
            seats_remaining=snapshot.seats_remaining if snapshot is not None else subscription.seats_remaining,
            is_initialized=subscription.is_initialized if state is not None else False,
            game_name=resolved_game_name or subscription.game_name,
            player_count=len(persisted_player_names),
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
                replaced_existing_watch=existing_subscription is not None,
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
            replaced_existing_watch=existing_subscription is not None,
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
            await self._interaction_send_with_retry(
                interaction,
                "status_server_only",
                lambda: interaction.response.send_message(
                    tr("error_command_server_only"),
                    ephemeral=True,
                ),
            )
            return

        subscriptions = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        if not subscriptions:
            await self._interaction_send_with_retry(
                interaction,
                "status_none",
                lambda: interaction.response.send_message(
                    tr("status_none"),
                    ephemeral=True,
                ),
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
            await self._interaction_send_with_retry(
                interaction,
                "settings_server_only",
                lambda: interaction.response.send_message(
                    tr("error_command_server_only"),
                    ephemeral=True,
                ),
            )
            return
        if not self._has_manage_permissions(interaction):
            await self._interaction_send_with_retry(
                interaction,
                "settings_no_permission",
                lambda: interaction.response.send_message(
                    tr("error_manage_server_required_settings"),
                    ephemeral=True,
                ),
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
            await self._interaction_send_with_retry(
                interaction,
                "settings_display",
                lambda: interaction.response.send_message(
                    tr(
                        "settings_display",
                        recruiting_only=current.recruiting_only,
                        delete_invite_message=current.delete_invite_message,
                        forced_channel_id=channel_display,
                    ),
                    ephemeral=True,
                ),
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
        await self._interaction_send_with_retry(
            interaction,
            "settings_saved",
            lambda: interaction.response.send_message(
                tr(
                    "settings_saved",
                    recruiting_only=new_recruiting_only,
                    delete_invite_message=new_delete_invite_message,
                    forced_channel_id=channel_display,
                ),
                ephemeral=True,
            ),
        )

    @staticmethod
    def _format_recruiting_age(recruiting_started_at: str | None) -> str:
        if not recruiting_started_at:
            return tr("recruiting_age_unknown")
        try:
            started = datetime.fromisoformat(recruiting_started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            delta_seconds = int((datetime.now(timezone.utc) - started).total_seconds())
            if delta_seconds < 0:
                delta_seconds = 0
            total_minutes = delta_seconds // 60
            hours, minutes = divmod(total_minutes, 60)
            days, hours = divmod(hours, 24)
            if days > 0:
                return tr("recruiting_age_days", days=days, hours=hours)
            if hours > 0:
                return tr("recruiting_age_hours", hours=hours, minutes=minutes)
            return tr("recruiting_age_minutes", minutes=total_minutes)
        except (ValueError, OverflowError):
            return tr("recruiting_age_unknown")

    @staticmethod
    def _stat_int(stats: dict[str, object], key: str) -> int:
        value = stats.get(key)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _format_decimal(value: object) -> str:
        if value is None:
            return "—"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "—"
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:.1f}"

    @classmethod
    def _format_minutes(cls, value: object) -> str:
        if value is None:
            return "—"
        try:
            minutes = max(0, int(round(float(value))))
        except (TypeError, ValueError):
            return "—"
        if minutes < 60:
            return tr("recruiting_age_minutes", minutes=minutes)
        hours, remaining_minutes = divmod(minutes, 60)
        if hours < 24:
            return tr("recruiting_age_hours", hours=hours, minutes=remaining_minutes)
        days, remaining_hours = divmod(hours, 24)
        return tr("recruiting_age_days", days=days, hours=remaining_hours)

    @classmethod
    def _format_hours(cls, value: object) -> str:
        if value is None:
            return "—"
        try:
            total_minutes = max(0, int(round(float(value) * 60)))
        except (TypeError, ValueError):
            return "—"
        return cls._format_minutes(total_minutes)

    @staticmethod
    def _join_stat_lines(title: str, lines: list[str]) -> str:
        effective_lines = [line for line in lines if line]
        if not effective_lines:
            effective_lines = [tr("stats_none_available")]
        return f"**{title}**\n" + "\n".join(effective_lines)

    def _build_stats_scope_summary(self, stats: dict[str, object], *, is_global: bool) -> str:
        return tr(
            "stats_scope_summary_global" if is_global else "stats_scope_summary_guild",
            tables=self._stat_int(stats, "currently_watching"),
            channels=self._stat_int(stats, "live_channels"),
            recruiting=self._stat_int(stats, "currently_recruiting"),
        )

    def _build_highlight_lines(
        self,
        stats: dict[str, object],
        *,
        guild: discord.Guild | None,
        is_global: bool,
    ) -> list[str]:
        games = stats.get("games_by_name") or []
        top_players = stats.get("top_players") or []
        recruiters = stats.get("top_recruiters") or []
        busiest_day = stats.get("busiest_day")
        lines = [
            f"🏅 {tr('stats_highlight_most_played')}: {self._format_top_game(games)}",
            f"🔥 {tr('stats_highlight_most_active_player')}: {self._format_top_player(top_players)}",
            f"📣 {tr('stats_highlight_top_recruiter')}: {self._format_top_recruiter(recruiters, guild=guild, is_global=is_global)}",
        ]
        if isinstance(busiest_day, dict) and busiest_day.get("day"):
            lines.append(
                tr(
                    "stats_highlight_busiest_day",
                    day=str(busiest_day["day"]),
                    count=int(busiest_day["count"]),
                )
            )
        else:
            lines.append(f"📅 {tr('stats_metric_busiest_day')}: {tr('stats_none_available')}")
        return lines

    @staticmethod
    def _format_top_game(games: object) -> str:
        if not isinstance(games, list) or not games:
            return tr("stats_none_available")
        top_game = games[0]
        return tr(
            "stats_game_count_line",
            name=format_game_name(str(top_game.get("name"))),
            count=int(top_game.get("count", 0)),
        )

    @staticmethod
    def _format_top_player(players: object) -> str:
        if not isinstance(players, list) or not players:
            return tr("stats_none_available")
        top_player = players[0]
        return tr(
            "stats_player_record_line",
            name=str(top_player.get("name", tr("value_unknown"))),
            appearances=int(top_player.get("appearances", 0)),
            wins=int(top_player.get("wins", 0)),
        )

    def _format_top_recruiter(
        self,
        recruiters: object,
        *,
        guild: discord.Guild | None,
        is_global: bool,
    ) -> str:
        if not isinstance(recruiters, list) or not recruiters:
            return tr("stats_none_available")
        top_recruiter = recruiters[0]
        return tr(
            "stats_recruiter_record_line",
            recruiter=self._format_user_ref(
                str(top_recruiter.get("discord_user_id", "")),
                guild=guild,
                is_global=is_global,
            ),
            count=int(top_recruiter.get("count", 0)),
        )

    def _format_game_rankings(self, stats: dict[str, object]) -> list[str]:
        rows = stats.get("games_by_name")
        if not isinstance(rows, list) or not rows:
            return []
        return [
            tr(
                "stats_game_ranking_line",
                rank=index,
                name=format_game_name(str(row.get("name"))),
                count=int(row.get("count", 0)),
            )
            for index, row in enumerate(rows[:5], start=1)
        ]

    def _format_player_rankings(self, stats: dict[str, object]) -> list[str]:
        rows = stats.get("top_players")
        if not isinstance(rows, list) or not rows:
            return []
        return [
            tr(
                "stats_player_ranking_line",
                rank=index,
                name=str(row.get("name", tr("value_unknown"))),
                appearances=int(row.get("appearances", 0)),
                wins=int(row.get("wins", 0)),
            )
            for index, row in enumerate(rows[:5], start=1)
        ]

    def _format_recruiter_rankings(
        self,
        stats: dict[str, object],
        *,
        guild: discord.Guild | None,
        is_global: bool,
    ) -> list[str]:
        rows = stats.get("top_recruiters")
        if not isinstance(rows, list) or not rows:
            return []
        return [
            tr(
                "stats_recruiter_ranking_line",
                rank=index,
                recruiter=self._format_user_ref(
                    str(row.get("discord_user_id", "")),
                    guild=guild,
                    is_global=is_global,
                ),
                count=int(row.get("count", 0)),
            )
            for index, row in enumerate(rows[:5], start=1)
        ]

    def _format_channel_rankings(
        self,
        stats: dict[str, object],
        *,
        guild: discord.Guild | None,
        is_global: bool,
    ) -> list[str]:
        rows = stats.get("top_channels")
        if not isinstance(rows, list) or not rows:
            return []
        return [
            tr(
                "stats_channel_ranking_line",
                rank=index,
                channel=self._format_channel_ref(
                    str(row.get("channel_id", "")),
                    guild=guild,
                    is_global=is_global,
                ),
                count=int(row.get("count", 0)),
            )
            for index, row in enumerate(rows[:5], start=1)
        ]

    def _build_recent_pace_lines(self, stats: dict[str, object]) -> list[str]:
        rows = stats.get("games_over_time")
        if not isinstance(rows, list) or not rows:
            return []
        total_recent = sum(int(row.get("count", 0)) for row in rows)
        active_days = len(rows)
        busiest_day = stats.get("busiest_day")
        lines = [
            tr("stats_recent_total_line", count=total_recent),
            tr("stats_recent_active_days_line", count=active_days),
        ]
        if isinstance(busiest_day, dict) and busiest_day.get("day"):
            lines.append(
                tr(
                    "stats_recent_busiest_line",
                    day=str(busiest_day["day"]),
                    count=int(busiest_day["count"]),
                )
            )
        return lines

    def _format_user_ref(
        self,
        discord_user_id: str,
        *,
        guild: discord.Guild | None,
        is_global: bool,
    ) -> str:
        if not discord_user_id:
            return tr("value_unknown")
        if guild is not None and discord_user_id.isdigit():
            member = guild.get_member(int(discord_user_id))
            if member is not None:
                return member.mention if not is_global else member.display_name
        if is_global and discord_user_id.isdigit():
            return tr("stats_masked_user", suffix=discord_user_id[-4:])
        return f"<@{discord_user_id}>"

    def _format_channel_ref(
        self,
        channel_id: str,
        *,
        guild: discord.Guild | None,
        is_global: bool,
    ) -> str:
        if not channel_id:
            return tr("value_unknown")
        if guild is not None and channel_id.isdigit():
            channel = guild.get_channel(int(channel_id))
            if channel is not None:
                return channel.mention if not is_global else f"#{channel.name}"
        if is_global and channel_id.isdigit():
            return tr("stats_masked_channel", suffix=channel_id[-4:])
        return f"<#{channel_id}>"

    @bga.command(name="stats", description=tr("command_stats_description"))
    @app_commands.rename(global_scope="global")
    @app_commands.describe(global_scope=tr("command_stats_global"))
    async def stats_command(
        self,
        interaction: discord.Interaction,
        global_scope: bool = False,
    ) -> None:
        if interaction.guild_id is None:
            await self._interaction_send_with_retry(
                interaction,
                "stats_server_only",
                lambda: interaction.response.send_message(
                    tr("error_command_server_only"),
                    ephemeral=True,
                ),
            )
            return

        stats = await asyncio.to_thread(
            self.database.get_global_extended_stats
            if global_scope
            else self.database.get_guild_extended_stats,
            *(() if global_scope else (str(interaction.guild_id),)),
        )
        await self._interaction_send_with_retry(
            interaction,
            "stats_display",
            lambda: interaction.response.send_message(
                view=StatsLayoutView(
                    cog=self,
                    guild=interaction.guild,
                    stats=stats,
                    is_global=global_scope,
                ),
                ephemeral=True,
            ),
        )

    @bga.command(name="recruiting", description=tr("command_recruiting_description"))
    async def recruiting(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await self._interaction_send_with_retry(
                interaction,
                "recruiting_server_only",
                lambda: interaction.response.send_message(
                    tr("error_command_server_only"),
                    ephemeral=True,
                ),
            )
            return

        all_subscriptions = self.database.list_watch_subscriptions_for_guild(str(interaction.guild_id))
        subscriptions = [s for s in all_subscriptions if s.lifecycle_state == "recruiting"]

        if not subscriptions:
            await self._interaction_send_with_retry(
                interaction,
                "recruiting_none",
                lambda: interaction.response.send_message(
                    tr("recruiting_none"),
                    ephemeral=True,
                ),
            )
            return

        embeds = [
            discord.Embed(
                title=tr("recruiting_header"),
                description=tr("recruiting_summary", count=len(subscriptions)),
                color=discord.Color.gold(),
            )
        ]

        for subscription in subscriptions:
            table_url = subscription.table_url or build_table_url(subscription.table_id)
            card = discord.Embed(
                title=tr(
                    "recruiting_card_title",
                    table_id=subscription.table_id,
                    game_name=format_game_name(subscription.game_name),
                ),
                url=table_url,
                color=discord.Color.gold(),
            )

            # Creator
            card.add_field(
                name=tr("recruiting_label_creator"),
                value=f"<@{subscription.created_by_discord_user_id}>",
                inline=True,
            )

            # Seats free
            if subscription.seats_remaining is not None:
                seats_value = str(subscription.seats_remaining)
                if subscription.seats_total is not None:
                    seats_value += f"/{subscription.seats_total}"
            else:
                seats_value = tr("recruiting_seats_unknown")
            card.add_field(
                name=tr("recruiting_label_seats_free"),
                value=seats_value,
                inline=True,
            )

            # Age
            card.add_field(
                name=tr("recruiting_label_age"),
                value=self._format_recruiting_age(subscription.recruiting_started_at),
                inline=True,
            )

            # Players
            player_names = list(subscription.seated_player_names.values()) or list(subscription.player_names.values())
            players_value = ", ".join(player_names) if player_names else tr("recruiting_players_none")
            card.add_field(
                name=tr("recruiting_label_players"),
                value=players_value,
                inline=False,
            )

            embeds.append(card)

        await self._send_ephemeral_embeds(interaction, embeds)
