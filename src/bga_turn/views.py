"""Persistent Discord UI: the "Link your BGA & Discord" button and its modal."""

from __future__ import annotations

import asyncio

import discord

from .database import Database
from .i18n import tr


class LinkBgaModal(discord.ui.Modal):
    """Modal shown when a user clicks the self-service 'Link your BGA & Discord' button."""

    bga_input: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="BGA Username or Player ID",
        placeholder="e.g. Haurrus  or  91713763",
        min_length=1,
        max_length=100,
    )

    def __init__(self) -> None:
        super().__init__(title=tr("modal_link_bga_title"))
        self.bga_input.label = tr("modal_link_bga_input_label")
        self.bga_input.placeholder = tr("modal_link_bga_input_placeholder")

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("link_self_no_guild"), ephemeral=True
            )
            return

        raw = self.bga_input.value.strip()
        if not raw:
            await interaction.response.send_message(
                tr("link_self_invalid_id"), ephemeral=True
            )
            return

        # Determine whether the input is a numeric ID or a username.
        if raw.isdigit():
            bga_player_id: str | None = raw
            bga_player_name: str | None = None
        else:
            bga_player_id = None
            bga_player_name = raw

        database: Database = interaction.client.database  # type: ignore[attr-defined]
        discord_user_id = str(interaction.user.id)

        await asyncio.to_thread(
            database.upsert_linked_user,
            discord_user_id,
            bga_player_id,
            bga_player_name,
        )
        linked = await asyncio.to_thread(database.get_linked_user, discord_user_id)
        name_display = (linked.bga_player_name if linked else None) or tr("link_missing_value_placeholder")
        id_display = (linked.bga_player_id if linked else None) or tr("link_missing_value_placeholder")

        await interaction.response.send_message(
            tr("link_self_saved", bga_name=name_display, bga_id=id_display),
            ephemeral=True,
        )


class LinkSelfButton(discord.ui.Button["LinkSelfPersistentView"]):
    """Persistent interactive button that lets any server member self-link their BGA account."""

    _CUSTOM_ID = "bga:link_self"

    def __init__(self) -> None:
        super().__init__(
            label=tr("button_link_bga_discord"),
            style=discord.ButtonStyle.secondary,
            custom_id=self._CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                tr("link_self_no_guild"), ephemeral=True
            )
            return

        database: Database = interaction.client.database  # type: ignore[attr-defined]
        discord_user_id = str(interaction.user.id)

        existing = await asyncio.to_thread(database.get_linked_user, discord_user_id)
        if existing is not None and (existing.bga_player_id or existing.bga_player_name):
            name_display = existing.bga_player_name or tr("link_missing_value_placeholder")
            id_display = existing.bga_player_id or tr("link_missing_value_placeholder")
            await interaction.response.send_message(
                tr("link_self_already_linked", bga_name=name_display, bga_id=id_display),
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(LinkBgaModal())


class LinkSelfPersistentView(discord.ui.LayoutView):
    """Minimal persistent LayoutView registered at startup so that self-link buttons
    on existing messages continue to work after a bot restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        row = discord.ui.ActionRow(LinkSelfButton())
        container = discord.ui.Container(row)
        self.add_item(container)
