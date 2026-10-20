"""The "I'm playing" button on Codenames cards, the seat picker it opens, and
the admin seat card from ``/codenames seats``."""

from __future__ import annotations

import logging

import discord

from ..database import AsyncDatabase
from ..i18n import tr
from .models import CodenamesPlayer
from .store import CodenamesSeatTakenError

LOGGER = logging.getLogger(__name__)

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
            others = [p for p in players if p.discord_user_id is None][:_MAX_OPTIONS]
            if others:
                self.add_item(_DeviceSelect(others))
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


class _DeviceSelect(discord.ui.Select["SeatPickerView"]):
    """For a player already in a seat: another name in the room that is also them."""

    def __init__(self, players: list[CodenamesPlayer]) -> None:
        options = [
            discord.SelectOption(label=(p.display_name or p.player_id)[:100], value=p.player_id) for p in players
        ]
        super().__init__(placeholder=tr("cn_claim_device_placeholder"), options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        room_id = self.view.room_id
        db = _db(interaction)
        await db.add_codenames_device(room_id, self.values[0], str(interaction.user.id))
        name = next((o.label for o in self.options if o.value == self.values[0]), self.values[0])
        await interaction.response.edit_message(
            content=tr("cn_claim_device_done", name=discord.utils.escape_markdown(name)), view=None
        )
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


# ----------------------------------------------------------------------
# /codenames seats — an admin says who is who in the room
# ----------------------------------------------------------------------

_NOT_PLAYING = "__not_playing__"
_MAX_ASSIGN_ROWS = 10  # 3 components per person (text, row, select) + 5 others; Discord allows 40


def can_assign_seats(interaction: discord.Interaction) -> bool:
    permissions = interaction.permissions
    return bool(permissions.administrator or permissions.manage_guild or permissions.manage_messages)


async def build_seat_assign_view(
    db: AsyncDatabase, room_id: str, watch_id: int, guild: discord.Guild | None, *, note: str | None = None
) -> SeatAssignView | None:
    """The admin seat card for a room, or ``None`` if nobody has entered the room yet."""
    room = await db.get_codenames_room(room_id)
    bot_seat = room.bot_player_id if room is not None else None
    seats = [p for p in await db.list_codenames_players(room_id) if p.player_id != bot_seat and p.display_name]
    if not seats:
        return None
    invited = await db.list_codenames_watch_members(watch_id)
    # Invited players first, then anyone else already linked to a seat (so a wrong claim can be fixed).
    people = list(dict.fromkeys([*invited, *(p.discord_user_id for p in seats if p.discord_user_id)]))
    return SeatAssignView(room_id, watch_id, seats, people, guild, note=note)


def _member_name(guild: discord.Guild | None, user_id: str) -> str:
    member = guild.get_member(int(user_id)) if guild is not None and user_id.isdecimal() else None
    return member.display_name if member is not None else f"user {user_id}"


def notice_view(text: str) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(discord.ui.TextDisplay(text)))
    return view


class SeatAssignView(discord.ui.LayoutView):
    """One row per person: who they are now, and a dropdown of every name in the room."""

    def __init__(
        self,
        room_id: str,
        watch_id: int,
        seats: list[CodenamesPlayer],
        people: list[str],
        guild: discord.Guild | None,
        *,
        note: str | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.room_id = room_id
        self.watch_id = watch_id
        container = discord.ui.Container(accent_colour=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay(tr("cn_seats_title", room=room_id)))
        if note:
            container.add_item(discord.ui.TextDisplay(note))
        if not people:
            container.add_item(discord.ui.TextDisplay(tr("cn_seats_nobody_invited")))
        for user_id in people[:_MAX_ASSIGN_ROWS]:
            current = next((p for p in seats if p.discord_user_id == user_id), None)
            name = discord.utils.escape_markdown(current.display_name) if current and current.display_name else None
            line = tr("cn_seats_row_linked", user=f"<@{user_id}>", name=name) if name else tr("cn_seats_row_unlinked", user=f"<@{user_id}>")
            container.add_item(discord.ui.TextDisplay(line))
            container.add_item(discord.ui.ActionRow(_AssignSelect(user_id, seats, guild)))
        if len(people) > _MAX_ASSIGN_ROWS:
            container.add_item(discord.ui.TextDisplay(tr("cn_seats_more", count=len(people) - _MAX_ASSIGN_ROWS)))
        container.add_item(discord.ui.TextDisplay(tr("cn_seats_footer")))
        self.add_item(container)


class _AssignSelect(discord.ui.Select["SeatAssignView"]):
    def __init__(self, user_id: str, seats: list[CodenamesPlayer], guild: discord.Guild | None) -> None:
        self.user_id = user_id
        options = []
        for seat in seats[: _MAX_OPTIONS - 1]:
            if seat.discord_user_id == user_id:
                description = tr("cn_seats_option_this_person")
            elif seat.discord_user_id:
                description = tr("cn_seats_option_held_by", name=_member_name(guild, seat.discord_user_id)[:80])
            else:
                description = None
            options.append(
                discord.SelectOption(
                    label=(seat.display_name or seat.player_id)[:100], value=seat.player_id, description=description
                )
            )
        options.append(discord.SelectOption(label=tr("cn_seats_option_not_playing"), value=_NOT_PLAYING, emoji="🚫"))
        super().__init__(placeholder=tr("cn_seats_placeholder", name=_member_name(guild, user_id)[:80]), options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        if not can_assign_seats(interaction):
            await interaction.response.send_message(tr("cn_seats_not_allowed"), ephemeral=True)
            return
        db = _db(interaction)
        room_id = self.view.room_id
        user = f"<@{self.user_id}>"
        if self.values[0] == _NOT_PLAYING:
            await db.release_codenames_player(room_id, self.user_id)
            note = tr("cn_seats_done_released", user=user)
        else:
            before = {p.player_id: p.discord_user_id for p in await db.list_codenames_players(room_id)}
            previous = before.get(self.values[0])
            player = await db.claim_codenames_player(room_id, self.values[0], self.user_id, override=True)
            name = discord.utils.escape_markdown(player.display_name or player.player_id)
            note = tr("cn_seats_done_linked", user=user, name=name)
            if previous and previous != self.user_id:
                note += " " + tr("cn_seats_done_replaced", user=f"<@{previous}>")
        LOGGER.info(
            "Codenames seat set by %s in room %s: %s -> %s", interaction.user.id, room_id, self.user_id, self.values[0]
        )
        view = await build_seat_assign_view(db, room_id, self.view.watch_id, interaction.guild, note=note)
        await interaction.response.edit_message(view=view or notice_view(tr("cn_seats_room_empty")))
        await _refresh_room(interaction, room_id)
