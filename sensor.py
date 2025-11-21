import logging
_LOGGER = logging.getLogger(__name__)

# We disable all sensor creation — Lighting App 56 has no useful sensors.
async def async_setup_entry(hass, entry, async_add_entities):
    _LOGGER.info("CBusSensor: No sensors created (disabled)")
    return

