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


async def test_hydrawise_close_issued_despite_stale_closed_state():
    """A lagging 'closed' state must not skip the close at timer end.

    Hydrawise local state refreshes on cloud polls; skipping the close on
    stale state would leave the run to the device's own timer only.
    """
    zone = _full_hydrawise_zone(state=(False, "closed"))  # stale/lagging
    with patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"):
        await zone.async_solenoid_turn_off()
    zone.hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_CLOSE_VALVE, {ATTR_ENTITY_ID: "valve.z1"}
    )


async def test_hydrawise_reopen_issued_despite_stale_open_state():
    """A repeat's re-open must fire even while stale state still reads open."""
    zone = _full_hydrawise_zone(state=(True, "open"))  # stale from last rep
    registry = MagicMock()
    registry.async_get.return_value = MagicMock(device_id="dev1")
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
            return_value=[sensor_entry],
        ),
    ):
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_awaited_once_with(
        "hydrawise",
        "start_watering",
        {ATTR_ENTITY_ID: "binary_sensor.z1_watering", "duration": 3},
    )


async def test_generic_optimistic_zone_close_not_gated_by_state():
    """An F4 optimistic zone also closes regardless of the local state."""
    zone = _full_hydrawise_zone(state=(False, "closed"))
    zone._programdata.controller_type = "Generic"
    zone._zonedata.optimistic = True
    with patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"):
        await zone.async_solenoid_turn_off()
    zone.hass.services.async_call.assert_awaited_once_with(
        "valve", SERVICE_CLOSE_VALVE, {ATTR_ENTITY_ID: "valve.z1"}
    )


async def test_non_optimistic_zone_close_skipped_when_state_reads_closed():
    """A non-optimistic zone keeps trusting its state and skips the close."""
    zone = _full_hydrawise_zone(state=(False, "closed"))
    zone._programdata.controller_type = "Generic"
    zone._zonedata.optimistic = False
    await zone.async_solenoid_turn_off()
    zone.hass.services.async_call.assert_not_awaited()


async def test_restart_ensure_off_skipped_when_state_reads_closed():
    """The restart safety-close must stay state-gated.

    With the optimistic bypass, every zone would queue a close on the shared
    lane at every HA restart, jamming it for zones x min_spacing and delaying
    real commands by minutes (observed live: an open delivered 2.5 minutes
    after its run timer had already expired).
    """
    zone = _full_rainpoint_zone(state=(False, "closed"))
    with patch("custom_components.irrigationprogram.zone.get_lane") as get_lane:
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._async_ensure_off_after_restart()
    lane.submit.assert_not_called()


async def test_restart_ensure_off_closes_when_state_reads_open():
    """A zone genuinely left open (or unknown) at restart is still closed."""
    for state in ((True, "open"), (None, "unknown")):
        zone = _full_rainpoint_zone(state=state)
        with (
            patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
            patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"),
        ):
            lane = MagicMock()
            get_lane.return_value = lane
            await zone._async_ensure_off_after_restart()
        lane.submit.assert_called_once()
        assert lane.submit.call_args[0][2] == SERVICE_CLOSE_VALVE


async def test_optimistic_zone_does_not_repeat_the_close():
    """Teardown paths call turn_off more than once; only one close is issued.

    The program end fires async_solenoid_turn_off from both the run-loop
    unwind and the cleanup path; commands are idempotent but each duplicate
    burns a cloud/lane slot (observed live: double close per zone end).
    """
    zone = _full_hydrawise_zone(state=(False, "closed"))
    with patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"):
        await zone.async_solenoid_turn_off()
        await zone.async_solenoid_turn_off()
    assert zone.hass.services.async_call.await_count == 1


async def test_optimistic_zone_reopen_after_close_still_fires():
    """Open→close→open alternation (eco repeats) is never suppressed."""
    zone = _full_hydrawise_zone(state=(False, "closed"))
    registry = MagicMock()
    registry.async_get.return_value = MagicMock(device_id="dev1")
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
            return_value=[sensor_entry],
        ),
    ):
        await zone.async_solenoid_turn_on()
        await zone.async_solenoid_turn_off()
        await zone.async_solenoid_turn_on()
        await zone.async_solenoid_turn_off()
    services = [c.args[1] for c in zone.hass.services.async_call.await_args_list]
    assert services == [
        "start_watering",
        SERVICE_CLOSE_VALVE,
        "start_watering",
        SERVICE_CLOSE_VALVE,
    ]


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


def _rainpoint_registry(with_number=True):
    """Mock entity registry: rainpoint valve + sibling homgar duration number."""
    registry = MagicMock()
    valve_entry = MagicMock(device_id="dev1", platform="homgar")
    registry.async_get.return_value = valve_entry
    entries = []
    if with_number:
        number_entry = MagicMock(
            domain="number",
            platform="homgar",
            entity_id="number.z1_duration",
        )
        entries.append(number_entry)
    other = MagicMock(domain="sensor", platform="homgar", entity_id="sensor.z1_x")
    entries.append(other)
    return registry, entries


async def test_rainpoint_sets_device_duration_before_open():
    """The valve's own run-duration is set to cover the computed run.

    RainPoint valves self-close after their per-valve duration; shorter than
    the V5 run means silent under-watering (observed live: 75-min runs cut at
    the valve's 60/40-min settings).
    """
    zone = _full_rainpoint_zone(state=(False, "closed"))
    zone._zonedata.repeat = None
    zone._scheduled = False
    zone.calc_run_time = AsyncMock(return_value=19 * 60)  # 19 min run
    zone.hass.services.async_call = AsyncMock()
    dur_state = MagicMock()
    dur_state.state = "60.0"
    zone.hass.states.get.return_value = dur_state
    registry, entries = _rainpoint_registry()
    name_p, water_p, wait_p, repeat_p = _zone_property_patches()
    with (
        name_p, water_p, wait_p, repeat_p,
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=registry,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=entries,
        ),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_awaited_once_with(
        "number", "set_value", {ATTR_ENTITY_ID: "number.z1_duration", "value": 20}
    )
    lane.submit.assert_called_once()


async def test_rainpoint_duration_set_skipped_when_already_right():
    """No cloud call when the valve's duration already matches the target."""
    zone = _full_rainpoint_zone(state=(False, "closed"))
    zone._zonedata.repeat = None
    zone._scheduled = False
    zone.calc_run_time = AsyncMock(return_value=19 * 60)
    zone.hass.services.async_call = AsyncMock()
    dur_state = MagicMock()
    dur_state.state = "20.0"  # already the target
    zone.hass.states.get.return_value = dur_state
    registry, entries = _rainpoint_registry()
    name_p, water_p, wait_p, repeat_p = _zone_property_patches()
    with (
        name_p, water_p, wait_p, repeat_p,
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=registry,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=entries,
        ),
    ):
        get_lane.return_value = MagicMock()
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_not_awaited()


async def test_rainpoint_duration_clamped_to_device_max():
    """Durations above the 60-minute device maximum are clamped, not sent.

    The devices reject opens with duration > 60 min outright (observed live:
    such opens are logged as bare 'Closed()' and nothing waters).
    """
    zone = _full_rainpoint_zone(state=(False, "closed"))
    zone._zonedata.repeat = None
    zone._scheduled = False
    zone.calc_run_time = AsyncMock(return_value=90 * 60)  # 90-min run
    zone.hass.services.async_call = AsyncMock()
    dur_state = MagicMock()
    dur_state.state = "40.0"
    zone.hass.states.get.return_value = dur_state
    registry, entries = _rainpoint_registry()
    name_p, water_p, wait_p, repeat_p = _zone_property_patches()
    with (
        name_p, water_p, wait_p, repeat_p,
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=registry,
        ),
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_device",
            return_value=entries,
        ),
    ):
        get_lane.return_value = MagicMock()
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_awaited_once_with(
        "number", "set_value", {ATTR_ENTITY_ID: "number.z1_duration", "value": 60}
    )


async def test_rainpoint_open_submitted_without_duration_number():
    """A rainpoint valve with no resolvable duration number still opens."""
    zone = _full_rainpoint_zone(state=(False, "closed"))
    zone.hass.services.async_call = AsyncMock()
    registry, _ = _rainpoint_registry(with_number=False)
    registry.async_get.return_value = None  # not even in the registry
    name_p, water_p, wait_p, repeat_p = _zone_property_patches()
    with (
        name_p, water_p, wait_p, repeat_p,
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch(
            "homeassistant.helpers.entity_registry.async_get",
            return_value=registry,
        ),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone.async_solenoid_turn_on()
    zone.hass.services.async_call.assert_not_awaited()
    lane.submit.assert_called_once()


async def test_rainpoint_valve_confirms_against_device_truth():
    """Valve commands confirm on the device-truth attribute, not the state.

    homgar sets the state optimistically on command, so state=='open' proves
    nothing (observed live: a dead hub 'watered' on paper for 75 minutes).
    """
    zone = _controller_zone("rainpoint", entity_type="valve")
    with patch("custom_components.irrigationprogram.zone.get_lane") as get_lane:
        lane = MagicMock()
        get_lane.return_value = lane
        zone._submit_cloud_command(opening=True)
        open_kwargs = lane.submit.call_args.kwargs
        open_expected = lane.submit.call_args[0][3]
        zone._submit_cloud_command(opening=False)
        close_kwargs = lane.submit.call_args.kwargs
        close_expected = lane.submit.call_args[0][3]
    assert open_kwargs["confirm_attr"] == "valve_state"
    assert "irrigation" in open_expected
    assert close_kwargs["confirm_attr"] == "valve_state"
    assert "idle" in close_expected


# --- re-arm: device/hub closed the valve while the run timer owns it -------

def _unexpected_state_zone(controller, optimistic=False):
    zone = _controller_zone(controller, entity_type="valve", zone_entity="valve.z1")
    zone._zonedata.optimistic = optimistic
    zone._scheduled = False
    zone._latency = 5
    zone._continue_on_unexpected_state = False
    zone.hass.bus.async_fire = MagicMock()
    zone.async_turn_off_zone_natural = AsyncMock()
    return zone


async def test_lane_zone_rearms_when_device_closed_mid_run():
    """A lane zone whose device closed mid-run re-issues the open."""
    zone = _unexpected_state_zone("rainpoint")
    with (
        patch("custom_components.irrigationprogram.zone.get_lane") as get_lane,
        patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"),
    ):
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state("closed", warning_issued=False)
    lane.submit.assert_called_once()
    assert lane.submit.call_args[0][2] == SERVICE_OPEN_VALVE
    zone.async_turn_off_zone_natural.assert_not_awaited()


async def test_optimistic_non_lane_zone_ignores_unexpected_state():
    """Optimistic non-lane zones (hydrawise) neither notify nor terminate."""
    zone = _unexpected_state_zone("hydrawise")
    with patch("custom_components.irrigationprogram.zone.get_lane") as get_lane:
        lane = MagicMock()
        get_lane.return_value = lane
        await zone._handle_unexpected_run_state("closed", warning_issued=False)
    lane.submit.assert_not_called()
    zone.async_turn_off_zone_natural.assert_not_awaited()
    zone.hass.bus.async_fire.assert_not_called()


async def test_non_optimistic_zone_keeps_notify_and_terminate():
    """Non-optimistic zones keep the upstream warn/terminate behaviour."""
    zone = _unexpected_state_zone("Generic")
    with (
        patch("custom_components.irrigationprogram.zone.async_create") as notify,
        patch("custom_components.irrigationprogram.zone.async_dismiss"),
        patch.object(Zone, "name", new_callable=PropertyMock, return_value="z1"),
    ):
        zone.entity_id = "switch.z1"
        result = await zone._handle_unexpected_run_state("off", warning_issued=False)
    notify.assert_called_once()
    zone.hass.bus.async_fire.assert_called_once()
    zone.async_turn_off_zone_natural.assert_awaited_once()
    assert result is True


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
