"""
coordinator.py – fully corrected version
"""

import logging
from typing import Any, Dict, Tuple, Callable, DefaultDict
from collections import defaultdict

from homeassistant.core import HomeAssistant

from .cgatesession import CGateSession

_LOGGER = logging.getLogger(__name__)


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
        self.group_levels: Dict[Tuple[str, str, int, int], int] = {}

        # Callbacks waiting for live updates:
        # callbacks[(project, network, app, group)] = [fn, fn, fn]
        self._callbacks: DefaultDict[
            Tuple[str, str, int, int], list[Callable[[int], None]]
        ] = defaultdict(list)

        # Attach ourselves to CGateSession event stream
        self.session.set_group_update_callback(self.handle_group_update)

        _LOGGER.info(
            "CBusCoordinator initialised for project=%s network=%s",
            self.project_name,
            self.network_id,
        )

    # ------------------------------------------------------------------
    # CALLBACK REGISTRATION
    # ------------------------------------------------------------------

    def register_callback(
        self,
        app: int,
        group: int,
        callback: Callable[[int], None],
        project: str | None = None,
        network: str | None = None,
    ) -> None:
        """Entities call this to subscribe to updates."""

        project = project or self.project_name
        network = network or self.network_id

        key = (str(project), str(network), int(app), int(group))
        self._callbacks[key].append(callback)

        _LOGGER.debug("Registered callback for %s", key)

    def unregister_callback(
        self,
        app: int,
        group: int,
        callback: Callable[[int], None],
        project: str | None = None,
        network: str | None = None,
    ) -> None:
        """Entities call this when being removed."""

        project = project or self.project_name
        network = network or self.network_id

        key = (str(project), str(network), int(app), int(group))

        try:
            self._callbacks[key].remove(callback)
            _LOGGER.debug("Unregistered callback for %s", key)
        except (KeyError, ValueError):
            pass

    # ------------------------------------------------------------------
    # HANDLE INCOMING CGATE EVENTS
    # ------------------------------------------------------------------

    def handle_group_update(
        self,
        project: str,
        network: str,
        app: int,
        group: int,
        level: int,
    ) -> None:
        """Called by CGateSession for every event level change."""

        key = (str(project), str(network), int(app), int(group))
        self.group_levels[key] = int(level)

        _LOGGER.debug(
            "Coordinator received update %s -> %d", key, level
        )

        # Dispatch to all subscribed entity callbacks
        for cb in list(self._callbacks.get(key, [])):
            try:
                cb(level)
            except Exception as exc:
                _LOGGER.error(
                    "Callback failed for %s: %s", key, exc
                )

