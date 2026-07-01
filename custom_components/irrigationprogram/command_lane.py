"""Serialized command lane for cloud-backed controllers (e.g. RainPoint).

Cloud valve integrations frequently rate-limit or accept only one command per
account at a time, and may silently drop commands. A :class:`CommandLane`
serializes commands sharing a key, spaces them by a minimum interval, confirms
each reaches its expected state, retries with exponential backoff, and
dead-letters (surfacing an event) after exhausting attempts.

Lanes are shared per controller type via :func:`get_lane` so that every zone of
that type funnels through a single serializer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
import logging
import time
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .const import (
    CONST_LANE_BACKOFF_BASE,
    CONST_LANE_CONFIRM_TIMEOUT,
    CONST_LANE_MAX_ATTEMPTS,
    CONST_LANE_MIN_SPACING,
    CONST_LANE_POLL_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_LANES_KEY = f"{DOMAIN}_command_lanes"


@dataclass
class _Command:
    """A single queued command."""

    entity_id: str
    domain: str
    service: str
    expected_states: tuple[str, ...]
    data: dict[str, Any] = field(default_factory=dict)


class CommandLane:
    """Serialize, space, confirm, retry and dead-letter cloud-valve commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        key: str,
        *,
        min_spacing: float = CONST_LANE_MIN_SPACING,
        confirm_timeout: float = CONST_LANE_CONFIRM_TIMEOUT,
        poll_interval: float = CONST_LANE_POLL_INTERVAL,
        max_attempts: int = CONST_LANE_MAX_ATTEMPTS,
        backoff_base: float = CONST_LANE_BACKOFF_BASE,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.hass = hass
        self.key = key
        self._min_spacing = min_spacing
        self._confirm_timeout = confirm_timeout
        self._poll_interval = poll_interval
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._queue: asyncio.Queue[_Command] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._last_ts: float | None = None
        self.dead_letters: list[_Command] = []

    def submit(
        self,
        entity_id: str,
        domain: str,
        service: str,
        expected_states: Sequence[str],
        data: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a command; start the worker if it is not running. Non-blocking."""
        self._queue.put_nowait(
            _Command(entity_id, domain, service, tuple(expected_states), data or {})
        )
        if self._worker is None or self._worker.done():
            self._worker = self.hass.async_create_task(self._worker_loop())

    async def wait_idle(self) -> None:
        """Wait until the queue has drained (test/utility helper)."""
        await self._queue.join()

    async def _worker_loop(self) -> None:
        while not self._queue.empty():
            cmd = self._queue.get_nowait()
            try:
                await self._deliver(cmd)
            except Exception:  # noqa: BLE001 - never let the lane worker die
                _LOGGER.exception(
                    "Unexpected error delivering %s.%s for %s",
                    cmd.domain,
                    cmd.service,
                    cmd.entity_id,
                )
            finally:
                self._queue.task_done()

    async def _deliver(self, cmd: _Command) -> bool:
        await self._space()
        for attempt in range(1, self._max_attempts + 1):
            await self.hass.services.async_call(
                cmd.domain,
                cmd.service,
                {ATTR_ENTITY_ID: cmd.entity_id, **cmd.data},
            )
            if await self._confirm(cmd):
                return True
            if attempt < self._max_attempts:
                await self._sleep(self._backoff_base ** (attempt - 1))
        self._dead_letter(cmd)
        return False

    async def _space(self) -> None:
        """Enforce the minimum interval between the start of consecutive commands."""
        now = self._clock()
        if self._last_ts is not None:
            wait = self._min_spacing - (now - self._last_ts)
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
        self._last_ts = now

    async def _confirm(self, cmd: _Command) -> bool:
        """Poll the target entity until it reports an expected state or times out."""
        deadline = self._clock() + self._confirm_timeout
        while True:
            state = self.hass.states.get(cmd.entity_id)
            if state is not None and state.state in cmd.expected_states:
                return True
            if self._clock() >= deadline:
                return False
            await self._sleep(self._poll_interval)

    def _dead_letter(self, cmd: _Command) -> None:
        self.dead_letters.append(cmd)
        _LOGGER.error(
            "Command lane '%s' dead-lettered %s.%s for %s after %d attempts",
            self.key,
            cmd.domain,
            cmd.service,
            cmd.entity_id,
            self._max_attempts,
        )
        self.hass.bus.async_fire(
            "irrigation_event",
            {
                "action": "error",
                "error": "Command lane dead-letter",
                "device_id": cmd.entity_id,
                "lane": self.key,
            },
        )


def get_lane(hass: HomeAssistant, key: str, **kwargs: Any) -> CommandLane:
    """Return the shared command lane for ``key``, creating it on first use."""
    lanes: dict[str, CommandLane] = hass.data.setdefault(_LANES_KEY, {})
    if key not in lanes:
        lanes[key] = CommandLane(hass, key, **kwargs)
    return lanes[key]
