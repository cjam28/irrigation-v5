"""Tests for the serialized cloud-controller command lane (F5).

The lane serializes commands sharing a key, spaces them by a minimum interval,
confirms each reaches its expected state, retries with backoff, and
dead-letters after exhausting attempts.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.irrigationprogram.command_lane import CommandLane


def _hass(state_value=None, state_sequence=None):
    """Mock hass whose valve/switch entity reports the given state(s)."""
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.bus.async_fire = MagicMock()
    hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    if state_sequence is not None:
        hass.states.get.side_effect = [
            (MagicMock(state=s) if s is not None else None) for s in state_sequence
        ]
    else:
        st = MagicMock()
        st.state = state_value
        hass.states.get.return_value = st
    return hass


def _lane(hass, **kw):
    """A lane with real time disabled for deterministic tests."""
    defaults = dict(
        min_spacing=0,
        confirm_timeout=0,
        poll_interval=0,
        max_attempts=3,
        backoff_base=2,
        sleep=AsyncMock(),
        clock=lambda: 0,
    )
    defaults.update(kw)
    return CommandLane(hass, "rainpoint", **defaults)


async def test_command_confirmed_in_one_attempt():
    """A command that immediately reaches its expected state is not retried."""
    hass = _hass(state_value="open")
    lane = _lane(hass)
    lane.submit("valve.z1", "valve", "open_valve", ("open", "on"))
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 1
    assert lane.dead_letters == []


async def test_command_retries_then_confirms():
    """If the first attempt does not confirm, the command is re-issued."""
    # confirm() polls once per attempt (confirm_timeout=0): closed, then open
    hass = _hass(state_sequence=["closed", "open"])
    sleep = AsyncMock()
    lane = _lane(hass, sleep=sleep)
    lane.submit("valve.z1", "valve", "open_valve", ("open", "on"))
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 2
    sleep.assert_awaited()  # backoff between attempts
    assert lane.dead_letters == []


async def test_command_dead_letters_after_max_attempts():
    """A command that never confirms is dead-lettered and surfaced."""
    hass = _hass(state_value="closed")  # never reaches 'open'
    lane = _lane(hass, max_attempts=3)
    lane.submit("valve.z1", "valve", "open_valve", ("open", "on"))
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 3
    assert len(lane.dead_letters) == 1
    assert lane.dead_letters[0].entity_id == "valve.z1"
    hass.bus.async_fire.assert_called()  # dead-letter is visible via an event


async def test_commands_are_serialized_in_order():
    """Commands submitted together are processed one at a time, FIFO."""
    order = []

    async def record(domain, service, data):
        order.append(data["entity_id"])
        await asyncio.sleep(0)

    hass = _hass(state_value="open")
    hass.services.async_call = AsyncMock(side_effect=record)
    lane = _lane(hass)
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    lane.submit("valve.z2", "valve", "open_valve", ("open",))
    lane.submit("valve.z3", "valve", "open_valve", ("open",))
    await lane.wait_idle()
    assert order == ["valve.z1", "valve.z2", "valve.z3"]


async def test_min_spacing_delays_second_command():
    """Consecutive commands are spaced by at least min_spacing seconds."""
    hass = _hass(state_value="open")
    sleep = AsyncMock()
    clock_vals = iter([0, 5, 5])  # first at t=0, second observed at t=5
    lane = _lane(hass, min_spacing=30, sleep=sleep, clock=lambda: next(clock_vals))
    await lane._space()  # first command: no wait, stamps t=0
    await lane._space()  # second command: must wait 30 - 5 = 25s
    waits = [c.args[0] for c in sleep.await_args_list if c.args]
    assert any(abs(w - 25) < 1e-9 for w in waits)
