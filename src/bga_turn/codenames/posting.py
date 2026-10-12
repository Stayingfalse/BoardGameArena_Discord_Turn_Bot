"""Where Codenames messages come from: a "Codenames" webhook, or the bot itself.

The bot posts as one name and avatar ("BGA Bot"). When it may manage webhooks
in a channel, Codenames cards and pings go through a webhook it owns, named
"Codenames" with its own avatar, so they're clearly Codenames. The webhook
belongs to the bot's application, so the card's buttons still reach the bot.
Without that permission, messages are sent by the bot as usual (and pings say
"Codenames" in their text instead).

Webhook messages can't be replies, so pings sent that way aren't attached to
the card; they carry their own "Open room" button.
"""

from __future__ import annotations

import io
import logging
import time
from importlib.resources import files

import discord

LOGGER = logging.getLogger(__name__)

WEBHOOK_NAME = "Codenames"
VIA_WEBHOOK = "webhook"
VIA_BOT = "bot"

_RETRY_SECONDS = 600  # look again this long after finding no usable webhook


def default_avatar() -> bytes:
    return files("bga_turn").joinpath("static", "codenames-avatar.png").read_bytes()


def make_file(attachment: tuple[str, bytes] | None) -> list[discord.File]:
    """A fresh ``discord.File`` per send: discord.py doesn't rewind one it already sent."""
    if attachment is None:
        return []
    filename, data = attachment
    return [discord.File(io.BytesIO(data), filename=filename)]


class BotPoster:
    """Sends as the bot, in the channel or thread itself."""

    via = VIA_BOT

    def __init__(self, channel) -> None:
        self.channel = channel

    async def send(self, content: str | None = None, *, view=None, allowed_mentions, reply_to=None, attachment=None):
        kwargs = {"allowed_mentions": allowed_mentions}
        if view is not None:
            kwargs["view"] = view
        if attachment is not None:
            kwargs["files"] = make_file(attachment)
        if reply_to is not None:
            kwargs["reference"] = reply_to.to_reference(fail_if_not_exists=False)
            kwargs["mention_author"] = False
        return await self.channel.send(content, **kwargs)

    async def fetch(self, message_id: int):
        return await self.channel.fetch_message(message_id)

    async def delete(self, message_id: int) -> None:
        await self.channel.get_partial_message(message_id).delete()


class WebhookPoster:
    """Sends through the channel's "Codenames" webhook (into a thread if needed)."""

    via = VIA_WEBHOOK

    def __init__(self, webhook, thread=None) -> None:
        self.webhook = webhook
        self.thread = thread

    def _thread_kwargs(self) -> dict:
        return {"thread": self.thread} if self.thread is not None else {}

    async def send(self, content: str | None = None, *, view=None, allowed_mentions, reply_to=None, attachment=None):
        kwargs = self._thread_kwargs()
        if view is not None:
            kwargs["view"] = view
        if attachment is not None:
            kwargs["files"] = make_file(attachment)
        return await self.webhook.send(
            content if content is not None else discord.utils.MISSING,
            username=WEBHOOK_NAME,
            allowed_mentions=allowed_mentions,
            wait=True,
            **kwargs,
        )

    async def fetch(self, message_id: int):
        return await self.webhook.fetch_message(message_id, **self._thread_kwargs())

    async def delete(self, message_id: int) -> None:
        await self.webhook.delete_message(message_id, **self._thread_kwargs())


class Posters:
    """Finds (or creates) the bot's "Codenames" webhook per channel, falling back to the bot."""

    def __init__(self, bot: discord.Client, *, enabled: bool = True, avatar: bytes | None = None) -> None:
        self.bot = bot
        self.enabled = enabled
        self._avatar = avatar
        self._webhooks: dict[int, tuple[object | None, float]] = {}

    async def for_sending(self, channel) -> BotPoster | WebhookPoster:
        """The poster for new messages: the webhook when possible, else the bot."""
        poster = await self._webhook_poster(channel)
        return poster or BotPoster(channel)

    async def for_message(self, channel, via: str | None) -> BotPoster | WebhookPoster | None:
        """The poster able to fetch or delete a message sent *via* it (``None`` if it's gone)."""
        if via == VIA_WEBHOOK:
            return await self._webhook_poster(channel)
        return BotPoster(channel)

    def forget(self, channel) -> None:
        """The webhook was deleted: look again (and recreate it) next time."""
        base, _thread = _base_channel(channel)
        self._webhooks.pop(getattr(base, "id", None), None)

    async def _webhook_poster(self, channel) -> WebhookPoster | None:
        if not self.enabled:
            return None
        base, thread = _base_channel(channel)
        base_id = getattr(base, "id", None)
        cached = self._webhooks.get(base_id)
        if cached is not None:
            webhook, checked_at = cached
            if webhook is not None:
                return WebhookPoster(webhook, thread)
            if time.monotonic() - checked_at < _RETRY_SECONDS:
                return None
        webhook = await self._find_or_create(base)
        self._webhooks[base_id] = (webhook, time.monotonic())
        return WebhookPoster(webhook, thread) if webhook is not None else None

    async def _find_or_create(self, base):
        guild = getattr(base, "guild", None)
        me = getattr(guild, "me", None)
        permissions_for = getattr(base, "permissions_for", None)
        if me is None or permissions_for is None or not permissions_for(me).manage_webhooks:
            return None
        bot_user_id = getattr(self.bot.user, "id", None)
        try:
            for webhook in await base.webhooks():
                owner = getattr(webhook, "user", None)
                if webhook.name == WEBHOOK_NAME and owner is not None and owner.id == bot_user_id and webhook.token:
                    return webhook
            return await base.create_webhook(
                name=WEBHOOK_NAME,
                avatar=self._avatar if self._avatar is not None else default_avatar(),
                reason="Codenames game updates",
            )
        except discord.DiscordException as exc:
            LOGGER.warning("Could not set up the Codenames webhook in channel %s: %s", base_id_of(base), exc)
            return None


def _base_channel(channel):
    """Webhooks live on the parent channel; threads are posted into with ``thread=``."""
    if isinstance(channel, discord.Thread):
        return channel.parent, channel
    return channel, None


def base_id_of(channel) -> object:
    return getattr(channel, "id", "?")
