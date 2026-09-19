# C-Bus (C-Gate) for Home Assistant

A custom Home Assistant integration that talks to a Clipsal/Schneider **C-Bus**
lighting installation through **C-Gate**, exposing your lights, switches and
fans as native Home Assistant entities — with live updates, not polling.

Originally based on [Dave Oxley's CBUS library](https://github.com/daveoxley/cbus)
and [Scott Linton's C-Bus openHAB add-on](https://github.com/scottgl/openhab-addon-cbus).

## Features

- **Live state, not polling.** The integration holds a persistent connection
  to C-Gate's command, event and load-change ports, so light/switch/fan state
  in Home Assistant updates the instant something changes on the bus —
  whether that's you, a wall switch, or a keypad.
- **Ground-truth discovery.** On startup the integration enumerates the
  physical units on your C-Bus network (relays, dimmers, PIRs) as well as
  the lighting groups, and classifies each group by what actually drives it
  — not just its name — so dimmers, relays and switches come through
  correctly with far less guesswork.
- **Fan support.** Single-group C-Bus ceiling fans are exposed as proper
  `fan` entities with Low/Medium/High presets, mapped to the real C-Bus
  levels used by fan controllers (84 / 163 / 255).
- **PIR motion & light level (optional).** If a PIR occupancy sensor is on
  your network, it's discovered automatically as a `binary_sensor` (motion)
  and, if it has a photocell, a lux `sensor` — no configuration needed.
- **Link health.** A diagnostic `binary_sensor` shows whether the connection
  to C-Gate and the C-Bus network interface are both up. If the link drops,
  affected entities go `unavailable` instead of silently showing stale
  state, and every group's level is refreshed automatically once the link
  recovers.
- **Light transitions.** `light.turn_on` / `turn_off` honour a `transition:`
  duration, sent to C-Gate as a timed ramp (`ramp <group> <level> <time>`).
  Requires C-Gate v3.7.0 or newer.
- **Manual overrides.** An optional `cbus_overrides.json` file lets you pin
  the device class, dimmability and name of any group the automatic
  classifier gets wrong.

## Installation

1. Copy this repository's files into your Home Assistant config under:

   ```
   custom_components/cbus/
   ```

   (i.e. every `.py` file and `manifest.json` from this repo go directly
   inside a `cbus` folder under `custom_components`.)

2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration**, search for
   **C-Bus (C-Gate)**, and enter your connection details (see below).

## Configuration

Configuration is done entirely through the UI (config flow) — there is no
YAML to edit. You'll be asked for:

| Field | Default | Notes |
|---|---|---|
| Host | — | IP address or hostname of your C-Gate server |
| Project | — | C-Gate project name |
| Network | `254` | C-Bus network number |
| Command port | `20023` | C-Gate command port |
| Event port | `20024` | C-Gate event port |
| Status port | `20025` | C-Gate load-change/status port |

The integration tests the connection (and that the named project can be
selected) before the entry is created.

## How groups are classified

Discovery reads the physical units on the network first, so it knows which
relay or dimmer channel actually drives each lighting group. Classification
then goes, in order:

1. Name contains `exhaust` (or `ex fan`) → `switch`, with an exhaust-fan
   icon.
2. Name contains `fan` → `fan` entity (Low / Medium / High presets).
3. Driven by a **dimmer** unit (`DIM*`) → `light`, dimmable.
4. Driven by a **relay** unit (`REL*`/`RELAY*`) → `light` (on/off), unless
   the name is clearly a non-light load (`gate`, `motor`, `hot water`,
   `pump`, `door`, `roller`, `blind`, `outlet`, `charger`, `irrigation`,
   `sprinkler`, `heater`, `towel`, `socket`, …), in which case it becomes a
   `switch`. Light words (`light`, `lamp`, `flood`, `downlight`, `led`,
   `pendant`, `oyster`, `spot`, …) always win over the switch words — so
   "Gate Floods" comes through as a light, and "Gate Motor" as a switch.
5. Programmed on keypads/PIRs only, with no relay or dimmer behind it →
   `switch` (a virtual / scene / flag group).
6. `Type=area` groups, and groups with no units on them at all, get no
   entity.

Groups whose name contains "Spare" still get an entity, but it's **disabled
by default** so unused channels don't clutter your entity list.

Only the default lighting application (56) is scanned — this integration
targets a standard single-application C-Bus lighting setup.

## Manual overrides (optional)

If the automatic classifier gets a group wrong, pin the correct answer by
creating **`/config/cbus_overrides.json`**:

```json
{
  "1":  { "device_class": "light",  "dimmable": true,  "name": "Kitchen Downlights" },
  "12": { "device_class": "light",  "dimmable": false, "name": "Pantry" }
}
```

- Keys are C-Bus **group numbers** on the lighting application.
- `device_class` is one of `light`, `switch`, `fan`.
- `dimmable` controls whether a light exposes brightness or is on/off only.
- `name` (optional) overrides the Toolkit tag name shown in Home Assistant.

**When this file is present it becomes authoritative:** only the groups
listed in it are created as entities. Remove the file to fall back to
automatic name/unit classification for everything.

## PIR motion & light level sensors

If a PIR occupancy sensor is present on your network, it's picked up by
discovery automatically — no configuration required. A C-Bus PIR doesn't
send a distinct "motion" signal of its own; it's programmed to switch a
lighting group directly. C-Gate tags every group change with the physical
unit that caused it, so the integration uses that to derive:

- **`binary_sensor.<pir>_motion`** — turns **on** when the PIR itself
  switches one of its groups on, and **off** when that group returns to 0
  (whether from the PIR's own timeout, a keypad, or Home Assistant).
- **`sensor.<pir>_light_level`** — the PIR's ambient light reading (lux),
  for PIR models with a built-in photocell. Polled every 2 minutes.

Both entities attach to a device for that physical PIR unit, alongside its
address and unit type.

## Link health & resilience

A diagnostic **`binary_sensor.<network>_link`** reports whether the C-Gate
command connection is up *and* the C-Bus network interface is actually
running — C-Gate will keep answering on the command port even if the
network interface itself has closed, so this checks both. While the link is
down, all C-Bus entities report `unavailable` rather than holding their last
known state. Once the link recovers (a reconnect, or the network interface
reopening), every load group's level is automatically re-read so nothing is
left stale.

## Credits

- [Dave Oxley](https://github.com/daveoxley) — original CBUS Python library
- Scott Linton — C-Bus openHAB add-on, the reference this integration's
  C-Gate protocol handling was originally modelled on
