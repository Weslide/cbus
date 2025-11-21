# config_flow.py
from __future__ import annotations

import logging
from typing import Any, Dict

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_PROJECT,
    CONF_NETWORK,
    CONF_PORT_CMD,
    CONF_PORT_EVENT,
    CONF_PORT_STATUS,
    DEFAULT_NETWORK,
    DEFAULT_PORT_CMD,
    DEFAULT_PORT_EVENT,
    DEFAULT_PORT_STATUS,
)
from .cgatesession import CGateSession

_LOGGER = logging.getLogger(__name__)


async def _test_connection(
    hass: HomeAssistant,
    host: str,
    project: str,
    port_cmd: int,
    port_event: int,
    port_status: int,
) -> bool:
    """Try to connect to C-Gate and select the project."""
    session = CGateSession(
        host=host,
        port_cmd=port_cmd,
        port_event=port_event,
        port_status=port_status,
    )
    try:
        await session.async_connect()
        await session.send_command(f"project use {project}")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("C-Gate connection test failed: %s", exc)
        return False
    finally:
        try:
            await session.async_close()
        except Exception:  # noqa: BLE001
            pass
    return True


class CBusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the C-Bus (C-Gate) integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Dict[str, Any] | None = None
    ):
        errors: Dict[str, str] = {}

        if user_input is None:
            return self._show_form(errors)

        host = user_input[CONF_HOST]
        project = user_input[CONF_PROJECT]
        network = user_input[CONF_NETWORK]
        port_cmd = user_input[CONF_PORT_CMD]
        port_event = user_input[CONF_PORT_EVENT]
        port_status = user_input[CONF_PORT_STATUS]

        ok = await _test_connection(
            self.hass,
            host=host,
            project=project,
            port_cmd=port_cmd,
            port_event=port_event,
            port_status=port_status,
        )

        if not ok:
            errors["base"] = "cannot_connect"

        if errors:
            return self._show_form(errors, user_input)

        await self.async_set_unique_id(f"{host}_{project}_{network}")
        self._abort_if_unique_id_configured()

        title = f"C-Bus @ {host}"
        return self.async_create_entry(title=title, data=user_input)

    def _show_form(
        self,
        errors: Dict[str, str],
        user_input: Dict[str, Any] | None = None,
    ):
        if user_input is None:
            user_input = {}

        data_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=user_input.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PROJECT,
                    default=user_input.get(CONF_PROJECT, ""),
                ): str,
                vol.Required(
                    CONF_NETWORK,
                    default=user_input.get(CONF_NETWORK, DEFAULT_NETWORK),
                ): str,
                vol.Required(
                    CONF_PORT_CMD,
                    default=user_input.get(CONF_PORT_CMD, DEFAULT_PORT_CMD),
                ): int,
                vol.Required(
                    CONF_PORT_EVENT,
                    default=user_input.get(CONF_PORT_EVENT, DEFAULT_PORT_EVENT),
                ): int,
                vol.Required(
                    CONF_PORT_STATUS,
                    default=user_input.get(CONF_PORT_STATUS, DEFAULT_PORT_STATUS),
                ): int,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )
