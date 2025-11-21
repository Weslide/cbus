from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up C-Bus climate platform.

    This integration currently does not expose any climate devices.
    The module exists purely as a safe stub so that 'climate'
    can be listed as a supported platform (now or in the future)
    without causing setup errors.
    """
    _LOGGER.debug(
        "C-Bus climate platform loaded for config entry %s, "
        "but no climate entities are currently implemented.",
        entry.entry_id,
    )
    # Intentionally do nothing: no entities to add.
    return None
