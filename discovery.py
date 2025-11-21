import logging
import re
from typing import Any, Dict, Tuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PROJECT, CONF_NETWORK
from .cgatesession import CGateSession

_LOGGER = logging.getLogger(__name__)

GROUPS_LINE_RE = re.compile(
    r"^3\d\d[-\s]+//[^:]+:\s+Groups=([0-9,]+)\s*$"
)
PARAM_LINE_RE = re.compile(
    r"^3\d\d[-\s]+//[^:]+:\s+(.*)$"
)

class CBusDiscovery:
    """Fast discovery using GET for units + DBGET for correct names."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.session: CGateSession | None = None

    async def async_discover(self) -> Dict[str, Any]:
        assert self.session is not None

        project = self.entry.data[CONF_PROJECT]
        network = str(self.entry.data[CONF_NETWORK])

        model: Dict[str, Any] = {
            network: {"applications": {}}
        }

        await self._safe_cmd(f"project use {project}")
        await self._safe_cmd(f"net open //{project}/{network}")

        # Only lighting app (56)
        model[network]["applications"]["56"] = await self._discover_app(
            project,
            network,
            app_id="56"
        )

        return model

    # ------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------

    async def _safe_cmd(self, cmd: str):
        try:
            await self.session.send_command(cmd)
        except Exception as ex:
            _LOGGER.warning("Command failed: %s (%s)", cmd, ex)

    async def _discover_app(self, project: str, network: str, app_id: str):
        app = {
            "type": "lighting",
            "name": f"Lighting (App {app_id})",
            "groups": {},
        }

        # 1) Read Groups list
        try:
            lines = await self.session.send_command(
                f"get //{project}/{network}/{app_id} Groups"
            )
        except:
            return app

        groups: list[int] = []
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

        # 2) For each group: GET + DBGET
        for gid in groups:
            gpath = f"//{project}/{network}/{app_id}/{gid}"

            # --- 2a) read GET (for Units + Level etc)
            try:
                glines = await self.session.send_command(f"get {gpath} *")
            except:
                glines = []

            params = self._parse_get_params(glines)
            units = params.get("Units", "").strip()

            # --- 2b) ALWAYS use DBGET for NAME (this is the FIX)
            name = await self._dbget_name(gpath)
            if not name:
                name = params.get("Name", "").strip() or f"Group {gid}"

            # classify
            device_class, is_load = self._classify(name, units)

            app["groups"][str(gid)] = {
                "name": name,
                "device_class": device_class,
                "is_load": is_load,
                "units": units,
            }

        _LOGGER.info(
            "Discovered %d lighting groups with loads on %s",
            sum(1 for g in app["groups"].values() if g["is_load"]),
            f"//{project}/{network}/{app_id}",
        )

        return app

    # ------------------------------------------------------------
    # DBGET NAME RESOLUTION  (From your OLD integration)
    # ------------------------------------------------------------

    async def _dbget_name(self, gpath: str) -> str | None:
        """Extract TagName using dbget (Toolkit-style names)."""
        try:
            rows = await self.session.send_command(f"dbget {gpath}")
        except Exception:
            return None

        for row in rows:
            if "TagName=" in row:
                return (
                    row.split("TagName=", 1)[1]
                    .replace('"', "")
                    .strip()
                )
        return None

    # ------------------------------------------------------------

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

    def _classify(self, name: str, units: str | None):
        """Accurate dimmer/relay classification based strictly on Units count."""

        # No units → keypad / logic-only → ignore
        if not units or not units.strip():
            return "keypad", False

        # Count units (“3,10,23” → 3 units)
        unit_list = [u.strip() for u in units.split(",") if u.strip()]
        unit_count = len(unit_list)

        lower = (name or "").lower()

        # Fan (OFF/33/66/100)
        if "fan" in lower:
            return "fan", True

        # Relay output: single unit → switch
        if unit_count == 1:
            return "switch", True

        # Multi-unit output: dimmer → light
        return "light", True
