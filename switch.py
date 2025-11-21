# switch.py – corrected for new discovery model
import logging
from typing import Any, List

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CBusCoordinator = data["coordinator"]
    model = coordinator.discovery_model
    project = coordinator.project_name

    entities: List[CBusSwitch] = []

    for network_id, network_data in model.items():

        apps = network_data.get("applications", {})
        app56 = apps.get("56")
        if not app56:
            continue

        for group_id, group_info in app56.get("groups", {}).items():

            if not group_info.get("is_load"):
                continue

            if group_info.get("device_class") != "switch":
                continue

            name = group_info.get("name")

            entities.append(
                CBusSwitch(
                    coordinator,
                    project,
                    str(network_id),
                    56,
                    int(group_id),
                    name
                )
            )

    if not entities:
        _LOGGER.info("No C-Bus switches found.")
        return

    _LOGGER.info("Loaded %d C-Bus switch entities", len(entities))
    async_add_entities(entities)


class CBusSwitch(SwitchEntity):
    _attr_should_poll = False

    def __init__(self, coordinator, project, network, app, group, name):
        self.coordinator = coordinator
        self.project = project
        self.network = network
        self._app = int(app)
        self._group = int(group)

        self._attr_name = name
        self._attr_unique_id = (
            f"cbus_switch_{project}_{network}_{app}_{group}"
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
    def is_on(self):
        key = (self.project, self.network, self._app, self._group)
        return self.coordinator.group_levels.get(key, 0) > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 255
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 0
        )
