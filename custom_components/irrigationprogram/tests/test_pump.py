"""Tests for PumpClass valve/switch actuation.

Covers the fix for driving a valve-domain pump (open/close services) and
tolerating a lagging/``unknown`` state: the pump is commanded unless it is
already confirmed to be in the target state.
"""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)

from custom_components.irrigationprogram.pump import PumpClass


def _make_pump(entity_id):
    """Build a PumpClass with a mock hass, neutralising constructor side effects."""
    hass = MagicMock()
    # The constructor schedules async_stop() via async_create_task; close the
    # coroutine without running it so it neither warns nor pollutes call counts.
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    hass.bus.async_listen = MagicMock()
    hass.services.async_call = AsyncMock()
    pump = PumpClass(hass, entity_id, [], None)
    hass.services.async_call.reset_mock()
    return hass, pump


def _set_state(hass, value):
    """Point hass.states.get at a State with .state == value (or None)."""
    if value is None:
        hass.states.get.return_value = None
    else:
        state = MagicMock()
        state.state = value
        hass.states.get.return_value = state


# --- valve-domain pump: async_start opens ---------------------------------

async def test_valve_pump_opens_when_closed():
    """A closed valve pump is opened on start (regression: object-vs-string bug)."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, "closed")
    await pump.async_start()
    hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_OPEN_VALVE, {ATTR_ENTITY_ID: "valve.pump1"}
    )


async def test_valve_pump_opens_when_unknown():
    """A valve reporting 'unknown' is still opened (lagging-state tolerance)."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, "unknown")
    await pump.async_start()
    hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_OPEN_VALVE, {ATTR_ENTITY_ID: "valve.pump1"}
    )


async def test_valve_pump_not_reopened_when_open():
    """An already-open valve is not commanded again on start."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, "open")
    await pump.async_start()
    hass.services.async_call.assert_not_awaited()


# --- valve-domain pump: async_stop closes ---------------------------------

async def test_valve_pump_closes_when_open():
    """An open valve pump is closed on stop."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, "open")
    await pump.async_stop()
    hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_CLOSE_VALVE, {ATTR_ENTITY_ID: "valve.pump1"}
    )


async def test_valve_pump_closes_when_unknown():
    """A valve reporting 'unknown' is still closed on stop."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, "unknown")
    await pump.async_stop()
    hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_CLOSE_VALVE, {ATTR_ENTITY_ID: "valve.pump1"}
    )


async def test_valve_pump_not_reclosed_when_closed():
    """An already-closed valve is not commanded again on stop."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, "closed")
    await pump.async_stop()
    hass.services.async_call.assert_not_awaited()


# --- switch-domain pump: unchanged happy path + unknown tolerance ----------

async def test_switch_pump_turns_on_when_off():
    """A switch pump that is off is turned on."""
    hass, pump = _make_pump("switch.pump1")
    _set_state(hass, "off")
    await pump.async_start()
    hass.services.async_call.assert_awaited_once_with(
        "switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.pump1"}
    )


async def test_switch_pump_turns_on_when_unknown():
    """A switch reporting 'unknown' is turned on (lagging-state tolerance)."""
    hass, pump = _make_pump("switch.pump1")
    _set_state(hass, "unknown")
    await pump.async_start()
    hass.services.async_call.assert_awaited_once_with(
        "switch", SERVICE_TURN_ON, {ATTR_ENTITY_ID: "switch.pump1"}
    )


async def test_switch_pump_not_reissued_when_on():
    """An already-on switch is not commanded again on start."""
    hass, pump = _make_pump("switch.pump1")
    _set_state(hass, "on")
    await pump.async_start()
    hass.services.async_call.assert_not_awaited()


async def test_switch_pump_turns_off_when_on():
    """A switch pump that is on is turned off on stop."""
    hass, pump = _make_pump("switch.pump1")
    _set_state(hass, "on")
    await pump.async_stop()
    hass.services.async_call.assert_awaited_once_with(
        "switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: "switch.pump1"}
    )


async def test_switch_pump_not_turned_off_when_off():
    """An already-off switch is not commanded again on stop."""
    hass, pump = _make_pump("switch.pump1")
    _set_state(hass, "off")
    await pump.async_stop()
    hass.services.async_call.assert_not_awaited()


# --- missing entity (state is None) → no action (file idiom) ---------------

async def test_no_action_when_state_missing():
    """A pump whose entity has no state is left alone (None-guard)."""
    hass, pump = _make_pump("valve.pump1")
    _set_state(hass, None)
    await pump.async_start()
    await pump.async_stop()
    hass.services.async_call.assert_not_awaited()


# --- shared pump across programs: cross-program ref-counting ---------------
#
# Two config entries can declare the SAME pump entity (a morning and an
# evening program driving one master valve). Each program's PumpClass must
# not close the physical pump while another program that declares it is
# actively running (observed live: the finishing program's turn_off_pump_all
# closed the master mid-run of the other program - silent paper watering).

def _make_shared_hass():
    hass = MagicMock()
    hass.data = {}
    hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())
    hass.bus.async_listen = MagicMock(return_value=MagicMock())
    hass.services.async_call = AsyncMock()
    return hass


def _mock_program(entity_id):
    prog = MagicMock()
    prog.entity_id = entity_id
    return prog


def _wire_states(hass, mapping):
    def get(entity_id):
        value = mapping.get(entity_id)
        if value is None:
            return None
        state = MagicMock()
        state.state = value
        return state
    hass.states.get.side_effect = get


def _event(action, program):
    event = MagicMock()
    event.data = {"action": action, "program": program, "device_id": "valve.z9"}
    return event


async def test_shared_pump_not_closed_while_other_program_running():
    hass = _make_shared_hass()
    prog_a = _mock_program("switch.prog_a")
    prog_b = _mock_program("switch.prog_b")
    pump_a = PumpClass(hass, "switch.shared_pump", [], prog_a)
    PumpClass(hass, "switch.shared_pump", [], prog_b)
    hass.services.async_call.reset_mock()
    _wire_states(hass, {"switch.shared_pump": "on", "switch.prog_b": "on"})
    await pump_a.handle_event(_event("turn_off_pump_all", "switch.prog_a"))
    hass.services.async_call.assert_not_awaited()


async def test_shared_pump_closed_when_other_program_idle():
    hass = _make_shared_hass()
    prog_a = _mock_program("switch.prog_a")
    prog_b = _mock_program("switch.prog_b")
    pump_a = PumpClass(hass, "switch.shared_pump", [], prog_a)
    PumpClass(hass, "switch.shared_pump", [], prog_b)
    hass.services.async_call.reset_mock()
    _wire_states(hass, {"switch.shared_pump": "on", "switch.prog_b": "off"})
    await pump_a.handle_event(_event("turn_off_pump_all", "switch.prog_a"))
    hass.services.async_call.assert_awaited_once_with(
        "switch", SERVICE_TURN_OFF, {ATTR_ENTITY_ID: "switch.shared_pump"}
    )


async def test_shared_pump_zone_path_also_guarded():
    """The per-zone turn_off_pump path honours the other running program too."""
    hass = _make_shared_hass()
    prog_a = _mock_program("switch.prog_a")
    prog_b = _mock_program("switch.prog_b")
    pump_a = PumpClass(hass, "switch.shared_pump", [], prog_a)
    PumpClass(hass, "switch.shared_pump", [], prog_b)
    hass.services.async_call.reset_mock()
    _wire_states(hass, {"switch.shared_pump": "on", "switch.prog_b": "on"})
    await pump_a.handle_event(_event("turn_off_pump", "switch.prog_a"))
    hass.services.async_call.assert_not_awaited()


async def test_detached_pump_leaves_registry_and_stops_listening():
    hass = _make_shared_hass()
    prog_a = _mock_program("switch.prog_a")
    prog_b = _mock_program("switch.prog_b")
    pump_a = PumpClass(hass, "switch.shared_pump", [], prog_a)
    pump_b = PumpClass(hass, "switch.shared_pump", [], prog_b)
    cancel_b = pump_b._cancel
    pump_b.detach()
    assert cancel_b.called
    hass.services.async_call.reset_mock()
    _wire_states(hass, {"switch.shared_pump": "on", "switch.prog_b": "on"})
    await pump_a.handle_event(_event("turn_off_pump_all", "switch.prog_a"))
    hass.services.async_call.assert_awaited_once()
