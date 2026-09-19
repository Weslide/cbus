# switch.py – relays and non-light loads
import logging
from typing import Any, List

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CBusCoordinator
from .entity import CBusLinkMixin

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CBusCoordinator = data["coordinator"]
    project = coordinator.project_name

    entities: List[CBusSwitch] = []

    for network_id, app_id, gid, g in coordinator.lighting_groups():
        if not g.get("is_load"):
            continue
        if g.get("device_class") not in ("switch", "exhaust"):
            continue

        entities.append(
            CBusSwitch(
                coordinator,
                project,
                network_id,
                app_id,
                gid,
                g["name"],
                g["device_class"],
                enabled_default=bool(g.get("enabled_default", True)),
            )
        )

    if entities:
        _LOGGER.info("Loaded %d C-Bus switch entities", len(entities))
        async_add_entities(entities)
    else:
        _LOGGER.info("No C-Bus switches found.")


class CBusSwitch(CBusLinkMixin, SwitchEntity):

    _attr_should_poll = False

    def __init__(self, coord, project, network, app, group, name, device_class, enabled_default: bool = True):
        self.coordinator = coord
        self._attr_entity_registry_enabled_default = enabled_default
        self.project = project
        self.network = network
        self._app = int(app)
        self._group = int(group)
        self._attr_name = name
        self._device_class = device_class

        self._attr_unique_id = f"cbus_switch_{project}_{network}_{app}_{group}"
        self._attr_device_info = coord.device_info_for_group(app, group, network)

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
        except Exception:
            pass

        self.coordinator.register_callback(self._app, self._group, self._update)
        self._attach_link_listener()
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self):
        self.coordinator.unregister_callback(self._app, self._group, self._update)
        self._detach_link_listener()

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
        self.coordinator.handle_group_update(
            self.project, self.network, self._app, self._group, 255
        )

    async def async_turn_off(self, **kwargs: Any):
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 0
        )
        self.coordinator.handle_group_update(
            self.project, self.network, self._app, self._group, 0
        )
