"""pump classs."""

import asyncio
import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_VALVE,
    SERVICE_OPEN_VALVE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant

from .const import (
    CONST_CLOSED,
    CONST_OFF,
    CONST_OFF_DELAY,
    CONST_ON,
    CONST_OPEN,
    CONST_SWITCH,
    CONST_VALVE,
)

_LOGGER = logging.getLogger(__name__)

# hass.data key: pump entity_id -> [PumpClass, ...] across ALL config entries.
# Two programs may declare the same physical pump/master; a program finishing
# must not close it while another program that declares it is still running.
PUMP_REGISTRY = "irrigationprogram_shared_pumps"


class PumpClass:
    """Pump class."""

    def __init__(self, hass: HomeAssistant, pump, zones, program=None) -> None:  # noqa: D107
        self.hass = hass
        self._pump = pump
        # a valve-domain entity uses open/close services; anything else
        # (switch, input_boolean, …) uses turn_on/turn_off
        self._valve = str(pump).split(".")[0] == CONST_VALVE
        self._zones = zones
        self._off_delay = CONST_OFF_DELAY
        self._program = program
        self._cancel = None

        registry = hass.data.setdefault(PUMP_REGISTRY, {})
        if isinstance(registry, dict):
            registry.setdefault(pump, []).append(self)

        # turn off the pump on start
        hass.async_create_task(self.async_stop())

        self._cancel = hass.bus.async_listen("irrigation_event", self.handle_event)

    def detach(self):
        """Stop listening and leave the shared-pump registry (program unload)."""
        if self._cancel:
            self._cancel()
            self._cancel = None
        registry = self.hass.data.get(PUMP_REGISTRY)
        if isinstance(registry, dict):
            peers = registry.get(self._pump, [])
            if self in peers:
                peers.remove(self)

    def _other_program_holds_pump(self) -> bool:
        """True while another program sharing this pump entity is running.

        Checked against the other PROGRAM's switch, not its zone entities:
        programs sharing a pump can alias the same physical zone entities,
        so the closing program's own winding-down zones would read as the
        other program still needing the pump.
        """
        registry = self.hass.data.get(PUMP_REGISTRY)
        if not isinstance(registry, dict):
            return False
        for peer in registry.get(self._pump, []):
            if peer is self or peer._program is None:
                continue
            state = self.hass.states.get(peer._program.entity_id)
            if state and state.state == CONST_ON:
                return True
        return False

    async def handle_event(self, event):
        """Inspect irrigation events."""

        if self._program and self._program.entity_id != event.data.get("program"):
            return

        if event.data.get("action") == "turn_on_pump":
            delay = int(event.data.get("delay"))
            await asyncio.sleep(delay)
            await self.async_start()

        if event.data.get("action") == "turn_off_pump_all":
            if not self._other_program_holds_pump():
                await self.async_stop()

        if event.data.get("action") == "turn_off_pump":
            # Now need to determine if other zones are running that
            # need the pump to remain on.
            for zone in self._zones:
                state = self.hass.states.get(zone.zone)
                if state and state.state in (
                    CONST_ON,
                    CONST_OPEN,
                ) and zone.zone != event.data.get("device_id"):
                    break
            else:
                if not self._other_program_holds_pump():
                    await self.async_stop()

    @property
    def zones(self) -> list:
        """Return list of zones."""
        return self._zones

    @property
    def pump(self) -> list:
        """Return pump."""
        return self._pump

    async def async_stop(self):
        """Turn off pump."""
        state = self.hass.states.get(self._pump)
        if self._valve:
            # close unless already confirmed closed (tolerates unknown/lagging state)
            if state and state.state != CONST_CLOSED:
                await self.hass.services.async_call(
                    CONST_VALVE, SERVICE_CLOSE_VALVE, {ATTR_ENTITY_ID: self._pump}
                )
        elif state and state.state != CONST_OFF:
            await self.hass.services.async_call(
                CONST_SWITCH, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: self._pump}
            )

    async def async_start(self):
        """Turn on the pump."""
        state = self.hass.states.get(self._pump)
        if self._valve:
            # open unless already confirmed open (tolerates unknown/lagging state)
            if state and state.state != CONST_OPEN:
                await self.hass.services.async_call(
                    CONST_VALVE, SERVICE_OPEN_VALVE, {ATTR_ENTITY_ID: self._pump}
                )
        elif state and state.state != CONST_ON:
            await self.hass.services.async_call(
                CONST_SWITCH, SERVICE_TURN_ON, {ATTR_ENTITY_ID: self._pump}
            )
