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
     as switches. Restart and update buttons are left out; an identify
     button runs when you tap **Identify Accessory** in the Home app.
   - selects and input selects as one switch per option
   - doorbell and button `event` entities
4. **Finishers** that add to what's been built: a battery level sensor adds a
   linked Battery service, and low-battery, tamper and problem sensors set
   the status flags on the device's sensor tiles.

Profiles are checked against HAP-python's service/characteristic catalogue at
startup; an invalid accessory is skipped with a log line naming the problem.

## Writing a profile

An integration adds `hap_bridge.py` next to its other platforms
(`custom_components/<domain>/` or `homeassistant/components/<domain>/`).
[examples/hap_bridge.py](examples/hap_bridge.py) is a commented starting
point.

```python
HAP_BRIDGE_API = 1  # optional: the API version this profile needs

async def async_describe_device(ctx) -> None:
    fan = ctx.entity("fan")
    filter_life = ctx.entity("sensor", translation_key="filter_life")
    if fan is None:
        return

    acc = ctx.accessory(category="AIR_PURIFIER")
    purifier = ctx.blocks.fan(acc.service(ctx.hap.Service.AIR_PURIFIER, primary=True), fan)
    purifier.char(
        ctx.hap.Char.CURRENT_AIR_PURIFIER_STATE,
        watch=[fan],
        get=lambda: ctx.hap.CurrentAirPurifierState.PURIFYING_AIR if ctx.is_on(fan)
        else ctx.hap.CurrentAirPurifierState.INACTIVE,
    )
    if filter_life:
        ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=purifier), life=filter_life)
```

**Nothing to import.** Everything comes in through `ctx`, so core integrations
can ship a profile, and it's harmless when HAP Bridge isn't installed. Import
`HapContext` under `TYPE_CHECKING` if you want types.

**Blocks: `ctx.blocks`.** These are the bridge's own tested mappings. Each
binds an entity onto a service you created and returns it. Put a block on the
wrong service or entity and it raises an error saying what to fix.

| Block | Service |
|---|---|
| `light` (incl. color and Adaptive Lighting) | Lightbulb |
| `switch` | Switch / Outlet |
| `fan` | Fanv2 / AirPurifier |
| `thermostat`, `water_heater` | Thermostat |
| `heater_cooler` | HeaterCooler |
| `humidifier` | HumidifierDehumidifier |
| `lock` | LockMechanism |
| `window_covering` | WindowCovering |
| `garage_door` | GarageDoorOpener |
| `security_system` | SecuritySystem |
| `valve` | Valve |
| `vacuum`, `lawn_mower`, `momentary` | Switch |
| `doorbell`, `button` | Doorbell / StatelessProgrammableSwitch |
| `sensor` (any sensor or binary sensor HomeKit has a service for) | see `sensor_service_type` |
| `air_quality(pm25=, pm10=, voc=, ...)` | AirQualitySensor |
| `filter(life=, change=, reset=)` | FilterMaintenance |
| `battery(level, low=, charging=)` | BatteryService |

`tv(media_player, remote=)`, `activity_remote(remote)` and
`camera(camera, doorbell=, motion=)` build whole standalone accessories.
`climate_service_type(climate)` says whether core HomeKit's rule makes it a
Thermostat or a HeaterCooler.

**Names: `ctx.hap`.** Service, characteristic and value names, generated from
HAP-python's catalogue (`scripts/gen_hap.py`). For example
`ctx.hap.Service.AIR_PURIFIER`, `ctx.hap.Char.ACTIVE`, or
`ctx.hap.ContactSensorState.CONTACT_NOT_DETECTED` instead of `1`.

**Binding characteristics directly**, for anything the blocks don't cover:
- `get` reruns whenever a `watch`ed entity changes, and its result is pushed
  to HomeKit. Returning `None` skips the update.
- `set` receives the HomeKit value when the Home app writes. It can return a
  coroutine; `ctx.call(...)` does exactly that.
- `get`/`set` don't have to involve entities. `ctx.runtime_data("<your
  domain>")` returns your config entry's `runtime_data` (coordinator, client)
  to talk to the device directly.
- Watching an entity claims it, so the bridge's own mapping won't expose it
  again.
- `svc.on_write(fn)` gets a whole Home app write to a service at once
  (`{characteristic: value}`, e.g. On + Brightness + Hue), so it can make one
  call.
- `affects_available=False` on a characteristic keeps its entities out of the
  "No Response" check (for side details like battery or tamper flags).
- `ctx.accessory(key=...)` decides the AID. The default (the device ID) is
  stable; only override it with something equally stable.
- `ctx.accessory(standalone=True)` publishes the accessory with its own
  pairing code instead of behind the bridge.

### Checking a profile

- **Preview.** In Developer tools → Actions, run **HAP Bridge: Preview**
  (`hap_bridge.preview`) with a device or entity. It runs the same steps as a
  bridge start and returns:
  - each accessory: which step built it (`integration:<domain>`,
    `profile:<name>` or `fallback`), its services and current values, and
    validation errors or warnings;
  - entities left out, and why;
  - problems with your profile.

  Nothing is paired or changed, and there's no restart needed between edits.
- **Repair issues.** If your profile raises, can't be imported, builds an
  accessory HomeKit would reject, or needs a newer `HAP_BRIDGE_API`, users get
  a repair issue naming your integration and the problem.
  - What your profile built is undone, so the device still shows up through
    the bridge's own mapping.
  - The issue clears on the next start without problems.
- **Diagnostics** list the same things for the whole bridge: the source of
  each accessory, excluded entities with reasons, and profile problems.
- **Tests.** `custom_components.hap_bridge.testing` runs your profile against
  fake states, with no Home Assistant or Home app:

  ```python
  from custom_components.hap_bridge import testing

  async def test_purifier():
      hass = testing.FakeHass()
      hass.states.set("fan.p", "on", percentage=50)
      result = await testing.async_describe(hap_bridge.async_describe_device,
                                            [testing.entry("fan.p")], hass=hass)
      result.assert_valid()  # what the bridge would reject at startup
      acc = result.accessory()
      assert result.values(acc)["AirPurifier.Active"] == 1
      await result.async_write(acc, "AirPurifier", "Active", 0)
      assert hass.services.calls[-1][:2] == ("fan", "turn_off")
  ```

### API versions

`ctx.api_version` is the bridge's API version (now 1). It goes up only for
incompatible changes. Set `HAP_BRIDGE_API` to the version your profile needs.
An older bridge then skips the profile with a repair issue instead of breaking
on it.

## Shims: profiles for integrations you don't maintain

Most integrations don't ship a `hap_bridge.py` yet. A **shim** is the same
kind of profile, written by you or the community, for someone else's
integration or for specific device models. It uses the same
`async_describe_device(ctx)`, blocks, `ctx.hap`, preview, test kit and repair
issues, so a good shim can later be copied into the integration unchanged.

```python
# <config>/hap_bridge/profiles/ikea_starkvind.py
HANDLES = {"integration": "zha", "manufacturer": "IKEA*", "model": "STARKVIND*"}

async def async_describe_device(ctx):
    ...
```

**Which devices.** `HANDLES` patterns are case-insensitive, with `*` and `?`
as wildcards, and every key given must match. It can also be a list of these.
Without `HANDLES`, a shim handles the integration it's named after (`acme.py`
→ the `acme` integration). Matching by manufacturer and model is what makes
shims useful for Zigbee, Z-Wave, Matter and MQTT devices, where one
integration covers thousands of products.

**Where shims come from.**
- **Files** in `<config>/hap_bridge/profiles/*.py`. They're re-read on every
  bridge start and every preview, so edit → preview → reload is quick, with no
  restart. They're Python run by Home Assistant, the same trust as anything
  in `custom_components`: only add files you'd trust there. See
  [examples/shims/ikea_starkvind.py](examples/shims/ikea_starkvind.py).
- **Profile packs.** Any integration (for example a community collection
  from HACS) can ship a `hap_bridge_shims.py` with a `SHIMS` list of `{"name",
  "handles", "describe"}` entries. See
  [examples/shims/hap_bridge_shims.py](examples/shims/hap_bridge_shims.py).
  A pack set up after the bridge starts makes the bridge reload to use it.

**What runs.** Per device, the most specific matching shim runs before
everything else, including the integration's own profile:
- A model match beats a manufacturer match, which beats an integration match.
- If two shims match equally, neither runs, and both get a repair issue
  saying to make one `HANDLES` more specific.
- Whatever a shim leaves unclaimed goes on to the integration's profile, the
  bundled profiles and the fallback.
- Accessories it builds show as `shim:<name>` in previews and diagnostics.

**When a shim breaks.** If a shim raises, fails to load, has bad `HANDLES`,
or builds something HomeKit would reject, what it built is undone and the
device falls back to the next step. A repair issue names the shim and its file
or pack, and clears on the next clean start.

**Keep HomeKit IDs stable.** HomeKit IDs follow accessory keys. Leave
`ctx.accessory()`'s default key (the device ID) unless you must change it.
Otherwise the Home app sees a new accessory, and rooms and automations
pointing at the old one stop working.

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
- Sound from the camera reaches the Home app as Opus when audio is turned
  on for it: in the options (**Camera audio**), or by a profile with
  `ctx.blocks.camera(camera, audio=True)`. It's off by default because
  ffmpeg won't play the video either if it's asked for audio the stream
  doesn't have.
- Talking back through the camera isn't supported, since Home Assistant has
  no way to send audio to a camera.

### HomeKit Secure Video

A camera with a motion sensor or a doorbell also offers HomeKit Secure Video.
Turn it on in the camera's settings in the Home app. You need a home hub
(Apple TV, HomePod or iPad) and an iCloud+ plan.

- When a trigger fires, the hub asks for a clip. The bridge records the
  camera's stream with ffmpeg as fragmented MP4 (H.264, plus AAC sound if
  the camera has audio on and the Home app records audio) and sends it over
  a HomeKit Data Stream.
- A clip runs until the hub has enough. As a fallback, the bridge ends it 10
  seconds after the triggers clear, or after 5 minutes.
- The hub analyses the clips itself, for people, animals, vehicles and
  packages. A camera can't tell it what it saw, only what starts a recording.
- While recording is on in the Home app, ffmpeg keeps running and holds the
  last few seconds, so a clip starts 4–8 seconds before its trigger. That
  costs one ffmpeg per recording camera, running all the time. On a slow
  machine, pick cameras under **Cameras → Cameras that record without a
  prebuffer** in the options. Their ffmpeg starts when the hub asks, and the
  clip begins a second or two after the trigger.
- If the Home app turns the camera off, or turns off its periodic or event
  snapshots, those requests are refused, not just hidden.
- The protocol isn't published by Apple. This follows HAP-NodeJS's
  reverse-engineered `RecordingManagement` and `DataStreamServer`; see
  `recording.py` and `hds.py`.

By default the camera's motion sensor and its doorbell start recordings.
A profile can choose other entities, for example the person and vehicle
sensors many cameras have:

```python
ctx.blocks.camera(
    "camera.driveway",
    motion="binary_sensor.driveway_motion",
    recording={
        "motion": ["binary_sensor.driveway_person", "binary_sensor.driveway_vehicle"],
        "doorbell": None,
    },
)
```

- A recording starts when any of the `motion` entities turns on. The
  camera's Motion Sensor in the Home app follows them, since that's what the
  hub watches.
- If only people and vehicles start recordings, choosing "animals" in the
  Home app won't catch anything, because the hub only analyses what gets
  recorded.
- `recording=False` turns HomeKit Secure Video off for that camera. The
  preview shows each camera's triggers.

The camera's settings in the Home app can also show **Night Vision** and
**Status Light**, and show the camera as manually turned off while a privacy
switch is on. The bundled profile finds these switches by their translation
keys: `ir_lights`, `infrared`, `night_vision` and similar; `status_light` and
`status_led`; `privacy_mode`. A profile can pass them as
`night_vision=`, `status_light=` and `privacy=`. They're part of the
recording service, so they only appear on cameras that can record.

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

The bridge offers it on every Lightbulb that has Brightness,
ColorTemperature and an `on_write`, when ColorTemperature watches a light
that supports color temperature, so profiles get it without asking. That
includes ones integrations and shims build by hand. A Lightbulb with
per-characteristic setters and no `on_write` can't offer it; the preview
warns about that. A profile can:

- name the light, when ColorTemperature doesn't watch it:
  `svc.adaptive_lighting("light.desk")`;
- opt out: `svc.adaptive_lighting(enabled=False)`.

It's only offered. Each light stays on its normal color until someone turns
Adaptive Lighting on for it in the Home app.

## Rooms

HomeKit has no way for an accessory to say which room it's in. Rooms live in
the Home app, which asks you to pick one for each accessory when you add the
bridge. To match your Home Assistant areas instead, use
`scripts/assign_rooms.py` with [HomeClaw](https://github.com/omarshahine/HomeClaw),
a free Mac app with HomeKit access:

```bash
export HA_TOKEN=...   # a long-lived access token from your HA profile page
python3 scripts/assign_rooms.py --url http://homeassistant.local:8123          # preview
python3 scripts/assign_rooms.py --url http://homeassistant.local:8123 --apply  # move them
```

- The `hap_bridge.room_assignments` action lists each accessory with the area
  it belongs in. An entity's own area wins over its device's.
- Each accessory has its own serial number (`HB-` plus a hash of the bridge
  and the accessory's stable key). The script reads it back through HomeClaw,
  so accessories are found even if you've renamed them in the Home app.
  A device's real serial number isn't used, because the same device may also
  be in HomeKit natively.
- HomeClaw creates rooms that don't exist yet and leaves accessories that are
  already in the right room alone. Accessories with no area, or not found in
  the Home app, are listed and left where they are.
- It only runs when you run it, for example after pairing or adding devices.
  HomeClaw must be running on the Mac and signed in to your home. It gets full
  access to your home, so only use it if you're happy with that.

## Accessory settings (for a Home app extension)

A device's own settings, like LED brightness, power-on behaviour or motion
sensitivity, and its details, like firmware or signal strength, have no
place in HomeKit, so the bridge leaves those entities out. Apple's Home
Extensions framework (iOS 27.2 and later) lets an app add settings pages to
an accessory in the Home app. Apple says it's "available at compile time
only" for now. These two actions are the Home Assistant side such an
extension would call:

- `hap_bridge.accessory_settings` takes an accessory's serial number (the
  `HB-…` one an extension reads from its `HMAccessory`). It returns the
  device's configuration entities as `settings` and its diagnostic entities
  as `info`, along with the manufacturer, model, firmware and the device's
  real serial number. Each setting's `kind` matches a Home Extensions setting:
  - `toggle`: a switch
  - `choice`: a select, with its options
  - `range`: a number, with its minimum, maximum, step and unit
  - `text`: a text entity
  - `info`: a read-only value

  Buttons come through as `action`, which a custom view would show as a
  button.
- `hap_bridge.set_accessory_setting` changes one of them. It only accepts
  entities that `accessory_settings` lists for that accessory, so an
  extension can't use it to control anything else.

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

The options have three parts:

- **What to expose:** only some integrations or entity domains, minus
  excluded devices and entities.
- **Rename an accessory:** give an accessory a different name in HomeKit.
  Leave the name empty to go back to the profile's. HomeKit takes the name
  when the accessory is first added; after that the Home app keeps its own,
  so rename accessories you've already added there. The preview shows the
  original as `default_name`.
- **Cameras:** which cameras send sound, and which record without a
  prebuffer (see above).

Adding, removing or renaming devices and entities in Home Assistant rebuilds
the bridge after 30 seconds of quiet.

An accessory shows **No Response** in the Home app when the entities its
primary service watches are unavailable. A profile can change that rule with
`acc.available = lambda: ...`.

Download diagnostics on the integration to see each accessory as HomeKit sees it:
AIDs, services, IIDs, values and the entities it watches.

**Paired controllers:** the `hap_bridge.list_pairings` action lists the
controllers paired with the bridge and with each TV and camera, and which
are admins. `hap_bridge.remove_pairing` removes one, for example a phone you
no longer have, without resetting everything. When the last one goes, the
pairing code shows again.

**Other details:**

- **Tile names:** when one accessory has several tiles (a power strip's
  outlets, a fan's light), each gets its entity's name, without the device
  name in front, rather than "Strip 1", "Strip 2". Renaming a tile in the
  Home app is remembered across restarts.
- **Timed writes:** unlocking a lock, opening a garage door and changing an
  alarm's mode use HAP timed writes, which can't be replayed.

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
- **Cameras have no talk-back.** HomeKit Secure Video hasn't been tried with a real home hub yet.
- **Plain speakers aren't exposed.** HomeKit has no useful service for them (only AirPlay speakers show up properly).
- **No Matter output.** Robot vacuums need Matter, because HAP has no vacuum service.
- **Reloads restart the whole bridge.** Controllers reconnect after a device is added or removed.
