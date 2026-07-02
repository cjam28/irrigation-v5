"""Tests for the serialized cloud-controller command lane (F5).

The lane serializes commands sharing a key, spaces them by a minimum interval,
confirms each reaches its expected state, retries with backoff, and
dead-letters after exhausting attempts.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError

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


async def test_service_error_is_retried_then_confirms():
    """A raising service call counts as a failed attempt and is retried.

    Cloud integrations surface rate limits/timeouts (e.g. code=4004) as
    exceptions; dropping the command on the floor would leave a valve in the
    wrong state with no retry and no dead-letter.
    """
    hass = _hass(state_value="open")
    hass.services.async_call = AsyncMock(
        side_effect=[HomeAssistantError("code=4004"), None]
    )
    lane = _lane(hass)
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 2
    assert lane.dead_letters == []


async def test_service_error_dead_letters_after_max_attempts():
    """Persistent service errors exhaust the attempts and dead-letter."""
    hass = _hass(state_value="closed")
    hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("timeout"))
    lane = _lane(hass, max_attempts=3)
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 3
    assert len(lane.dead_letters) == 1
    hass.bus.async_fire.assert_called()


async def test_retry_waits_min_spacing_since_previous_attempt():
    """Retries are cloud calls too: they must honour the lane's min spacing."""
    now = {"t": 0.0}
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        now["t"] += secs

    hass = _hass(state_value="closed")  # never confirms
    lane = _lane(
        hass,
        min_spacing=30,
        max_attempts=2,
        backoff_base=1,  # 1s nominal backoff, dwarfed by the spacing
        clock=lambda: now["t"],
        sleep=fake_sleep,
    )
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 2
    # attempt 1 issues at t=0 and the backoff sleeps 1s; the second attempt
    # must then wait the remaining 29s so the cloud calls are >= 30s apart
    assert 29 in sleeps


async def test_submit_during_delivery_is_processed():
    """A command submitted while the worker is mid-delivery still runs."""
    hass = _hass(state_value="open")
    delivered = []

    async def record(domain, service, data):
        delivered.append(data["entity_id"])
        if len(delivered) == 1:
            lane.submit("valve.z2", "valve", "open_valve", ("open",))
        await asyncio.sleep(0)

    hass.services.async_call = AsyncMock(side_effect=record)
    lane = _lane(hass)
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    await lane.wait_idle()
    assert delivered == ["valve.z1", "valve.z2"]


async def test_confirm_uses_attribute_when_requested():
    """With confirm_attr set, the entity attribute is the confirmation truth.

    Cloud integrations may set the state optimistically on command, so
    state == 'open' proves nothing; a device-truth attribute (e.g. homgar's
    valve_state == 'irrigation') only changes when the device really ran.
    """
    hass = _hass()
    st = MagicMock()
    st.state = "open"  # optimistic echo
    st.attributes = {"valve_state": "irrigation"}  # device truth
    hass.states.get.return_value = st
    lane = _lane(hass)
    lane.submit(
        "valve.z1", "valve", "open_valve",
        ("irrigation", "open", "on"),
        confirm_attr="valve_state",
    )
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 1
    assert lane.dead_letters == []


async def test_confirm_attr_rejects_optimistic_state_echo():
    """Optimistic state 'open' with device-truth 'idle' must NOT confirm."""
    hass = _hass()
    st = MagicMock()
    st.state = "open"  # optimistic echo from the integration
    st.attributes = {"valve_state": "idle"}  # device never ran
    hass.states.get.return_value = st
    lane = _lane(hass, max_attempts=2)
    lane.submit(
        "valve.z1", "valve", "open_valve",
        ("irrigation", "open", "on"),
        confirm_attr="valve_state",
    )
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 2  # retried
    assert len(lane.dead_letters) == 1  # and surfaced


async def test_confirm_attr_falls_back_to_state_when_absent():
    """Entities without the attribute keep legacy state-based confirmation."""
    hass = _hass()
    st = MagicMock()
    st.state = "open"
    st.attributes = {}
    hass.states.get.return_value = st
    lane = _lane(hass)
    lane.submit(
        "valve.z1", "valve", "open_valve",
        ("irrigation", "open", "on"),
        confirm_attr="valve_state",
    )
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 1
    assert lane.dead_letters == []


async def test_identical_pending_command_is_deduplicated():
    """Re-submitting a command identical to the newest pending one is a no-op.

    The re-arm keepalive may re-submit an open every monitor cycle; only one
    may be queued at a time or the lane floods.
    """
    hass = _hass(state_value="open")
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated(domain, service, data):
        started.set()
        await release.wait()

    hass.services.async_call = AsyncMock(side_effect=gated)
    lane = _lane(hass)
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    await started.wait()  # first command mid-delivery
    lane.submit("valve.z2", "valve", "open_valve", ("open",))
    lane.submit("valve.z2", "valve", "open_valve", ("open",))  # dup: dropped
    lane.submit("valve.z2", "valve", "open_valve", ("open",))  # dup: dropped
    release.set()
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 2  # z1 + one z2


async def test_open_close_open_alternation_is_not_deduplicated():
    """Only consecutive identical commands dedup; sequences stay intact."""
    hass = _hass(state_value="open")
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated(domain, service, data):
        started.set()
        await release.wait()

    hass.services.async_call = AsyncMock(side_effect=gated)
    lane = _lane(hass)
    lane.submit("valve.z1", "valve", "open_valve", ("open",))
    await started.wait()
    lane.submit("valve.z2", "valve", "open_valve", ("open",))
    # confirm-state 'open' keeps the mock confirming every command; the test
    # is about queueing, not confirmation
    lane.submit("valve.z2", "valve", "close_valve", ("open",))
    lane.submit("valve.z2", "valve", "open_valve", ("open",))  # legit re-open
    release.set()
    await lane.wait_idle()
    assert hass.services.async_call.await_count == 4


async def test_dead_letters_are_capped():
    """The dead-letter list keeps only the most recent entries."""
    hass = _hass(state_value="closed")  # never confirms
    lane = _lane(hass, max_attempts=1)
    for i in range(60):
        lane.submit(f"valve.z{i}", "valve", "open_valve", ("open",))
    await lane.wait_idle()
    assert len(lane.dead_letters) == 50
    assert lane.dead_letters[-1].entity_id == "valve.z59"
