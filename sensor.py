"""C-Bus sensors: ambient light level (lux) from PIR units that carry a
photocell (e.g. 5751L / 5750WPL).

C-Gate exposes it as the unit parameter ``LightLevel`` (0-1600 lx), refreshed
on each parameter sync, so a slow poll is all that is needed.

Matt: like binary_sensor.py's motion sensor, this creates zero entities
until a PIR with a light-level sensor is discovered on your C-Bus network.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_UNIT, ATTR_UNIT_TYPE, DOMAIN, ROLE_PIR
from .coordinator import CBusCoordinator
from .entity import CBusLinkMixin

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=120)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CBusCoordinator = data["coordinator"]
    project = coordinator.project_name

    entities: List[CBusLightLevelSensor] = []

    for network_id, net_data in coordinator.discovery_model.items():
        for addr, unit in net_data.get("units", {}).items():
            if unit.get("role") != ROLE_PIR or not unit.get("has_light_level"):
                continue
            entities.append(
                CBusLightLevelSensor(coordinator, project, str(network_id), unit)
            )

    if entities:
        _LOGGER.info("Loaded %d C-Bus light-level sensors", len(entities))
        async_add_entities(entities, update_before_add=True)
    else:
        _LOGGER.info("No C-Bus light-level sensors found.")


class CBusLightLevelSensor(CBusLinkMixin, SensorEntity):
    _attr_should_poll = True
    _attr_has_entity_name = True
    _attr_name = "Light level"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = LIGHT_LUX

    def __init__(
        self,
        coordinator: CBusCoordinator,
        project: str,
        network: str,
        unit: Dict[str, Any],
    ) -> None:
        self.coordinator = coordinator
        self.project = project
        self.network = network
        self._unit = int(unit["address"])
        self._unit_type = unit.get("type")
        self._attr_native_value = None

        self._attr_unique_id = f"cbus_lux_{project}_{network}_p{self._unit}"
        self._attr_device_info = coordinator.device_info_for_unit(self._unit, network)

    async def async_added_to_hass(self) -> None:
        self._attach_link_listener()

    async def async_will_remove_from_hass(self) -> None:
        self._detach_link_listener()

    async def async_update(self) -> None:
        if not self.coordinator.link_ok:
            return
        try:
            raw = await self.coordinator.session.get_unit_param(
                self.project, self.network, self._unit, "LightLevel"
            )
            self._attr_native_value = int(float(raw)) if raw not in (None, "") else None
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("LightLevel read failed for unit %s: %s", self._unit, exc)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        return {ATTR_UNIT: self._unit, ATTR_UNIT_TYPE: self._unit_type}
