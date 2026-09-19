import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

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

PLATFORMS = ["light", "switch", "fan", "sensor", "binary_sensor"]


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

    # 3) Coordinator — this wires itself up to the session's group-update
    # and link callbacks (see CBusCoordinator.__init__), so no separate
    # session.register_global_callback() is needed here. (The previous
    # version registered both, which meant every event handler ran twice.)
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

    # 4) Give the session its network context + a resync hook so the
    #    keepalive can watch/reopen the C-Bus interface and refresh state
    #    after a reconnect.
    session.set_context(project, network)
    session.set_resync_callback(coordinator.async_resync)

    # 5) Register the hub device (the C-Gate server / C-Bus network) so it
    #    shows up in the device registry as its own device.
    dev_reg = dr.async_get(hass)
    hub = coordinator.hub_device_info()
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=hub["identifiers"],
        manufacturer=hub.get("manufacturer"),
        name=hub.get("name"),
        model=hub.get("model"),
        sw_version=hub.get("sw_version"),
    )

    # 6) Load platforms
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
