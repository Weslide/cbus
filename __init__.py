import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PROJECT,
    CONF_NETWORK,
    CONF_PORT_CMD,
    CONF_PORT_EVENT,
    CONF_PORT_STATUS,
)
from .cgatesession import CGateSession
from .discovery import CBusDiscovery
from .coordinator import CBusCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["light", "switch", "fan", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up the C-Bus integration from a config entry."""
    host = entry.data[CONF_HOST]
    project = entry.data[CONF_PROJECT]
    network = str(entry.data[CONF_NETWORK])

    port_cmd = entry.data[CONF_PORT_CMD]
    port_event = entry.data[CONF_PORT_EVENT]
    port_status = entry.data[CONF_PORT_STATUS]

    # 1) Create the C-Gate session
    session = CGateSession(
        host=host,
        port_cmd=port_cmd,
        port_event=port_event,
        port_status=port_status,
    )

    try:
        await session.async_connect()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("Failed to connect to C-Gate: %s", exc)
        raise

    # 2) Discovery
    discovery = CBusDiscovery(hass, entry)
    discovery.session = session

    try:
        model = await discovery.async_discover()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("C-Bus discovery failed: %s", exc)
        raise

    # 3) Coordinator
    coordinator = CBusCoordinator(
        hass=hass,
        session=session,
        discovery_model=model,
        project_name=project,
        network_id=network,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "session": session,
        "coordinator": coordinator,
        "model": model,
    }

    # 4) Register global event callback
    def handle_global_event(proj: str, net: str, app: int, grp: int, level: int):
        coordinator.handle_group_update(proj, net, app, grp, level)

    session.register_global_callback(handle_global_event)

    # 5) Load platforms (light, sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("C-Bus integration setup complete for project=%s, network=%s", project, network)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a C-Bus config entry."""
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    if not data:
        return True

    session: CGateSession = data["session"]

    try:
        await session.async_close()
    except Exception:  # noqa: BLE001
        pass

    await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return True
