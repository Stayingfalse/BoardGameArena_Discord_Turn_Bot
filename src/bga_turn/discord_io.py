"""Rate-limit-aware Discord calls shared by the monitor and the commands."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from datetime import UTC, datetime, timedelta

import discord

from .i18n import tr

LOGGER = logging.getLogger(__name__)


class DiscordRateLimiter:
    """Queues Discord calls per channel and pauses all Discord I/O after a 429.

    Discord's own limits are per channel (about 5 writes per 5s, fewer edits per
    second on messages older than an hour), so calls wait for a free slot before
    being sent, and a rate-limited call is retried after Discord's ``retry_after``.
    """

    _WRITE_THRESHOLD = 5
    _WRITE_WINDOW_SECONDS = 5.0
    _OLD_EDIT_THRESHOLD = 3
    _OLD_EDIT_WINDOW_SECONDS = 6.0
    _OLD_EDIT_AGE = timedelta(hours=1)
    _GLOBAL_CONCURRENT_CALLS = 8
    _CHANNEL_STATE_TTL_SECONDS = 1800.0

    def __init__(self) -> None:
        self._channel_locks: dict[str, asyncio.Lock] = {}
        self._channel_requests: dict[tuple[str, str], deque[float]] = {}
        self._channel_activity: dict[str, float] = {}
        self._global_semaphore = asyncio.Semaphore(self._GLOBAL_CONCURRENT_CALLS)
        # Monotonic timestamp before which Discord I/O should be skipped due to
        # rate-limiting. Zero means no active pause.
        self._paused_until: float = 0.0
        # Exponential backoff for Discord rate-limit pauses (seconds).
        self._pause_backoff: float = 5.0

    def reset(self) -> None:
        self._channel_locks.clear()
        self._channel_requests.clear()
        self._channel_activity.clear()
        self._paused_until = 0.0
        self._pause_backoff = 5.0

    async def call(
        self,
        operation: str,
        table_id: str,
        coro_factory,
        *,
        max_retries: int = 3,
        channel_id: str | None = None,
        operation_kind: str = "send",
        message_created_at: datetime | None = None,
    ):
        """Call a Discord coroutine, retrying on HTTP 429 rate-limit responses.

        *coro_factory* must be a zero-argument callable that returns a fresh
        coroutine each time it is called (awaitables cannot be re-awaited).

        Calls are proactively queued per channel to reduce rate-limit hits before
        they happen, then retried on 429 responses using Discord's ``retry_after``
        value plus jitter. If retries are exhausted it logs a warning, records a
        Discord I/O pause and returns ``None``. All other exceptions are re-raised
        immediately so callers can handle them normally.
        """
        for attempt in range(1, max_retries + 2):
            # Record a slot (and wait if the channel bucket is full) on every
            # attempt, including retries, so the proactive queue correctly
            # accounts for all API calls made against each channel.
            if channel_id is not None:
                await self._wait_for_channel_slot(
                    channel_id=channel_id,
                    operation=operation,
                    operation_kind=operation_kind,
                    message_created_at=message_created_at,
                )
            try:
                async with self._global_semaphore:
                    result = await coro_factory()
                # Successful call — reset the Discord backoff.
                self._paused_until = 0.0
                self._pause_backoff = 5.0
                return result
            except (discord.RateLimited, discord.HTTPException) as exc:
                retry_after: float | None = None
                if isinstance(exc, discord.RateLimited):
                    retry_after = exc.retry_after
                elif isinstance(exc, discord.HTTPException) and exc.status == 429:
                    # discord.py populates retry_after on HTTPException for 429s too.
                    retry_after = getattr(exc, "retry_after", None)

                if retry_after is None:
                    # Not a rate-limit error; let it propagate.
                    raise

                # Broadcast the rate-limit window immediately so all other
                # concurrent workers stop issuing Discord calls while we wait.
                broadcast_until = time.monotonic() + retry_after
                if broadcast_until > self._paused_until:
                    self._paused_until = broadcast_until

                if attempt > max_retries:
                    LOGGER.warning(
                        tr(
                            "discord_rate_limit_exhausted",
                            operation=operation,
                            table_id=table_id,
                        )
                    )
                    # Extend the I/O pause to at least retry_after so a brief
                    # Discord window doesn't cause an immediate re-flood.
                    extended_until = time.monotonic() + max(retry_after, min(self._pause_backoff, 60.0))
                    if extended_until > self._paused_until:
                        self._paused_until = extended_until
                    self._pause_backoff = min(self._pause_backoff * 2, 60.0)
                    return None

                sleep_for = retry_after + random.uniform(0.1, 0.5)
                LOGGER.debug(
                    tr(
                        "discord_rate_limited_retry",
                        operation=operation,
                        table_id=table_id,
                        retry_after=sleep_for,
                        attempt=attempt,
                        max_retries=max_retries,
                    )
                )
                await asyncio.sleep(sleep_for)

    async def _wait_for_channel_slot(
        self,
        *,
        channel_id: str,
        operation: str,
        operation_kind: str,
        message_created_at: datetime | None,
    ) -> None:
        lock = self._channel_locks.setdefault(channel_id, asyncio.Lock())
        while True:
            sleep_for: float | None = None
            async with lock:
                now = time.monotonic()
                self._channel_activity[channel_id] = now
                bucket_limits: list[tuple[str, int, float]] = [
                    (
                        "write",
                        self._WRITE_THRESHOLD,
                        self._WRITE_WINDOW_SECONDS,
                    )
                ]
                if (
                    operation_kind == "edit"
                    and message_created_at is not None
                    and self._is_old_message(message_created_at)
                ):
                    bucket_limits.append(
                        (
                            "edit_old",
                            self._OLD_EDIT_THRESHOLD,
                            self._OLD_EDIT_WINDOW_SECONDS,
                        )
                    )

                waits: list[float] = []
                for bucket_name, threshold, window_seconds in bucket_limits:
                    bucket_key = (channel_id, bucket_name)
                    attempts = self._channel_requests.setdefault(bucket_key, deque())
                    while attempts and now - attempts[0] >= window_seconds:
                        attempts.popleft()
                    if len(attempts) >= threshold:
                        waits.append(window_seconds - (now - attempts[0]))

                if not waits:
                    for bucket_name, _threshold, _window_seconds in bucket_limits:
                        self._channel_requests[(channel_id, bucket_name)].append(now)
                    return

                sleep_for = max(waits) + random.uniform(0.05, 0.20)

            if sleep_for is None:
                return
            LOGGER.debug(
                tr(
                    "discord_queue_wait",
                    operation=operation,
                    channel_id=channel_id,
                    wait=sleep_for,
                )
            )
            await asyncio.sleep(sleep_for)

    def sweep(self, active_channel_ids: set[str]) -> None:
        now = time.monotonic()
        stale_channels = [
            channel_id
            for channel_id, last_seen in self._channel_activity.items()
            if channel_id not in active_channel_ids
            and (now - last_seen) > self._CHANNEL_STATE_TTL_SECONDS
        ]
        if not stale_channels:
            return
        stale_set = set(stale_channels)
        for channel_id in stale_channels:
            self._channel_locks.pop(channel_id, None)
            self._channel_activity.pop(channel_id, None)
        for bucket_key in list(self._channel_requests):
            if bucket_key[0] in stale_set:
                self._channel_requests.pop(bucket_key, None)

    @classmethod
    def _is_old_message(cls, created_at: datetime) -> bool:
        normalized = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
        return (datetime.now(UTC) - normalized) >= cls._OLD_EDIT_AGE

    def io_allowed(self) -> bool:
        """Return ``True`` unless Discord I/O is currently paused due to rate-limiting."""
        now = time.monotonic()
        if now >= self._paused_until:
            return True
        wait = self._paused_until - now
        LOGGER.debug(tr("discord_io_paused", wait=wait))
        return False
