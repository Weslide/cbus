# light.py for Home Assistant C-Bus Integration
import logging
from typing import List

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CBusCoordinator = data["coordinator"]
    model = coordinator.discovery_model
    project = coordinator.project_name

    entities: List[CBusLight] = []

    for network_id, network_data in model.items():
        apps = network_data.get("applications", {})
        app56 = apps.get("56")
        if not app56:
            continue

        for group_id, group_info in app56.get("groups", {}).items():
            if not group_info.get("is_load", True):
                continue
            if group_info.get("device_class") != "light":
                continue

            name = group_info.get("name", f"C-Bus {group_id}")

            entities.append(
                CBusLight(
                    coordinator=coordinator,
                    project=project,
                    network=str(network_id),
                    app=56,
                    group=int(group_id),
                    name=name,
                )
            )

    if not entities:
        _LOGGER.info("No C-Bus lights found.")
        return

    _LOGGER.info("Loaded %d C-Bus light entities", len(entities))
    async_add_entities(entities)


class CBusLight(LightEntity):
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, coordinator: CBusCoordinator, project: str, network: str, app: int, group: int, name: str):
        self.coordinator = coordinator
        self.project = project
        self.network = network
        self._app = int(app)
        self._group = int(group)

        self._attr_name = name
        self._attr_unique_id = f"cbus_light_{project}_{network}_{app}_{group}"

    async def async_added_to_hass(self) -> None:
        key = (self.project, self.network, self._app, self._group)

        try:
            lvl = await self.coordinator.session.get_group_level(
                self.project, self.network, self._app, self._group
            )
            if lvl is not None:
                self.coordinator.group_levels[key] = int(lvl)
        except Exception:
            pass

        self.coordinator.register_callback(self._app, self._group, self._level_update)
        self.async_write_ha_state()

    def _level_update(self, level: int) -> None:
        key = (self.project, self.network, self._app, self._group)
        self.coordinator.group_levels[key] = int(level)
        self.async_write_ha_state()

    @property
    def _current_level(self) -> int:
        key = (self.project, self.network, self._app, self._group)
        return int(self.coordinator.group_levels.get(key, 0))

    @property
    def is_on(self) -> bool:
        return self._current_level > 0

    @property
    def brightness(self):
        lvl = self._current_level
        return lvl if lvl > 0 else None

    async def async_turn_on(self, **kwargs):
        brightness = int(kwargs.get(ATTR_BRIGHTNESS, 255))
    
        # C-Bus dimmers cannot ramp to 0 → OFF instead
        if brightness <= 10:
            await self.async_turn_off()
            return
    
        await self.coordinator.session.set_group_level(
            self.project,
            self.network,
            self._app,
            self._group,
            brightness,
        )



    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 0
        )
