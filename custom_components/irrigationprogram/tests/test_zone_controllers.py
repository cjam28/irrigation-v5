"""Tests for the cloud controller categories (F5 rainpoint, F6 hydrawise)."""

from unittest.mock import AsyncMock, MagicMock, patch

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
