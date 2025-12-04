# fan.py – corrected for new discovery model
import logging
from typing import Any, List, Optional

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)

_SPEED_STEPS = [0, 33, 66, 100]


def _nearest_step(level):
    if level <= 0:
        return 0
    if level <= 85:
        return 33
    if level <= 170:
        return 66
    return 100


def _step_to_level(step):
    if step <= 0:
        return 0
    if step <= 33:
        return 85
    if step <= 66:
        return 170
    return 255


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CBusCoordinator = data["coordinator"]
    model = coordinator.discovery_model
    project = coordinator.project_name

    entities: List[CBusFan] = []

    for network_id, network_data in model.items():

        apps = network_data.get("applications", {})
        app56 = apps.get("56")

        if not app56:
            continue

        for group_id, group_info in app56.get("groups", {}).items():

            if not group_info.get("is_load"):
                continue

            if group_info.get("device_class") != "fan":
                continue

            entities.append(
                CBusFan(
                    coordinator,
                    project,
                    str(network_id),
                    56,
                    int(group_id),
                    group_info["name"]
                )
            )

    if not entities:
        _LOGGER.info("No C-Bus fans found.")
        return

    _LOGGER.info("Loaded %d C-Bus fan entities", len(entities))
    async_add_entities(entities)


class CBusFan(FanEntity):

    _attr_should_poll = False
    #_attr_supported_features = FanEntityFeature.SET_PERCENTAGE
    _attr_supported_features = FanEntityFeature.SET_SPEED


    def __init__(self, coordinator, project, network, app, group, name):
        self.coordinator = coordinator
        self.project = project
        self.network = network
        self._app = int(app)
        self._group = int(group)

        self._attr_name = name
        self._attr_unique_id = (
            f"cbus_fan_{project}_{network}_{app}_{group}"
        )

    async def async_added_to_hass(self):
        key = (self.project, self.network, self._app, self._group)

        try:
            lvl = await self.coordinator.session.get_group_level(
                self.project, self.network, self._app, self._group
            )
            if lvl is not None:
                self.coordinator.group_levels[key] = int(lvl)
        except Exception:
            pass

        self.coordinator.register_callback(self._app, self._group, self._update)
        self.async_write_ha_state()

    def _update(self, level: int):
        key = (self.project, self.network, self._app, self._group)
        self.coordinator.group_levels[key] = level
        self.async_write_ha_state()

    @property
    def _current_level(self):
        key = (self.project, self.network, self._app, self._group)
        return self.coordinator.group_levels.get(key, 0)

    @property
    def percentage(self) -> Optional[int]:
        return _nearest_step(self._current_level)

    @property
    def is_on(self):
        return self._current_level > 0

    async def async_turn_on(self, **kwargs: Any):
        pct = kwargs.get("percentage", 66)
        await self.async_set_percentage(pct)

    async def async_turn_off(self, **kwargs: Any):
        await self.async_set_percentage(0)

    async def async_set_percentage(self, pct: int):
        step = min(_SPEED_STEPS, key=lambda s: abs(s - pct))
        level = _step_to_level(step)

        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, level
        )
