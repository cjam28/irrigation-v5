"""Tests for the cloud controller categories (F5 rainpoint, F6 hydrawise)."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)

from custom_components.irrigationprogram.zone import Zone


def _controller_zone(controller_type, entity_type="valve", zone_entity="valve.z1"):
    """Bare Zone with just the attributes the solenoid dispatch reads."""
    zone = Zone.__new__(Zone)
    zone._programdata = MagicMock()
    zone._programdata.controller_type = controller_type
    zone._zonedata = MagicMock()
    zone._zonedata.type = entity_type
    zone._zonedata.zone = zone_entity
    zone._zonedata.optimistic = False
    zone.hass = MagicMock()
    return zone


async def test_rainpoint_optimistic_auto_implied():
    """A rainpoint zone is optimistic regardless of its per-zone flag."""
    zone = _controller_zone("rainpoint")
    assert zone.optimistic is True


async def test_non_cloud_controller_not_optimistic_by_default():
    """A generic zone is not optimistic unless explicitly flagged."""
    zone = _controller_zone("Generic")
    assert zone.optimistic is False


async def test_uses_command_lane_only_for_rainpoint():
    """Only the lane-backed cloud controller has non-authoritative local state."""
    assert _controller_zone("rainpoint").uses_command_lane is True
    assert _controller_zone("hydrawise").uses_command_lane is False
    assert _controller_zone("Generic").uses_command_lane is False


def _full_rainpoint_zone(state):
    """A rainpoint zone runnable through the full turn on/off path."""
    zone = _controller_zone("rainpoint", entity_type="valve", zone_entity="valve.z1")
    zone._zonedata.name = "z1"
    zone._pump = None  # skip the pump prelude in async_solenoid_turn_on
    zone._scheduled = False
    zone._state = "open"
    zone._remaining_time = 0
    zone.hass.bus.async_fire = MagicMock()
    zone.check_switch_state = AsyncMock(return_value=state)
    return zone


async def test_rainpoint_close_submitted_when_local_state_reads_closed():
    """The rainpoint close must be queued even if HA state lags to 'closed'.

    Regression: the early-return on local state would leave the valve open.
    """
    zone = _full_rainpoint_zone(state=(False, "closed"))  # stale/lagging closed
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone.async_solenoid_turn_off()
    lane.submit.assert_called_once()
    assert lane.submit.call_args[0][2] == SERVICE_CLOSE_VALVE


async def test_rainpoint_open_submitted_when_local_state_reads_open():
    """The rainpoint open must be queued even if HA state lags to 'open'.

    Regression: the state gate would suppress a repeat's open and a pending
    close would then shut the valve mid-repeat.
    """
    zone = _full_rainpoint_zone(state=(True, "open"))  # stale/lagging open
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"),
        patch.object(Zone, "water", new_callable=PropertyMock, return_value=10),
        patch.object(Zone, "wait", new_callable=PropertyMock, return_value=0),
        patch.object(Zone, "repeat", new_callable=PropertyMock, return_value=1),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone.async_solenoid_turn_on()
    lane.submit.assert_called_once()
    assert lane.submit.call_args[0][2] == SERVICE_OPEN_VALVE


async def test_submit_cloud_command_opens_valve_via_lane():
    """Opening a rainpoint valve queues an open on the controller's lane."""
    zone = _controller_zone("rainpoint", entity_type="valve")
    with patch("custom_components.irrigationprogram.zone.get_lane") as get_lane:
        lane = MagicMock()
        get_lane.return_value = lane
        zone._submit_cloud_command(opening=True)
    get_lane.assert_called_once_with(zone.hass, "rainpoint")
    lane.submit.assert_called_once()
    entity, domain, service, expected = lane.submit.call_args[0]
    assert entity == "valve.z1"
    assert domain == "valve"
    assert service == SERVICE_OPEN_VALVE
    assert "open" in expected


async def test_submit_cloud_command_closes_valve_via_lane():
    """Closing a rainpoint valve queues a close on the controller's lane."""
    zone = _controller_zone("rainpoint", entity_type="valve")
    with patch("custom_components.irrigationprogram.zone.get_lane") as get_lane:
        lane = MagicMock()
        get_lane.return_value = lane
        zone._submit_cloud_command(opening=False)
    entity, domain, service, expected = lane.submit.call_args[0]
    assert domain == "valve"
    assert service == SERVICE_CLOSE_VALVE
    assert "closed" in expected


async def test_hydrawise_optimistic_auto_implied():
    """A hydrawise zone is optimistic regardless of its per-zone flag."""
    zone = _controller_zone("hydrawise")
    assert zone.optimistic is True


async def test_duration_controller_passes_run_minutes():
    """A duration-based controller receives the V5 run time in whole minutes."""
    zone = _controller_zone("hydrawise")
    zone._zonedata.repeat = None  # -> repeat property returns 1
    zone._scheduled = False
    zone.calc_run_time = AsyncMock(return_value=125)  # seconds
    zone.hass.services.async_call = AsyncMock()
    await zone._call_duration_controller("hydrawise", "start_watering", "duration")
    zone.hass.services.async_call.assert_awaited_once_with(
        "hydrawise",
        "start_watering",
        {ATTR_ENTITY_ID: "valve.z1", "duration": 3},  # ceil(125 / 60)
    )


def _full_hydrawise_zone(state):
    """A hydrawise zone runnable through the full turn-on path."""
    zone = _controller_zone("hydrawise", entity_type="valve", zone_entity="valve.z1")
    zone._zonedata.name = "z1"
    zone._zonedata.repeat = None
    zone._pump = None  # skip the pump prelude in async_solenoid_turn_on
    zone._scheduled = False
    zone._state = "open"
    zone._remaining_time = 0
    zone.hass.bus.async_fire = MagicMock()
    zone.hass.services.async_call = AsyncMock()
    zone.check_switch_state = AsyncMock(return_value=state)
    zone.calc_run_time = AsyncMock(return_value=125)  # -> duration 3 minutes
    return zone


def _zone_property_patches():
    """The Zone properties async_solenoid_turn_on reads for its event data."""
    return (
        patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"),
        patch.object(Zone, "water", new_callable=PropertyMock, return_value=10),
        patch.object(Zone, "wait", new_callable=PropertyMock, return_value=0),
        patch.object(Zone, "repeat", new_callable=PropertyMock, return_value=1),
    )


async def test_hydrawise_start_watering_targets_watering_sensor():
    """hydrawise.start_watering must target the zone's watering binary_sensor.

    The real HA service is registered on the binary_sensor platform
    (device_class 'running'); the configured valve entity is not a valid
    target, so the zone resolves the sibling sensor on the same device.
    """
    zone = _full_hydrawise_zone(state=(False, "closed"))
    registry = MagicMock()
    registry.async_get.return_value = MagicMock(device_id="dev1")
    valve_entry = MagicMock(domain="valve", platform="hydrawise")
    sensor_entry = MagicMock(
        domain="binary_sensor",
        platform="hydrawise",
        device_class=None,
        original_device_class="running",
        entity_id="binary_sensor.z1_watering",
    )
    name_p, water_p, wait_p, repeat_p = _zone_property_patches()
    with (
        name_p, water_p, wait_p, repeat_p,
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=registry,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=[valve_entry, sensor_entry],
        ),
    ):
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_awaited_once_with(
        "hydrawise",
        "start_watering",
        {ATTR_ENTITY_ID: "binary_sensor.z1_watering", "duration": 3},
    )


async def test_hydrawise_falls_back_to_open_valve_without_watering_sensor():
    """Without a resolvable watering sensor the zone opens its valve directly.

    The device then runs its app-default duration; the program timer still
    owns the close via valve.close_valve.
    """
    zone = _full_hydrawise_zone(state=(False, "closed"))
    registry = MagicMock()
    registry.async_get.return_value = None  # solenoid not in the registry
    name_p, water_p, wait_p, repeat_p = _zone_property_patches()
    with (
        name_p, water_p, wait_p, repeat_p,
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=registry,
        ),
    ):
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_OPEN_VALVE, {ATTR_ENTITY_ID: "valve.z1"}
    )


async def test_submit_cloud_command_switch_entity_uses_turn_on_off():
    """A switch-type cloud entity uses turn_on/turn_off, not open/close."""
    zone = _controller_zone("rainpoint", entity_type="switch", zone_entity="switch.z1")
    with patch("custom_components.irrigationprogram.zone.get_lane") as get_lane:
        lane = MagicMock()
        get_lane.return_value = lane
        zone._submit_cloud_command(opening=True)
        on_service = lane.submit.call_args[0][2]
        zone._submit_cloud_command(opening=False)
        off_service = lane.submit.call_args[0][2]
    assert on_service == SERVICE_TURN_ON
    assert off_service == SERVICE_TURN_OFF
