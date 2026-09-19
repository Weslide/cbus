"""Coordinator: holds the discovery model, live group levels and routes
C-Gate events to entities.

Two kinds of subscribers:
  * group callbacks  — cb(level)                     (lights, switches, fans)
  * unit callbacks   — cb(app, group, level)         (keypads, eDLTs, PIRs)

A unit callback fires when that *physical unit originated* a group change,
which C-Gate reports via ``sourceunit=N`` on its event / load-change lines.
"""

import logging
from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict, Optional, Tuple

from homeassistant.core import HomeAssistant

from .cgatesession import CGateSession
from .const import (
    ATTR_APP,
    ATTR_GROUP,
    ATTR_GROUP_NAME,
    ATTR_LEVEL,
    ATTR_SLOT,
    ATTR_UNIT,
    ATTR_UNIT_NAME,
    ATTR_UNIT_TYPE,
    EVENT_CBUS_UNIT_EVENT,
    INPUT_ROLES,
    ROLE_LOAD,
)
from .device import hub_device_info, unit_device_info

_LOGGER = logging.getLogger(__name__)

GroupKey = Tuple[str, str, int, int]
UnitKey = Tuple[str, str, int]


class CBusCoordinator:
    """Holds discovery model, live levels, and callback routing."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: CGateSession,
        discovery_model: Dict[str, Any],
        project_name: str,
        network_id: str,
    ) -> None:
        self.hass = hass
        self.session = session
        self.discovery_model = discovery_model

        self.project_name = project_name
        self.network_id = str(network_id)

        # (project, network, app, group) -> int level
        self.group_levels: Dict[GroupKey, int] = {}
        # (project, network, app, group) -> unit that last changed it (or None)
        self.last_source_unit: Dict[GroupKey, Optional[int]] = {}

        self._group_callbacks: DefaultDict[
            GroupKey, list[Callable[[int], None]]
        ] = defaultdict(list)
        self._unit_callbacks: DefaultDict[
            UnitKey, list[Callable[[int, int, int], None]]
        ] = defaultdict(list)

        # Link health (command port up AND network running) for availability
        self.link_ok: bool = True
        self._link_listeners: list[Callable[[bool], None]] = []

        self.session.set_group_update_callback(self.handle_group_update)
        self.session.set_link_callback(self.handle_link_change)

        _LOGGER.info(
            "CBusCoordinator initialised for project=%s network=%s",
            self.project_name,
            self.network_id,
        )

    # ------------------------------------------------------------------
    # Model helpers
    # ------------------------------------------------------------------

    def lighting_groups(self):
        """Yield (network_id, app_id:int, group_id:int, group_info) for every
        group of every lighting-type application discovered."""
        for network_id, net in self.discovery_model.items():
            for app_id, app in net.get("applications", {}).items():
                if app.get("type") != "lighting":
                    continue
                for gid, gi in app.get("groups", {}).items():
                    yield str(network_id), int(app_id), int(gid), gi

    def units(self, network: str | None = None) -> Dict[str, Any]:
        net = self.discovery_model.get(str(network or self.network_id), {})
        return net.get("units", {})

    def unit(self, address: int, network: str | None = None) -> Dict[str, Any] | None:
        return self.units(network).get(str(int(address)))

    def group_info(self, app: int, group: int, network: str | None = None) -> Dict[str, Any]:
        net = self.discovery_model.get(str(network or self.network_id), {})
        return (
            net.get("applications", {})
            .get(str(int(app)), {})
            .get("groups", {})
            .get(str(int(group)), {})
        )

    def group_name(self, app: int, group: int, network: str | None = None) -> str:
        return self.group_info(app, group, network).get("name") or f"Group {group}"

    def device_info_for_unit(self, address: int, network: str | None = None):
        unit = self.unit(address, network)
        if not unit:
            return None
        return unit_device_info(self.project_name, str(network or self.network_id), unit)

    def hub_device_info(self, network: str | None = None):
        return hub_device_info(
            self.project_name,
            str(network or self.network_id),
            getattr(self.session, "host", None),
            getattr(self.session, "server_version", None),
        )

    def device_info_for_group(self, app: int, group: int, network: str | None = None):
        """DeviceInfo of the output unit that drives this group (first load
        unit; falls back to the first input unit for keypad-only groups)."""
        info = self.group_info(app, group, network)
        for addr in info.get("load_units", []) + info.get("input_units", []):
            di = self.device_info_for_unit(addr, network)
            if di:
                return di
        return None

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def _gkey(self, app, group, project=None, network=None) -> GroupKey:
        return (
            str(project or self.project_name),
            str(network or self.network_id),
            int(app),
            int(group),
        )

    def _ukey(self, unit, project=None, network=None) -> UnitKey:
        return (
            str(project or self.project_name),
            str(network or self.network_id),
            int(unit),
        )

    def register_callback(
        self,
        app: int,
        group: int,
        callback: Callable[[int], None],
        project: str | None = None,
        network: str | None = None,
    ) -> None:
        """Entities call this to subscribe to level updates for a group."""
        key = self._gkey(app, group, project, network)
        self._group_callbacks[key].append(callback)
        _LOGGER.debug("Registered group callback for %s", key)

    def unregister_callback(
        self,
        app: int,
        group: int,
        callback: Callable[[int], None],
        project: str | None = None,
        network: str | None = None,
    ) -> None:
        key = self._gkey(app, group, project, network)
        try:
            self._group_callbacks[key].remove(callback)
        except (KeyError, ValueError):
            pass

    def register_unit_callback(
        self,
        unit: int,
        callback: Callable[[int, int, int], None],
        project: str | None = None,
        network: str | None = None,
    ) -> None:
        """Subscribe to group changes *originated by* a physical unit."""
        key = self._ukey(unit, project, network)
        self._unit_callbacks[key].append(callback)
        _LOGGER.debug("Registered unit callback for %s", key)

    def unregister_unit_callback(
        self,
        unit: int,
        callback: Callable[[int, int, int], None],
        project: str | None = None,
        network: str | None = None,
    ) -> None:
        key = self._ukey(unit, project, network)
        try:
            self._unit_callbacks[key].remove(callback)
        except (KeyError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Link health
    # ------------------------------------------------------------------

    def add_link_listener(self, cb: Callable[[bool], None]) -> Callable[[], None]:
        """Subscribe to link up/down changes; returns an unsubscribe fn."""
        self._link_listeners.append(cb)

        def _unsub() -> None:
            try:
                self._link_listeners.remove(cb)
            except ValueError:
                pass

        return _unsub

    def handle_link_change(self, ok: bool) -> None:
        self.link_ok = bool(ok)
        for cb in list(self._link_listeners):
            try:
                cb(self.link_ok)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Link listener failed: %s", exc)

    @property
    def link_info(self) -> Dict[str, Any]:
        info = dict(getattr(self.session, "stats", {}))
        info["link_ok"] = self.link_ok
        info["cgate_version"] = getattr(self.session, "server_version", None)
        info["host"] = getattr(self.session, "host", None)
        return info

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def async_resync(self) -> None:
        """Re-poll every load group's level from C-Gate and push to entities.

        Called after the C-Gate link recovers (socket reattach or the C-Bus
        network reopening) so HA reflects any changes missed while blind.
        Polled updates carry no source unit, so they don't fire spurious
        motion / keypad events.
        """
        count = 0
        for net_id, net in self.discovery_model.items():
            for app_id, app in net.get("applications", {}).items():
                for gid, gi in app.get("groups", {}).items():
                    if not gi.get("is_load"):
                        continue
                    try:
                        lvl = await self.session.get_group_level(
                            self.project_name, net_id, int(app_id), int(gid)
                        )
                    except Exception:  # noqa: BLE001
                        continue
                    if lvl is not None:
                        self.handle_group_update(
                            self.project_name, net_id, int(app_id), int(gid), int(lvl)
                        )
                        count += 1
        _LOGGER.info("cbus: resync refreshed %d group levels", count)

    # ------------------------------------------------------------------
    # Incoming C-Gate events
    # ------------------------------------------------------------------

    def handle_group_update(
        self,
        project: str,
        network: str,
        app: int,
        group: int,
        level: int,
        source_unit: int | None = None,
    ) -> None:
        """Called by CGateSession for every group level change."""
        key = self._gkey(app, group, project, network)
        self.group_levels[key] = int(level)
        if source_unit is not None:
            self.last_source_unit[key] = int(source_unit)

        _LOGGER.debug(
            "Coordinator update %s -> %d (source unit %s)", key, level, source_unit
        )

        for cb in list(self._group_callbacks.get(key, [])):
            try:
                cb(int(level))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Group callback failed for %s: %s", key, exc)

        if source_unit is None:
            return

        ukey = self._ukey(source_unit, project, network)
        for cb in list(self._unit_callbacks.get(ukey, [])):
            try:
                cb(int(app), int(group), int(level))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.error("Unit callback failed for %s: %s", ukey, exc)

        self._fire_unit_event(str(network), int(app), int(group), int(level), int(source_unit))

    def _fire_unit_event(self, network: str, app: int, group: int, level: int, unit: int) -> None:
        """Publish ``cbus_unit_event`` on the HA bus for input-unit originated changes."""
        info = self.unit(unit, network)
        if not info or info.get("role") not in INPUT_ROLES:
            return

        slot = None
        for s in info.get("slots", []):
            if int(s.get("app", -1)) == app and int(s.get("group", -1)) == group:
                slot = s.get("slot")
                break

        try:
            self.hass.bus.async_fire(
                EVENT_CBUS_UNIT_EVENT,
                {
                    ATTR_UNIT: unit,
                    ATTR_UNIT_NAME: info.get("name"),
                    ATTR_UNIT_TYPE: info.get("type"),
                    ATTR_APP: app,
                    ATTR_GROUP: group,
                    ATTR_GROUP_NAME: self.group_name(app, group, network),
                    ATTR_LEVEL: level,
                    ATTR_SLOT: slot,
                },
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Could not fire %s: %s", EVENT_CBUS_UNIT_EVENT, exc)
