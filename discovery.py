import logging
import re
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PROJECT, CONF_NETWORK
from .cgatesession import CGateSession

_LOGGER = logging.getLogger(__name__)

GROUPS_LINE_RE = re.compile(r"^3\d\d[-\s]+//[^:]+:\s+Groups=([0-9,]+)\s*$")
PARAM_LINE_RE = re.compile(r"^3\d\d[-\s]+//[^:]+:\s+(.*)$")

class CBusDiscovery:
    """Discovery using GET + DBGET + name classification."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.session: CGateSession | None = None

    async def async_discover(self) -> Dict[str, Any]:
        assert self.session is not None

        project = self.entry.data[CONF_PROJECT]
        network = str(self.entry.data[CONF_NETWORK])

        model = {network: {"applications": {}}}

        await self._safe_cmd(f"project use {project}")
        await self._safe_cmd(f"net open //{project}/{network}")

        model[network]["applications"]["56"] = await self._discover_app(
            project, network, "56"
        )

        return model

    async def _safe_cmd(self, cmd: str):
        try:
            await self.session.send_command(cmd)
        except Exception as ex:
            _LOGGER.warning("Command failed: %s (%s)", cmd, ex)

    async def _discover_app(self, project: str, network: str, app_id: str):
        app = {"type": "lighting", "name": f"Lighting {app_id}", "groups": {}}

        # Read group list
        try:
            lines = await self.session.send_command(
                f"get //{project}/{network}/{app_id} Groups"
            )
        except:
            return app

        groups = []
        for line in lines:
            m = GROUPS_LINE_RE.match(line)
            if not m:
                continue
            for tok in m.group(1).split(","):
                try:
                    groups.append(int(tok.strip()))
                except:
                    pass

        groups = sorted(set(groups))
        if not groups:
            return app

        # Group details
        for gid in groups:
            gpath = f"//{project}/{network}/{app_id}/{gid}"

            try:
                glines = await self.session.send_command(f"get {gpath} *")
            except:
                glines = []

            params = self._parse_get_params(glines)
            units = params.get("Units", "").strip()

            # Toolkit name via DBGET
            name = await self._dbget_name(gpath)
            if not name:
                name = params.get("Name", "").strip() or f"Group {gid}"
            # DEBUG — REMOVE LATER
            _LOGGER.warning("DISCOVERY: gid=%s name=%s units=%s", gid, name, units)
    
            device_class, is_load = self._classify(name, units)
            
            # DEBUG — REMOVE LATER
            _LOGGER.warning(
                "CLASSIFY: gid=%s → device_class=%s is_load=%s",
                gid, device_class, is_load
            )
    
            app["groups"][str(gid)] = {
                "name": name,
                "device_class": device_class,
                "is_load": is_load,
                "units": units,
            }

        return app

    async def _dbget_name(self, gpath: str) -> str | None:
        try:
            rows = await self.session.send_command(f"dbget {gpath}")
        except:
            return None

        for r in rows:
            if "TagName=" in r:
                return r.split("TagName=", 1)[1].replace('"', "").strip()
        return None

    def _parse_get_params(self, lines):
        params = {}
        for line in lines:
            m = PARAM_LINE_RE.match(line)
            if not m:
                continue
            payload = m.group(1).strip()
            if "=" not in payload:
                continue
            k, v = payload.split("=", 1)
            params[k.strip()] = v.strip()
        return params

    # ------------------------------------------------------------
    # CLASSIFIER (Exhaust → Fan → Relay → Dimmer)
    # ------------------------------------------------------------
    def _classify(self, name: str, units: str | None):

        n = (name or "").lower()

        # 1) Exhaust fan (must be FIRST)
        if "exhaust" in n:
            return "exhaust", True

        # 2) Ceiling fan
        if "fan" in n:
            return "fan", True

        # 3) No units → ignore (keypads)
        if not units or not units.strip():
            return "keypad", False

        # Count relay/dimmer channels
        unit_count = len([u for u in units.split(",") if u.strip()])

        if unit_count == 1:
            return "switch", True  # Relay
        return "light", True       # Dimmer
