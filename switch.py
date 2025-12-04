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

    for network_id, net_data in model.items():
        apps = net_data.get("applications", {})
        app56 = apps.get("56")
        if not app56:
            continue

        for gid, g in app56["groups"].items():

            if not g.get("is_load"):
                continue

            if g.get("device_class") != "switch" and g.get("device_class") != "exhaust":
                continue

            e = CBusSwitch(
                coordinator,
                project,
                str(network_id),
                56,
                int(gid),
                g["name"],
                g["device_class"],
            )
            entities.append(e)

    if entities:
        async_add_entities(entities)
    else:
        _LOGGER.info("No C-Bus switches found.")


class CBusSwitch(SwitchEntity):

    _attr_should_poll = False

    def __init__(self, coord, project, network, app, group, name, device_class):
        self.coordinator = coord
        self.project = project
        self.network = network
        self._app = int(app)
        self._group = int(group)
        self._attr_name = name
        self._device_class = device_class

        self._attr_unique_id = f"cbus_switch_{project}_{network}_{app}_{group}"

        # Icon override for exhaust fans
        if device_class == "exhaust":
            self._attr_icon = "mdi:exhaust-fan"

    async def async_added_to_hass(self):
        key = (self.project, self.network, self._app, self._group)

        try:
            lvl = await self.coordinator.session.get_group_level(
                self.project, self.network, self._app, self._group
            )
            if lvl is not None:
                self.coordinator.group_levels[key] = lvl
        except:
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

    async def async_turn_on(self, **kwargs: Any):
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 255
        )

    async def async_turn_off(self, **kwargs: Any):
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 0
        )
