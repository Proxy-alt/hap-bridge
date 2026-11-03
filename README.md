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
   Today:
   - TVs and receivers, and activity remotes such as Harmony, as Televisions
     (see below)
   - cameras, as video doorbells when the device has a doorbell, with the
     device's motion sensor (see below)
   - air purifier, with filter life, a "replace filter" sensor and a
     "reset filter" button if the device has them
   - ceiling fan with light
   - climate: AC-style units as heater/coolers; others as thermostats, with
     the device's humidity sensor folded in
   - humidifier, with the device's humidity sensor
   - doorbell, with the device's motion sensor linked
   - garage door, with the device's safety/problem sensor as obstruction
   - air quality: all of a device's pollutant sensors in one tile, rated by
     the worst
   - multi-button remotes: one accessory with a numbered switch per button
3. **Fallback**: per-entity mapping similar to core HomeKit Bridge:
   - lights, with brightness, hue/saturation and color temperature, plus
     Adaptive Lighting for lights with color temperature (see below)
   - switches and outlets
   - fans, with oscillation, direction and an auto preset
   - temperature, humidity, light level, CO and CO2 sensors
   - single pollutant sensors as air quality
   - binary sensors, including CO and gas
   - locks
   - covers, with tilt; garage doors become garage door openers
   - climate: AC-style units (fan speeds or swing, no humidity target, as in
     core HomeKit) as heater/coolers, everything else as thermostats
   - humidifiers and dehumidifiers
   - alarm panels as security systems, offering only the arm modes they
     support. Panels that need a code can't be armed or disarmed from Home,
     which has nowhere to enter one.
   - valves, and water heaters as heat-only thermostats
   - vacuums and lawn mowers as switches: on starts, off sends them home
   - scenes and buttons as switches that turn themselves back off; scripts
     as switches. Restart, update and identify buttons are left out.
   - selects and input selects as one switch per option
   - doorbell and button `event` entities
4. **Finishers** that add to what's been built: a battery level sensor adds a
   linked Battery service, and low-battery, tamper and problem sensors set
   the status flags on the device's sensor tiles.

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
- `svc.on_write(fn)` handles a whole controller write to a service at once:
  `fn` gets `{characteristic: value}` for everything the Home app changed
  together, e.g. On + Brightness + Hue, so it can make one service call.
- `affects_available=False` on a characteristic keeps its entities out of
  the "No Response" check (for side details like battery or tamper flags).
- `ctx.accessory(key=...)` decides the AID. The default (the device ID) is
  stable; only override it with something equally stable.

## TVs and cameras

TVs, receivers, activity remotes and cameras are **standalone accessories**.
HomeKit expects them to be paired on their own rather than behind a bridge,
the same way core HomeKit and Homebridge publish them. Each one gets:
- its own port, kept across restarts;
- its own pairing notification with a QR code;
- its own **Reset pairing** button on the bridge's device.

Add each one in the Home app the same way you added the bridge.

**TVs** (media players with the `tv` or `receiver` device class, or from a TV
integration below) get:
- power, inputs (the source list, including apps) and the current input;
- play/pause state;
- volume: the iPhone's volume buttons while the Remote is open, mute, and a
  volume level where the TV supports one;
- the **Remote** in Control Center: arrows, select, back, exit, info,
  rewind/fast-forward and play/pause. Keys go to the TV's own remote entity
  (or `webostv.button` for LG) for Android TV, Apple TV, Roku, Samsung,
  Philips and LG. Play/pause always goes through the media player.
  - Every key press also fires a `hap_bridge_tv_remote_key` event (`entity_id`,
    `key`, `handled`), so automations can handle keys for other TVs.
- **View TV Settings** in the Home app opens the TV's settings or menu, where
  the integration has a key for it.
- names you give the TV and its inputs in the Home app, and inputs you hide
  there, are kept across restarts.

Activity remotes (e.g. Harmony) work the same way, with their activities as
the inputs.

**Cameras** stream live video to the Home app and provide snapshots. They use
the camera's stream source with ffmpeg, as core HomeKit does.
- If the device also has a doorbell `event` entity, the camera becomes a video
  doorbell, and rings show on your devices with a snapshot.
- If the device has a motion binary sensor, it's added for motion
  notifications.
- Video only for now: no audio, and no HomeKit Secure Video recording.

## Adaptive Lighting

Lights with color temperature offer Adaptive Lighting in the Home app. HomeKit
bulbs run the schedule themselves, so here the bridge does it for them:

- It works out the color temperature from the schedule and the light's
  brightness, and sends it with `light.turn_on` at the interval the Home app
  asks for (usually every minute).
- An off light is skipped. Home Assistant can't change its color without
  turning it on, so it gets the right temperature as it turns on. When the
  Home app turns it on, that happens in the same call; when something else
  does, it's corrected straight after.
- Choosing a color or white by hand switches Adaptive Lighting off, the same
  as on a real bulb. This applies in the Home app or anywhere in Home
  Assistant (the dashboard, automations, a wall switch).
- The schedule is saved, so restarts and reloads pick it up again.

The protocol isn't published by Apple. This follows HAP-NodeJS's
reverse-engineered `AdaptiveLightingController`; see `transition.py`.

A profile turns it on with `svc.adaptive_lighting(light_entity_id)` on a
Lightbulb that has Brightness, ColorTemperature and an `on_write`.

## Stable IDs

Home keys rooms, scenes and automations on AID/IID. AIDs are keyed on the
device ID (or entity registry ID for fallback accessories). IIDs are keyed on
service type + service unique ID + characteristic type. Both are persisted
in `.storage/hap_bridge.<entry>.ids`. Adding a characteristic or service
later never renumbers existing ones (see `tests/test_ids.py`).

## Install

Copy `custom_components/hap_bridge` into your HA config (or add this repo to
HACS as a custom repository), restart, add **HAP Bridge** in Settings →
Devices & services, then pair by scanning the QR code in the notification (or
typing its code). The bridge's **Reset pairing** button removes every paired
Home app so you can add it again.

The options choose what to expose: only some integrations or entity domains,
minus excluded devices and entities. Adding, removing or renaming devices and
entities rebuilds the bridge after 30 seconds of quiet.

An accessory shows **No Response** in the Home app when the entities its
primary service watches are unavailable. A profile can change that rule with
`acc.available = lambda: ...`.

Download diagnostics on the integration to see each accessory as HomeKit sees it:
AIDs, services, IIDs, values and the entities it watches.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

`tests/` covers ID stability with real HAP-python objects and validates every
bundled profile against HAP-python's catalogue. It doesn't need Home Assistant.

`tests_ha/` runs the bridge inside a real Home Assistant (with the HAP server
stubbed out): exposure, writes, No Response, pairing, filters and reloads.

```bash
python3 -m venv .venv-ha && .venv-ha/bin/pip install -e '.[test-ha]'
.venv-ha/bin/python -m pytest tests_ha -o asyncio_mode=auto
```

## Not done yet

- **Not yet paired with a real Home app.** The HA tests stub out the HAP server. That matters most for Adaptive Lighting, whose protocol is reverse-engineered.
- **Bridges are capped at 149 accessories.** There's no automatic splitting across bridges yet.
- **Only one request at a time is batched.** `on_write` gets everything in a single Home app request. Core HomeKit also merges changes that arrive a few milliseconds apart, and this bridge doesn't yet.
- **Cameras have no audio and no HomeKit Secure Video.** Live video and snapshots only.
- **Plain speakers aren't exposed.** HomeKit has no useful service for them (only AirPlay speakers show up properly).
- **No Matter output.** Robot vacuums need Matter, because HAP has no vacuum service.
- **Reloads restart the whole bridge.** Controllers reconnect after a device is added or removed.
