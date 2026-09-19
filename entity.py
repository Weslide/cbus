"""Shared entity behaviour: availability tied to the C-Gate link."""
from __future__ import annotations

from homeassistant.core import callback


class CBusLinkMixin:
    """Mix into entities so they go *unavailable* when the C-Gate command
    link or the C-Bus network interface is down, rather than showing stale
    state. Requires ``self.coordinator`` (CBusCoordinator).
    """

    _link_unsub = None

    @property
    def available(self) -> bool:
        return bool(getattr(self.coordinator, "link_ok", True))

    def _attach_link_listener(self) -> None:
        """Call from async_added_to_hass."""

        @callback
        def _on_link(_ok: bool) -> None:
            self.async_write_ha_state()

        self._link_unsub = self.coordinator.add_link_listener(_on_link)

    def _detach_link_listener(self) -> None:
        if self._link_unsub:
            self._link_unsub()
            self._link_unsub = None
