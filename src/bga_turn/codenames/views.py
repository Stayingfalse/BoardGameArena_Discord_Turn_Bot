"""The "I'm playing" button on Codenames cards and the seat picker it opens."""

from __future__ import annotations

import discord

from ..database import AsyncDatabase
from ..i18n import tr
from .models import CodenamesPlayer
from .store import CodenamesSeatTakenError

_MAX_OPTIONS = 25  # Discord's limit for a select menu


def _db(interaction: discord.Interaction) -> AsyncDatabase:
    return AsyncDatabase(interaction.client.database)  # type: ignore[attr-defined]


async def _refresh_room(interaction: discord.Interaction, room_id: str) -> None:
    monitor = getattr(interaction.client, "codenames_monitor", None)
    if monitor is not None:
        await monitor.refresh_room(room_id)


class ClaimSeatButton(discord.ui.Button):
    """Persistent: works on every card, including after a restart."""

    CUSTOM_ID = "codenames:claim_seat"

    def __init__(self) -> None:
        super().__init__(label=tr("cn_button_im_playing"), style=discord.ButtonStyle.primary, custom_id=self.CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction) -> None:
        db = _db(interaction)
        game = await db.get_codenames_game_by_message(interaction.message.id) if interaction.message else None
        watch = await db.get_codenames_watch(game.watch_id) if game is not None else None
        if watch is None:
            await interaction.response.send_message(tr("cn_claim_not_watched"), ephemeral=True)
            return
        room = await db.get_codenames_room(watch.room_id)
        bot_seat = room.bot_player_id if room is not None else None
        players = [p for p in await db.list_codenames_players(watch.room_id) if p.player_id != bot_seat and p.display_name]
        if not players:
            await interaction.response.send_message(tr("cn_claim_nobody_in_room"), ephemeral=True)
            return
        user_id = str(interaction.user.id)
        remembered = await db.get_codenames_remembered_name(user_id)
        await interaction.response.send_message(
            tr("cn_claim_prompt"),
            view=SeatPickerView(watch.room_id, players, user_id, remembered),
            ephemeral=True,
        )


class ClaimSeatPersistentView(discord.ui.LayoutView):
    """Registered at startup so card buttons keep working after a restart."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(discord.ui.Container(discord.ui.ActionRow(ClaimSeatButton())))


class SeatPickerView(discord.ui.View):
    def __init__(self, room_id: str, players: list[CodenamesPlayer], user_id: str, remembered: str | None) -> None:
        super().__init__(timeout=300)
        self.room_id = room_id
        mine = next((p for p in players if p.discord_user_id == user_id), None)
        suggested = mine or next(
            (p for p in players if remembered and (p.display_name or "").casefold() == remembered.casefold()), None
        )
        # No option is pre-selected: Discord ignores picking the option that is
        # already selected, which would make the suggested name unclickable.
        # The suggestion goes first, with a hint, instead.
        ordered = sorted(players, key=lambda p: suggested is None or p.player_id != suggested.player_id)
        options = []
        for player in ordered[:_MAX_OPTIONS]:
            if player is mine:
                description = tr("cn_claim_option_current")
            elif player.discord_user_id not in (None, user_id):
                description = tr("cn_claim_option_taken")
            elif player is suggested:
                description = tr("cn_claim_option_suggested")
            else:
                description = None
            options.append(
                discord.SelectOption(
                    label=(player.display_name or player.player_id)[:100],
                    value=player.player_id,
                    description=description,
                )
            )
        self.add_item(_SeatSelect(options))
        if mine is not None:
            self.add_item(_ReleaseButton())


class _SeatSelect(discord.ui.Select["SeatPickerView"]):
    def __init__(self, options: list[discord.SelectOption]) -> None:
        super().__init__(placeholder=tr("cn_claim_placeholder"), options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        room_id = self.view.room_id
        try:
            player = await _db(interaction).claim_codenames_player(room_id, self.values[0], str(interaction.user.id))
        except CodenamesSeatTakenError as taken:
            await interaction.response.edit_message(
                content=tr("cn_claim_taken", holder=f"<@{taken.holder_discord_user_id}>"), view=None
            )
            return
        name = discord.utils.escape_markdown(player.display_name or player.player_id)
        await interaction.response.edit_message(content=tr("cn_claim_done", name=name), view=None)
        await _refresh_room(interaction, room_id)


class _ReleaseButton(discord.ui.Button["SeatPickerView"]):
    def __init__(self) -> None:
        super().__init__(label=tr("cn_button_not_playing"), style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        room_id = self.view.room_id
        await _db(interaction).release_codenames_player(room_id, str(interaction.user.id))
        await interaction.response.edit_message(content=tr("cn_claim_released"), view=None)
        await _refresh_room(interaction, room_id)
