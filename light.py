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
        # Increase threshold to 12 to ignore pre-heat/noise (approx 5%)
        return self._current_level > 6

    @property
    def brightness(self):
        lvl = self._current_level
        if lvl >= 255:
            return 255
        # Return None if below the 5% threshold so the UI shows 'Off'
        return lvl if lvl > 6 else None

    async def async_turn_on(self, **kwargs):
        # If no brightness provided (toggle), default to full
        brightness = int(kwargs.get(ATTR_BRIGHTNESS, 255))
    
        # Ensure we don't send 0 to C-Gate as an 'on' command
        if brightness <= 5:
            await self.async_turn_off()
            return
    
        # Send to C-Gate
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, brightness
        )
        
        # UI Optimistic Update: Immediately tell the coordinator we are at this level
        # This prevents the "jump" while waiting for the C-Gate confirm
        key = (self.project, self.network, self._app, self._group)
        self.coordinator.handle_group_update(
            self.project, self.network, self._app, self._group, brightness
        )



    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, 0
        )
