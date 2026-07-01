"""Tests for the per-zone optimistic (fire-once) primitive.

An optimistic zone fires its solenoid command once and trusts it, letting the
program timer govern the run instead of confirming/​re-firing the switch state.
"""

from unittest.mock import AsyncMock, MagicMock

from custom_components.irrigationprogram.zone import Zone


def _bare_zone(optimistic):
    """Build a Zone bypassing __init__, with only what the short-circuits need."""
    zone = Zone.__new__(Zone)
    zone._programdata = MagicMock()
    zone._programdata.controller_type = "Generic"
    zone._zonedata = MagicMock()
    zone._zonedata.optimistic = optimistic
    zone.async_solenoid_turn_on = AsyncMock()
    zone.async_solenoid_turn_off = AsyncMock()
    zone._latency = 1
    zone._aborted = False
    zone._status = "on"
    zone.hass = MagicMock()
    zone.name = "zone1"
    zone.entity_id = "switch.irrigation_zone1"
    zone._scheduled = False
    return zone


async def test_optimistic_property_reads_zonedata():
    """The optimistic property passes through IrrigationZoneData.optimistic."""
    zone = Zone.__new__(Zone)
    zone._programdata = MagicMock()
    zone._programdata.controller_type = "Generic"
    zone._zonedata = MagicMock()
    zone._zonedata.optimistic = True
    assert zone.optimistic is True
    zone._zonedata.optimistic = False
    assert zone.optimistic is False


async def test_check_is_on_optimistic_fires_once_without_confirming():
    """Optimistic: fire the turn-on once and return; never poll/re-fire."""
    zone = _bare_zone(optimistic=True)
    # State would never confirm 'on' -> a non-optimistic zone would re-fire.
    zone.check_switch_state = AsyncMock(return_value=(False, "off"))
    await zone.check_is_on()
    zone.async_solenoid_turn_on.assert_awaited_once()
    zone.check_switch_state.assert_not_awaited()


async def test_check_is_off_optimistic_returns_without_confirming():
    """Optimistic: skip the off-state confirmation loop."""
    zone = _bare_zone(optimistic=True)
    # State would never confirm 'off' -> a non-optimistic zone would re-fire.
    zone.check_switch_state = AsyncMock(return_value=(True, "on"))
    await zone.check_is_off()
    zone.check_switch_state.assert_not_awaited()


async def test_check_is_on_non_optimistic_confirms_state():
    """Non-optimistic still polls to confirm the switch turned on."""
    zone = _bare_zone(optimistic=False)
    zone.check_switch_state = AsyncMock(return_value=(True, "on"))
    await zone.check_is_on()
    zone.async_solenoid_turn_on.assert_awaited()
    zone.check_switch_state.assert_awaited()
