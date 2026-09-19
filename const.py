"""Constants for the C-Bus (C-Gate) integration."""

DOMAIN = "cbus"

CONF_HOST = "host"
CONF_PROJECT = "project"
CONF_NETWORK = "network"

CONF_PORT_CMD = "port_cmd"
CONF_PORT_EVENT = "port_event"
CONF_PORT_STATUS = "port_status"

DEFAULT_NETWORK = "254"
DEFAULT_PORT_CMD = 20023
DEFAULT_PORT_EVENT = 20024
DEFAULT_PORT_STATUS = 20025

MANUFACTURER = "Clipsal / Schneider Electric"

# Roles assigned to physical C-Bus units by discovery (see device.py).
# KEYPAD/EDLT are classified for forward-compatibility (so a future keypad
# or eDLT "just works" for classification purposes) even though no entity
# platform currently uses them.
ROLE_LOAD = "load"        # relay / dimmer output units
ROLE_KEYPAD = "keypad"    # Saturn / Neo / classic key input units
ROLE_EDLT = "edlt"        # glass eDLT / DLT display keypads
ROLE_PIR = "pir"          # occupancy / PIR sensors
ROLE_SYSTEM = "system"    # CNI / PCI / NAC / SHAC etc.
ROLE_OTHER = "other"

# Roles that can "originate" a group change (sourceunit=N) and are treated
# as candidates for input-only / virtual groups by the classifier.
INPUT_ROLES = (ROLE_KEYPAD, ROLE_EDLT, ROLE_PIR)

# Fired on the HA event bus whenever an input unit (currently: PIR) is the
# unit that originated a group change on the bus.
EVENT_CBUS_UNIT_EVENT = "cbus_unit_event"

ATTR_APP = "app"
ATTR_GROUP = "group"
ATTR_GROUP_NAME = "group_name"
ATTR_LEVEL = "level"
ATTR_SLOT = "slot"
ATTR_UNIT = "unit"
ATTR_UNIT_NAME = "unit_name"
ATTR_UNIT_TYPE = "unit_type"
ATTR_LAST_MOTION = "last_motion"
