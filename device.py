"""Unit classification and Home Assistant device-registry helpers."""
from __future__ import annotations

from typing import Any, Dict

from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DOMAIN,
    MANUFACTURER,
    ROLE_EDLT,
    ROLE_KEYPAD,
    ROLE_LOAD,
    ROLE_OTHER,
    ROLE_PIR,
    ROLE_SYSTEM,
)

# Ordered: first matching prefix of the C-Gate unit Type wins.
# KEYGL* (glass eDLT) must be tested before the generic KEY* keypads.
_TYPE_PREFIX_ROLES = (
    ("KEYGL", ROLE_EDLT),
    ("DLT", ROLE_EDLT),
    ("KEY", ROLE_KEYPAD),
    ("SENPIR", ROLE_PIR),
    ("SEN", ROLE_PIR),
    ("RELDN", ROLE_LOAD),
    ("RELAY", ROLE_LOAD),
    ("REL", ROLE_LOAD),
    ("DIMDN", ROLE_LOAD),
    ("DIM", ROLE_LOAD),
    ("PC_", ROLE_SYSTEM),
    ("SYS_", ROLE_SYSTEM),
    ("CNI", ROLE_SYSTEM),
    ("PCI", ROLE_SYSTEM),
)

# Fallback on the C-Gate Java class name when the Type code is unfamiliar.
_CLASSNAME_ROLES = (
    ("InputUnit", ROLE_KEYPAD),
    ("SENPI", ROLE_PIR),
    ("Output", ROLE_LOAD),
    ("Dimmer", ROLE_LOAD),
    ("Relay", ROLE_LOAD),
)


def unit_role(unit_type: str | None, class_name: str | None = None) -> str:
    """Classify a C-Gate unit Type (e.g. KEYE3, SENPIRIA, RELDN12) into a role."""
    t = (unit_type or "").upper()
    for prefix, role in _TYPE_PREFIX_ROLES:
        if t.startswith(prefix):
            return role
    c = class_name or ""
    for needle, role in _CLASSNAME_ROLES:
        if needle in c:
            return role
    return ROLE_OTHER


def unit_identifier(project: str, network: str, address: int) -> tuple[str, str]:
    return (DOMAIN, f"{project}_{network}_p{int(address)}")


def hub_identifier(project: str, network: str) -> tuple[str, str]:
    return (DOMAIN, f"{project}_{network}_cgate")


def hub_device_info(
    project: str, network: str, host: str | None = None, version: str | None = None
) -> DeviceInfo:
    """DeviceInfo for the C-Gate server / C-Bus network (the 'hub')."""
    return DeviceInfo(
        identifiers={hub_identifier(project, network)},
        name=f"C-Gate {project}/{network}",
        manufacturer=MANUFACTURER,
        model="C-Gate",
        sw_version=version or None,
        configuration_url=None,
    )


def unit_device_info(project: str, network: str, unit: Dict[str, Any]) -> DeviceInfo:
    """Build DeviceInfo for a physical C-Bus unit (a standalone device;
    it does not declare via_device to the hub, since some HA versions
    refuse entities carrying that field for a device created this way)."""
    address = int(unit["address"])
    unit_type = unit.get("type") or "unknown"
    catalog = unit.get("catalog")
    model = f"{unit_type} ({catalog})" if catalog else unit_type

    return DeviceInfo(
        identifiers={unit_identifier(project, network, address)},
        name=unit.get("name") or f"C-Bus unit {address}",
        manufacturer=MANUFACTURER,
        model=model,
        sw_version=unit.get("firmware") or None,
    )
