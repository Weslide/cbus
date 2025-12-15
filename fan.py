# fan.py – Home Assistant C-Bus fan entity (HA 2025 compatible)
#
# Single lighting-group ceiling fan:
# OFF = 0
# LOW ≈ 84
# MED ≈ 163
# HIGH = 255
#
# Matches real C-Gate traffic:
#   lighting ramp ... 84
#   lighting ramp ... 163
#   lighting on
#   lighting off

import logging
from typing import Any, List

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Exact C-Bus fan levels (from real logs)
# -------------------------------------------------------------------

LEVEL_OFF = 0
LEVEL_LOW = 84
LEVEL_MED = 163
LEVEL_HIGH = 255

# Preset labels shown in Home Assistant UI
PRESET_LOW = "Low"
PRESET_MED = "Medium"
PRESET_HIGH = "High"

PRESET_TO_LEVEL = {
    PRESET_LOW: LEVEL_LOW,
    PRESET_MED: LEVEL_MED,
    PRESET_HIGH: LEVEL_HIGH,
}

LEVEL_TO_PRESET = {
    LEVEL_LOW: PRESET_LOW,
    LEVEL_MED: PRESET_MED,
    LEVEL_HIGH: PRESET_HIGH,
}

# -------------------------------------------------------------------
# Mapping helpers (explicit, no guessing)
# -------------------------------------------------------------------

def _level_to_pct(level: int) -> int:
    """Convert C-Bus level → HA percentage."""
    if level <= 0:
        return 0
    if level < 100:      # ~84
        return 33
    if level < 200:      # ~163
        return 67
    return 100           # 255


def _pct_to_level(percentage: int) -> int:
    """Convert HA percentage → C-Bus level."""
    if percentage <= 0:
        return LEVEL_OFF
    if percentage <= 33:
        return LEVEL_LOW
    if percentage <= 67:
        return LEVEL_MED
    return LEVEL_HIGH


# -------------------------------------------------------------------
# Platform setup
# -------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CBusCoordinator = data["coordinator"]
    model = coordinator.discovery_model
    project = coordinator.project_name

    entities: List[CBusFan] = []

    for network_id, network_data in model.items():
        app56 = network_data.get("applications", {}).get("56")
        if not app56:
            continue

        for group_id, group_info in app56.get("groups", {}).items():
            if not group_info.get("is_load"):
                continue
            if group_info.get("device_class") != "fan":
                continue

            entities.append(
                CBusFan(
                    coordinator=coordinator,
                    project=project,
                    network=str(network_id),
                    app=56,
                    group=int(group_id),
                    name=group_info.get("name", f"Fan {group_id}"),
                )
            )

    if entities:
        _LOGGER.info("Loaded %d C-Bus fan entities", len(entities))
        async_add_entities(entities)
    else:
        _LOGGER.info("No C-Bus fans found.")


# -------------------------------------------------------------------
# Fan Entity
# -------------------------------------------------------------------

class CBusFan(FanEntity):
    """Single-group C-Bus ceiling fan."""

    _attr_should_poll = False

    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
    )

    _attr_percentage_step = 33
    _attr_preset_modes = [PRESET_LOW, PRESET_MED, PRESET_HIGH]

    def __init__(
        self,
        coordinator: CBusCoordinator,
        project: str,
        network: str,
        app: int,
        group: int,
        name: str,
    ) -> None:
        self.coordinator = coordinator
        self.project = str(project)
        self.network = str(network)
        self._app = int(app)
        self._group = int(group)

        self._attr_name = name
        self._attr_unique_id = (
            f"cbus_fan_{self.project}_{self.network}_{self._app}_{self._group}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _key(self):
        return (self.project, self.network, self._app, self._group)

    @property
    def _current_level(self) -> int:
        return int(self.coordinator.group_levels.get(self._key, 0))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        try:
            lvl = await self.coordinator.session.get_group_level(
                self.project, self.network, self._app, self._group
            )
            if lvl is not None:
                self.coordinator.group_levels[self._key] = int(lvl)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Initial level fetch failed for %s: %s", self._key, exc)

        self.coordinator.register_callback(
            self._app,
            self._group,
            self._update_from_bus,
            project=self.project,
            network=self.network,
        )

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self.coordinator.unregister_callback(
            self._app,
            self._group,
            self._update_from_bus,
            project=self.project,
            network=self.network,
        )

    def _update_from_bus(self, level: int) -> None:
        self.coordinator.group_levels[self._key] = int(level)
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # HA state
    # ------------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return self._current_level > 0

    @property
    def percentage(self) -> int | None:
        # HA requires a non-zero percentage to keep the slider "on"
        # even when preset_mode is active.
        if not self.is_on:
            return 0
        return _level_to_pct(self._current_level)

    @property
    def preset_mode(self) -> str | None:
        return LEVEL_TO_PRESET.get(self._current_level)

    # ------------------------------------------------------------------
    # HA commands
    # ------------------------------------------------------------------

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        if percentage is not None:
            await self.async_set_percentage(int(percentage))
            return

        level = self._current_level if self._current_level > 0 else LEVEL_MED
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, level
        )

        # Immediate UI sync
        self.coordinator.group_levels[self._key] = level
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, LEVEL_OFF
        )

    async def async_set_percentage(self, percentage: int) -> None:
        level = _pct_to_level(int(percentage))
        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, level
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        level = PRESET_TO_LEVEL.get(preset_mode)
        if level is None:
            return

        await self.coordinator.session.set_group_level(
            self.project, self.network, self._app, self._group, level
        )

        self.coordinator.group_levels[self._key] = level
        self.async_write_ha_state()
