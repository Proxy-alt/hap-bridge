# HAP Bridge

A HomeKit bridge for Home Assistant where **integrations describe their own
devices at the HAP level**: services, characteristics, props, primary and
linked services. The bridge handles pairing, transport and stable IDs.

The core HomeKit Bridge maps one entity to one accessory by domain. A purifier
shows up as a fan plus three loose sensors; a ceiling fan as two tiles. Here a
purifier is one `AirPurifier` accessory with linked `FilterMaintenance` and
`AirQualitySensor` services, because something that knows the device said so.

## How a device gets exposed

For each device, in order, each step seeing only what earlier steps left:

1. **The integration's own `hap_bridge.py`**, if it ships one.
2. **Bundled profiles** in `profiles/generic.py`: matched on device shape
   (e.g. "one fan + a filter-life sensor = purifier") or per integration.
3. **Fallback**: per-entity mapping similar to core HomeKit Bridge.

Profiles are checked against HAP-python's service/characteristic catalogue at
startup; an invalid accessory is skipped with a log line naming the problem.

## Writing a profile

An integration adds `custom_components/<domain>/hap_bridge.py` (or
`homeassistant/components/<domain>/hap_bridge.py`):

```python
from custom_components.hap_bridge.api import HapContext

async def async_describe_device(ctx: HapContext) -> None:
    fan = ctx.entity("fan")
    filter_life = ctx.entity("sensor", translation_key="filter_life")
    if fan is None:
        return

    acc = ctx.accessory(category="AIR_PURIFIER")
    purifier = acc.service("AirPurifier", primary=True)
    purifier.char(
        "Active",
        watch=[fan],
        get=lambda: int(ctx.is_on(fan)),
        set=lambda v: ctx.call("fan", "turn_on" if v else "turn_off", fan),
    )
    purifier.char("RotationSpeed", watch=[fan], props={"minStep": 25},
                  get=lambda: ctx.attr(fan, "percentage"),
                  set=lambda v: ctx.call("fan", "set_percentage", fan, percentage=v))
    ...
    if filter_life:
        acc.service("FilterMaintenance", linked_to=purifier).char(
            "FilterLifeLevel", watch=[filter_life], get=lambda: ctx.number(filter_life))
```

- Names are raw HAP names (`pyhap/resources/services.json`,
  `characteristics.json`). `props` and `valid_values` are raw HAP props.
- `get` reruns whenever a `watch`ed entity changes, and its result is pushed to
  HomeKit. Returning `None` skips the update. Values are HAP values (e.g.
  `Active` is `0/1`).
- `set` receives the HAP value when a controller writes. It can return a
  coroutine. `ctx.call(...)` does exactly that.
- `get`/`set` don't have to involve HA entities at all. They can talk to the
  device directly.
- Watching an entity claims it, so the fallback won't expose it again.
- `ctx.accessory(key=...)` decides the AID. The default (the device ID) is
  stable; only override it with something equally stable.

## Stable IDs

Home keys rooms, scenes and automations on AID/IID. AIDs are keyed on the
device ID (or entity registry ID for fallback accessories). IIDs are keyed on
service type + service unique ID + characteristic type. Both are persisted
in `.storage/hap_bridge.<entry>.ids`. Adding a characteristic or service
later never renumbers existing ones (see `tests/test_ids.py`).

## Install

Copy `custom_components/hap_bridge` into your HA config (or add this repo to
HACS as a custom repository), restart, add **HAP Bridge** in Settings →
Devices & services, then pair using the code from the notification. Use the
options to limit which integrations are exposed.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

Tests cover ID stability with real HAP-python objects and validate every
bundled profile against HAP-python's catalogue. They don't need Home Assistant.

## Not done yet

- **Not yet run inside a real Home Assistant.** Only the HA-free parts are tested.
- **Bridges are capped at 149 accessories.** There's no automatic splitting across bridges yet.
- **Writes aren't batched.** Home sends `On` + `Brightness` together, and that makes two service calls.
- **No standalone (non-bridged) accessory mode** for cameras and TVs.
- **No Matter output.** Robot vacuums need Matter, because HAP has no vacuum service.
- **No accessory reload without restart.** Changing entities means reloading the entry.
