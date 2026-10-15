"""Where a Codenames room's turn pings go.

When a room is watched in an ordinary text channel, every turn, reminder and
line-up ping would land in that channel, which is noise for people who aren't
playing. If the bot is allowed to, it opens a thread for the room and pings
there instead; the live card stays in the channel so anyone can follow along.

* private thread when the bot may create one (only the pinged players are
  added, and see the pings),
* public thread when it may only create public ones,
* otherwise the pings stay in the channel, as before.

Rooms already watched in a thread (tournament match threads, or a link posted
in a thread) keep pinging there.
"""

from __future__ import annotations

import discord

PRIVATE = "private"
PUBLIC = "public"

THREAD_NAME_MAX = 100


def ping_thread_kind(channel: object, me: object) -> str | None:
    """``PRIVATE``, ``PUBLIC`` or ``None`` (keep pinging in *channel*)."""
    # Threads, forum posts, voice-channel chats and DMs are left alone.
    if not isinstance(channel, discord.TextChannel) or me is None:
        return None
    permissions = channel.permissions_for(me)
    if not (permissions.view_channel and permissions.send_messages_in_threads):
        return None
    # Announcement channels can't have private threads.
    if channel.type == discord.ChannelType.text and permissions.create_private_threads:
        return PRIVATE
    if permissions.create_public_threads:
        return PUBLIC
    return None


def ping_thread_name(room_id: str) -> str:
    return f"🕵️ Codenames · {room_id} · turn pings"[:THREAD_NAME_MAX]
