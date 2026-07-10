"""Mid-run deliberate-stop handling.

A status change while a zone is watering is either a device fault (the valve
dropped — re-arm it) or a deliberate signal to stop (disable, rain-stop,
no water) — and the two must never be confused: re-arming a valve against an
explicit disable forces it open against the operator (observed live
2026-07-09: a zone held open 52 minutes against its enable switch).

These tests pin the intended partition:
- zone_disabled / program_disabled arriving mid-run stop the zone cleanly;
- the same statuses present at run start (a manual run of a disabled zone is
  an intentional override) do not stop that run;
- rain_behaviour == "finish" lets the running zone complete when rain-stop
  trips mid-run, while new starts stay suspended;
- genuine device drops still re-arm.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.const import SERVICE_OPEN_VALVE

from custom_components.irrigationprogram.const import (
    CONST_ADJUSTED_OFF,
    CONST_NO_WATER_SOURCE,
    CONST_ON,
    CONST_PROGRAM_DISABLED,
    CONST_RAINING_STOP,
    CONST_ZONE_DISABLED,
)
from custom_components.irrigationprogram.zone import Zone


def _run_state_zone(controller, optimistic=False, rain_behaviour="stop"):
    """Bare Zone with the attributes the mid-run handlers read."""
    zone = Zone.__new__(Zone)
    zone._programdata = MagicMock()
    zone._programdata.controller_type = controller
    zone._programdata.rain_behaviour = rain_behaviour
    zone._zonedata = MagicMock()
    zone._zonedata.type = "valve"
    zone._zonedata.zone = "valve.z1"
    zone._zonedata.optimistic = optimistic
    zone.hass = MagicMock()
    zone.hass.bus.async_fire = MagicMock()
    zone._scheduled = False
    zone._latency = 5
    zone._continue_on_unexpected_state = False
    zone._stop = False
    zone._aborted = False
    zone.async_turn_off_zone_natural = AsyncMock()
    return zone


def _name_patch():
    return patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1")


# --- _handle_unexpected_run_state: deliberate stops must not re-arm --------


async def test_lane_zone_does_not_rearm_on_zone_disabled_mid_run():
    """Disabling a zone mid-run must stop it, not force the valve back open.

    Regression: the re-arm keepalive treated zone_disabled like a device
    drop and re-opened the valve every monitor cycle against the disable.
    """
    zone = _run_state_zone("rainpoint")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state(
            CONST_ZONE_DISABLED, warning_issued=False
        )
    lane.submit.assert_not_called()
    assert zone._stop is True
    assert zone._aborted is True


async def test_lane_zone_does_not_rearm_on_program_disabled_mid_run():
    """Disabling the program mid-run must stop the zone, not re-arm it."""
    zone = _run_state_zone("rainpoint")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state(
            CONST_PROGRAM_DISABLED, warning_issued=False
        )
    lane.submit.assert_not_called()
    assert zone._stop is True
    assert zone._aborted is True


async def test_exempt_disable_neither_stops_nor_rearms():
    """A run started under an explicit disable ignores that same status.

    A manual run of a disabled zone (or of a zone in a disabled program) is
    an intentional override; the pre-existing disable is not a stop signal
    for that run, and it is not a device drop either.
    """
    zone = _run_state_zone("rainpoint")
    zone._deliberate_stop_exempt = frozenset({CONST_PROGRAM_DISABLED})
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state(
            CONST_PROGRAM_DISABLED, warning_issued=False
        )
    lane.submit.assert_not_called()
    assert zone._stop is False


async def test_lane_zone_does_not_rearm_on_raining_stop_finish_mode():
    """With rain_behaviour 'finish', rain mid-run neither stops nor re-arms."""
    zone = _run_state_zone("rainpoint", rain_behaviour="finish")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state(
            CONST_RAINING_STOP, warning_issued=False
        )
    lane.submit.assert_not_called()
    assert zone._stop is False


async def test_lane_zone_stops_on_raining_stop_stop_mode():
    """With rain_behaviour 'stop', rain reaching the re-arm path stops the
    zone instead of re-opening the valve in the rain (race backstop —
    handle_state_change normally terminates first)."""
    zone = _run_state_zone("rainpoint", rain_behaviour="stop")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state(
            CONST_RAINING_STOP, warning_issued=False
        )
    lane.submit.assert_not_called()
    assert zone._stop is True


async def test_lane_zone_still_rearms_on_device_drop():
    """A raw device state (valve closed itself) must still re-arm."""
    zone = _run_state_zone("rainpoint")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state("closed", warning_issued=False)
    lane.submit.assert_called_once()
    assert lane.submit.call_args[0][2] == SERVICE_OPEN_VALVE
    assert zone._stop is False


async def test_non_optimistic_zone_stops_quietly_on_zone_disabled():
    """A generic zone disabled mid-run stops without the unexpected-state
    alarm — a deliberate disable is not an abnormal device state."""
    zone = _run_state_zone("Generic")
    with (
        patch("custom_components.irrigationprogram.zone.async_create") as notify,
        patch("custom_components.irrigationprogram.zone.async_dismiss"),
        _name_patch(),
    ):
        zone.entity_id = "switch.z1"
        await zone._handle_unexpected_run_state(
            CONST_ZONE_DISABLED, warning_issued=False
        )
    notify.assert_not_called()
    assert zone._stop is True
    assert zone._aborted is True


# --- handle_state_change: the designated mid-run stop gate -----------------


def _state_change_zone(status, rain_behaviour="stop", internal=CONST_ON):
    zone = _run_state_zone("rainpoint", rain_behaviour=rain_behaviour)
    zone._status = internal
    zone.get_status = AsyncMock(return_value=status)
    return zone


async def test_handle_state_change_stops_running_zone_when_zone_disabled():
    zone = _state_change_zone(CONST_ZONE_DISABLED)
    with _name_patch():
        zone.entity_id = "switch.z1"
        await zone.handle_state_change()
    assert zone._stop is True
    assert zone._aborted is True


async def test_handle_state_change_stops_running_zone_when_program_disabled():
    zone = _state_change_zone(CONST_PROGRAM_DISABLED)
    with _name_patch():
        zone.entity_id = "switch.z1"
        await zone.handle_state_change()
    assert zone._stop is True
    assert zone._aborted is True


async def test_handle_state_change_exempt_program_disabled_keeps_running():
    """A manual run on a disabled program must not stop itself."""
    zone = _state_change_zone(CONST_PROGRAM_DISABLED)
    zone._deliberate_stop_exempt = frozenset({CONST_PROGRAM_DISABLED})
    with _name_patch():
        zone.entity_id = "switch.z1"
        await zone.handle_state_change()
    assert zone._stop is False


async def test_handle_state_change_rain_finish_keeps_running():
    """With rain_behaviour 'finish', rain-stop mid-run completes the zone."""
    zone = _state_change_zone(CONST_RAINING_STOP, rain_behaviour="finish")
    with _name_patch():
        zone.entity_id = "switch.z1"
        await zone.handle_state_change()
    assert zone._stop is False
    assert zone._aborted is False


async def test_handle_state_change_rain_stop_still_terminates():
    """With rain_behaviour 'stop', rain-stop mid-run terminates (upstream)."""
    zone = _state_change_zone(CONST_RAINING_STOP, rain_behaviour="stop")
    with (
        patch("custom_components.irrigationprogram.zone.async_create"),
        patch("custom_components.irrigationprogram.zone.async_dismiss"),
        _name_patch(),
    ):
        zone.entity_id = "switch.z1"
        await zone.handle_state_change()
    assert zone._stop is True
    assert zone._aborted is True


async def test_handle_state_change_no_water_source_still_terminates():
    zone = _state_change_zone(CONST_NO_WATER_SOURCE)
    with (
        patch("custom_components.irrigationprogram.zone.async_create"),
        patch("custom_components.irrigationprogram.zone.async_dismiss"),
        _name_patch(),
    ):
        zone.entity_id = "switch.z1"
        await zone.handle_state_change()
    assert zone._stop is True
    assert zone._aborted is True


# --- run-start exemption capture -------------------------------------------


async def test_exemptions_captured_when_started_disabled():
    """Statuses already true at start are exempt for that run."""
    zone = _run_state_zone("rainpoint")
    enabled = MagicMock()
    enabled.state = "off"
    zone._programdata.enabled.is_on = False
    with patch.object(
        Zone, "enabled", new_callable=PropertyMock, return_value=enabled
    ):
        exempt = zone._capture_deliberate_stop_exemptions()
    assert CONST_ZONE_DISABLED in exempt
    assert CONST_PROGRAM_DISABLED in exempt


async def test_no_exemptions_captured_when_started_enabled():
    zone = _run_state_zone("rainpoint")
    enabled = MagicMock()
    enabled.state = "on"
    zone._programdata.enabled.is_on = True
    with patch.object(
        Zone, "enabled", new_callable=PropertyMock, return_value=enabled
    ):
        exempt = zone._capture_deliberate_stop_exemptions()
    assert exempt == frozenset()


# --- run-loop valid statuses (rain finish keeps device-drop protection) ----


async def test_valid_run_statuses_include_raining_stop_only_in_finish_mode():
    """In finish mode the run loop treats raining_stop as still-running, so
    the solenoid check keeps running and device drops still re-arm."""
    finish = _run_state_zone("rainpoint", rain_behaviour="finish")
    stop = _run_state_zone("rainpoint", rain_behaviour="stop")
    assert CONST_RAINING_STOP in finish._valid_run_statuses()
    assert CONST_RAINING_STOP not in stop._valid_run_statuses()
    assert CONST_ON in finish._valid_run_statuses()


# --- adjusted_off unchanged (pin the 2026-07-03 fix while refactoring) -----


async def test_adjusted_off_still_neither_stops_nor_rearms():
    zone = _run_state_zone("rainpoint")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        _name_patch(),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state(
            CONST_ADJUSTED_OFF, warning_issued=False
        )
    lane.submit.assert_not_called()
    assert zone._stop is False
