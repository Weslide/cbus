import json
import logging
import os
import re
from typing import Any, Dict, List

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PROJECT, CONF_NETWORK, ROLE_LOAD, INPUT_ROLES
from .cgatesession import CGateSession
from .device import unit_role

_LOGGER = logging.getLogger(__name__)

GROUPS_LINE_RE = re.compile(r"^3\d\d[-\s]+//[^:]+:\s+Groups=([0-9,]+)\s*$")
PARAM_LINE_RE = re.compile(r"^3\d\d[-\s]+//[^:]+:\s+(.*)$")

OVERRIDES_PATH = "/config/cbus_overrides.json"

# C-Gate uses 255 as "unassigned" in unit slot / widget tables
UNASSIGNED = 255


class CBusDiscovery:
    """Discovery using GET + DBGET + name classification.

    Produces a model of the form::

        {network: {
            "applications": {"56": {"groups": {gid: {...}}}},
            "units": {addr: {address, type, catalog, firmware, name, role,
                             app, groups, slots, has_light_level}},
        }}

    Every group additionally records the physical units that drive it
    (``load_units``) and the input units programmed to it (``input_units``).

    Only the default lighting application ("56") is enumerated — this
    integration targets a single-application C-Bus lighting setup, so
    multi-application scanning is intentionally left out to limit scope.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.session: CGateSession | None = None
        self._overrides: Dict[str, Any] = {}

    @staticmethod
    def _load_overrides_file(path: str) -> Dict[str, Any]:
        """Blocking file read — must run in an executor, not the event loop."""
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    async def async_discover(self) -> Dict[str, Any]:
        assert self.session is not None

        project = self.entry.data[CONF_PROJECT]
        network = str(self.entry.data[CONF_NETWORK])

        try:
            self._overrides = await self.hass.async_add_executor_job(
                self._load_overrides_file, OVERRIDES_PATH
            )
            if self._overrides:
                _LOGGER.info("cbus: loaded %d group overrides", len(self._overrides))
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("cbus: override load failed: %s", e)
            self._overrides = {}

        model = {network: {"applications": {}, "units": {}}}

        await self._safe_cmd(f"project use {project}")
        await self._safe_cmd(f"net open //{project}/{network}")

        # Units first: knowing which relay/dimmer drives a group is the most
        # reliable way to classify it.
        model[network]["units"] = await self._discover_units(project, network)
        load_types = self._load_types_by_group(model[network]["units"])
        input_units = self._input_units_by_group(model[network]["units"])

        model[network]["applications"]["56"] = await self._discover_app(
            project, network, "56", load_types, input_units
        )

        self._annotate_groups(model[network])

        return model

    async def _safe_cmd(self, cmd: str):
        try:
            await self.session.send_command(cmd)
        except Exception as ex:  # noqa: BLE001
            _LOGGER.warning("Command failed: %s (%s)", cmd, ex)

    # ------------------------------------------------------------------
    # Lighting application groups
    # ------------------------------------------------------------------

    async def _discover_app(
        self,
        project: str,
        network: str,
        app_id: str,
        load_types: Dict[tuple, List[str]] | None = None,
        input_units: Dict[tuple, List[int]] | None = None,
    ):
        app = {"type": "lighting", "name": f"Lighting {app_id}", "groups": {}}
        app_num = int(app_id)
        load_types = load_types or {}
        # None => unit roles unknown (legacy heuristics); {} => known, none
        roles_known = input_units is not None
        input_units = input_units or {}
        overrides_authoritative = bool(self._overrides) and app_num == 56

        # Read group list
        try:
            lines = await self.session.send_command(
                f"get //{project}/{network}/{app_id} Groups"
            )
        except Exception:  # noqa: BLE001
            return app

        groups = []
        for line in lines:
            m = GROUPS_LINE_RE.match(line)
            if not m:
                continue
            groups.extend(self._parse_int_list(m.group(1)))

        groups = sorted(set(groups))
        if not groups:
            return app

        # Group details
        for gid in groups:
            gpath = f"//{project}/{network}/{app_id}/{gid}"

            try:
                glines = await self.session.send_command(f"get {gpath} *")
            except Exception:  # noqa: BLE001
                glines = []

            params = self._parse_get_params(glines)
            units = params.get("Units", "").strip()

            # Area groups are broadcast targets, not loads — no entity.
            if params.get("Type", "group").strip().lower() == "area":
                _LOGGER.debug("DISCOVERY: gid=%s is an area group; skipping", gid)
                continue

            # Toolkit name via DBGET
            name = await self._dbget_name(gpath)
            if not name:
                name = params.get("Name", "").strip() or f"Group {gid}"
            _LOGGER.debug("DISCOVERY: gid=%s name=%s units=%s", gid, name, units)

            _ov = self._overrides.get(f"{app_num}/{gid}")
            if _ov is None and app_num == 56:
                _ov = self._overrides.get(str(gid))
            if _ov or overrides_authoritative:
                if not _ov:
                    continue
                device_class = _ov.get("device_class", "light")
                is_load = True
                dimmable = bool(_ov.get("dimmable", False))
                if _ov.get("name"):
                    name = _ov["name"]
            else:
                device_class, is_load, dimmable = self._classify(
                    name,
                    units,
                    load_types.get((app_num, gid)),
                    input_units.get((app_num, gid), []) if roles_known else None,
                )

            # Unused channels ("Relay 1/11 Spare") still get entities, but
            # disabled by default so they don't clutter the UI.
            enabled_default = "spare" not in name.lower()

            _LOGGER.debug(
                "CLASSIFY: gid=%s -> device_class=%s is_load=%s dimmable=%s enabled=%s",
                gid, device_class, is_load, dimmable, enabled_default
            )

            app["groups"][str(gid)] = {
                "name": name,
                "device_class": device_class,
                "is_load": is_load,
                "dimmable": dimmable,
                "enabled_default": enabled_default,
                "units": units,
                "load_units": [],
                "input_units": [],
            }

        return app

    # ------------------------------------------------------------------
    # Physical units (relays, dimmers, keypads, eDLTs, PIRs ...)
    # ------------------------------------------------------------------

    async def _discover_units(self, project: str, network: str) -> Dict[str, Any]:
        units: Dict[str, Any] = {}

        try:
            lines = await self.session.send_command(f"get //{project}/{network} Units")
        except Exception as ex:  # noqa: BLE001
            _LOGGER.warning("Unit enumeration failed: %s", ex)
            return units

        addresses: List[int] = []
        for line in lines:
            m = PARAM_LINE_RE.match(line)
            if m and m.group(1).startswith("Units="):
                addresses.extend(self._parse_int_list(m.group(1)[len("Units="):]))

        for addr in sorted(set(addresses)):
            upath = f"//{project}/{network}/p/{addr}"
            try:
                ulines = await self.session.send_command(f"get {upath} *")
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Unit %s params unavailable: %s", addr, ex)
                continue

            p = self._parse_get_params(ulines)
            unit_type = p.get("Type", "").strip()
            app = self._to_int(p.get("Application"), 56)
            role = unit_role(unit_type, p.get("ClassName"))

            name = await self._dbget_name(upath)
            if not name:
                name = p.get("Name", "").strip() or f"{unit_type or 'Unit'} {addr}"

            unit = {
                "address": addr,
                "type": unit_type,
                "catalog": p.get("CatalogNumber", "").strip() or None,
                "firmware": (p.get("Version") or p.get("FirmwareVersion") or "").strip() or None,
                "name": name,
                "role": role,
                "app": app,
                "groups": self._parse_int_list(p.get("Groups", "")),
                "slots": self._parse_slots(p, app),
                "has_light_level": "LightLevel" in p,
            }
            units[str(addr)] = unit
            _LOGGER.debug(
                "UNIT: %s type=%s role=%s name=%r groups=%s slots=%d",
                addr, unit_type, role, name, unit["groups"], len(unit["slots"]),
            )

        _LOGGER.info("Discovered %d C-Bus units", len(units))
        return units

    def _parse_slots(self, p: Dict[str, str], app: int) -> List[Dict[str, int]]:
        """Key/slot -> (app, group) mapping for input units.

        * Keypads and PIRs expose ``SlotGroups=g0,g1,...`` (one group per key
          slot, 255 = unassigned; the application is the unit's primary app).
        * Glass eDLTs expose ``WidgetGroups=a0,g0,a1,g1,...`` as (app, group)
          pairs, one per widget position.
        """
        slots: List[Dict[str, int]] = []

        if p.get("WidgetGroups"):
            vals = self._parse_int_list(p["WidgetGroups"])
            for i in range(0, len(vals) - 1, 2):
                a, g = vals[i], vals[i + 1]
                if a == UNASSIGNED or g == UNASSIGNED:
                    continue
                slots.append({"slot": i // 2, "app": a, "group": g})
            return slots

        if p.get("SlotGroups"):
            for i, g in enumerate(self._parse_int_list(p["SlotGroups"])):
                if g == UNASSIGNED:
                    continue
                slots.append({"slot": i, "app": app, "group": g})

        return slots

    @staticmethod
    def _load_types_by_group(units: Dict[str, Any]) -> Dict[tuple, List[str]]:
        """(app, group) -> unit Types of the output units driving it."""
        out: Dict[tuple, List[str]] = {}
        for unit in units.values():
            if unit.get("role") != ROLE_LOAD:
                continue
            app = int(unit.get("app", 56))
            for g in unit.get("groups", []):
                out.setdefault((app, int(g)), []).append(unit.get("type", ""))
        return out

    @staticmethod
    def _input_units_by_group(units: Dict[str, Any]) -> Dict[tuple, List[int]]:
        """(app, group) -> addresses of keypads / eDLTs / PIRs programmed to it."""
        out: Dict[tuple, List[int]] = {}
        for unit in units.values():
            if unit.get("role") not in INPUT_ROLES:
                continue
            app = int(unit.get("app", 56))
            targets = [(int(s.get("app", app)), int(s["group"])) for s in unit.get("slots", [])] \
                or [(app, int(g)) for g in unit.get("groups", [])]
            for key in targets:
                out.setdefault(key, []).append(int(unit["address"]))
        return out

    def _annotate_groups(self, net_model: Dict[str, Any]) -> None:
        """Link groups to the units that drive / control them."""
        apps = net_model.get("applications", {})
        for unit in net_model.get("units", {}).values():
            role = unit.get("role")
            if role == ROLE_LOAD:
                field = "load_units"
            elif role in INPUT_ROLES:
                field = "input_units"
            else:
                continue

            app = int(unit.get("app", 56))
            targets = [(int(s.get("app", app)), int(s["group"])) for s in unit.get("slots", [])] \
                or [(app, int(g)) for g in unit.get("groups", [])]
            for a, g in targets:
                gi = apps.get(str(a), {}).get("groups", {}).get(str(g))
                if gi is not None and unit["address"] not in gi[field]:
                    gi[field].append(unit["address"])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _dbget_name(self, path: str) -> str | None:
        try:
            rows = await self.session.send_command(f"dbget {path}")
        except Exception:  # noqa: BLE001
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

    @staticmethod
    def _parse_int_list(text: str | None) -> List[int]:
        out: List[int] = []
        for tok in (text or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                out.append(int(tok))
            except ValueError:
                pass
        return out

    @staticmethod
    def _to_int(value: str | None, default: int) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------
    # CLASSIFIER
    #   Exhaust -> Fan -> (by output unit type, ground truth) Dimmer / Relay
    #   -> keypad-only / area -> legacy name-count guess
    # Returns (device_class, is_load, dimmable)
    # ------------------------------------------------------------

    # Names that clearly denote a light — these win over _SWITCH_WORDS
    # ("Gate Floods" is a light, "Gate Motor" is not).
    _LIGHT_WORDS = (
        "light", "lamp", "led", "d/l", "downlight", "flood", "festoon",
        "uplight", "up light", "strip", "pendant", "oyster", "spot", "wall lgt",
    )

    # Relay channels whose name says they are not lights
    _SWITCH_WORDS = (
        "gate", "motor", "hot water", "hws", "outlet", "gpo", "power point",
        "pump", "irrigation", "sprinkler", "heater", "towel", "blind",
        "curtain", "door", "rsd", "roller", "shutter", "spare", "socket",
        "charger",
    )

    def _classify(
        self,
        name: str,
        units: str | None,
        load_types: List[str] | None = None,
        input_units: List[int] | None = None,
    ):

        n = (name or "").lower()

        # 1) Exhaust fan (must be FIRST)
        if "exhaust" in n or " ex fan" in f" {n}":
            return "exhaust", True, False

        # 2) Ceiling fan
        if "fan" in n:
            return "fan", True, False

        # 3) Ground truth from the output unit driving the group
        if load_types:
            types = [t.upper() for t in load_types]
            if any(t.startswith("DIM") for t in types):
                return "light", True, True          # dimmer channel
            if any(t.startswith(("REL", "RELAY")) for t in types):
                if any(w in n for w in self._LIGHT_WORDS):
                    return "light", True, False     # relay-fed light (on/off)
                if any(w in n for w in self._SWITCH_WORDS):
                    return "switch", True, False    # relay, non-light load
                return "light", True, False         # relay channel, assume light

        # 4) Unit roles are known but nothing drives this group
        if input_units is not None:
            if input_units:
                # Programmed on keypads only: a virtual / scene / flag group
                return "switch", True, False
            # Area or otherwise orphaned group -> no entity
            return "area", False, False

        # 5) No units at all -> keypad-only / virtual group
        if not units or not units.strip():
            return "keypad", False, False

        # 6) Legacy heuristic (unit types unknown): count channels
        unit_count = len([u for u in units.split(",") if u.strip()])

        if unit_count == 1:
            return "switch", True, False  # Relay
        return "light", True, True        # Dimmer
