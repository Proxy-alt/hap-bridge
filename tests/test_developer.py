"""The developer-facing API: blocks, ctx.hap, the testing kit, and hap.py."""

import asyncio
import functools
import importlib.util
from pathlib import Path

import pytest

from hb import testing
from hb.api import API_VERSION

ROOT = Path(__file__).resolve().parents[1]


def run(test):
    """Run an async test without an asyncio pytest plugin."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


async def purifier_profile(ctx):
    """What an integration's hap_bridge.py looks like with blocks: no imports."""
    fan = ctx.entity("fan")
    life = ctx.entity("sensor", translation_key="filter_life")
    battery = ctx.entity("sensor", device_class="battery")
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
    purifier.char(ctx.hap.Char.TARGET_AIR_PURIFIER_STATE, watch=[fan],
                  get=lambda: ctx.hap.TargetAirPurifierState.MANUAL)
    if life:
        ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=purifier), life=life)
    if battery:
        ctx.blocks.battery(acc.service("BatteryService", linked_to=purifier), battery)


@run
async def test_profile_written_with_blocks():
    hass = testing.FakeHass()
    hass.states.set("fan.p", "on", percentage=50)
    hass.states.set("sensor.p_filter", "8")
    hass.states.set("sensor.p_battery", "15")
    result = await testing.async_describe(purifier_profile, [
        testing.entry("fan.p", supported_features=1),
        testing.entry("sensor.p_filter", translation_key="filter_life"),
        testing.entry("sensor.p_battery", device_class="battery"),
    ], hass=hass)

    result.assert_valid()
    acc = result.accessory()
    values = result.values(acc)
    assert values["AirPurifier.CurrentAirPurifierState"] == 2
    assert values["FilterMaintenance.FilterChangeIndication"] == 1
    assert values["BatteryService.StatusLowBattery"] == 1
    assert acc.source == "test"

    await result.async_write(acc, "AirPurifier", "Active", 0)
    assert hass.services.calls[-1] == ("fan", "turn_off", {"entity_id": "fan.p"})


@run
async def test_blocks_explain_mistakes():
    async def wrong_service(ctx):
        acc = ctx.accessory()
        ctx.blocks.light(acc.service("Switch"), "light.desk")

    with pytest.raises(ValueError, match="goes on a Lightbulb service, not Switch"):
        await testing.async_describe(wrong_service, [testing.entry("light.desk")])

    async def foreign_entity(ctx):
        ctx.blocks.lock(ctx.accessory().service("LockMechanism"), "lock.elsewhere")

    with pytest.raises(ValueError, match="not one of this device's entities"):
        await testing.async_describe(foreign_entity, [testing.entry("light.desk")])

    async def wrong_domain(ctx):
        ctx.blocks.fan(ctx.accessory().service("Fanv2"), "light.desk")

    with pytest.raises(ValueError, match="is a light; expected fan"):
        await testing.async_describe(wrong_domain, [testing.entry("light.desk")])


@run
async def test_batched_light_write_and_sensor_blocks():
    hass = testing.FakeHass()
    hass.states.set("light.desk", "on", brightness=255, hs_color=(10, 10))
    hass.states.set("binary_sensor.door", "on")

    async def profile(ctx):
        acc = ctx.accessory(category="LIGHTBULB")
        ctx.blocks.light(acc.service("Lightbulb", primary=True), "light.desk")
        door = "binary_sensor.door"
        ctx.blocks.sensor(acc.service(ctx.blocks.sensor_service_type(door)), door)

    result = await testing.async_describe(profile, [
        testing.entry("light.desk", capabilities={"supported_color_modes": ["hs"]}),
        testing.entry("binary_sensor.door", device_class="door"),
    ], hass=hass)
    result.assert_valid()
    acc = result.accessory()
    assert result.values(acc)["ContactSensor.ContactSensorState"] == 1
    await result.async_write_many(acc, "Lightbulb", {"On": True, "Brightness": 30, "Hue": 90})
    assert hass.services.calls[-1] == (
        "light", "turn_on", {"entity_id": "light.desk", "brightness_pct": 30, "hs_color": (90, 10)})


@run
async def test_tv_and_camera_blocks_are_standalone():
    async def profile(ctx):
        ctx.blocks.tv("media_player.tv", remote="remote.tv")
        ctx.blocks.camera("camera.door", doorbell="event.ring")

    result = await testing.async_describe(profile, [
        testing.entry("media_player.tv", device_class="tv", platform="roku"),
        testing.entry("remote.tv", platform="roku"),
        testing.entry("camera.door"),
        testing.entry("event.ring", device_class="doorbell"),
    ])
    result.assert_valid()
    tv, camera = result.accessories
    assert tv.standalone and camera.camera == "camera.door"
    assert result.ctx.unclaimed() == []


def test_ctx_exposes_version_and_hap_names():
    from hb.api import HapContext

    ctx = HapContext(testing.FakeHass(), None, [])
    assert ctx.api_version == API_VERSION == 1
    assert ctx.hap.Service.AIR_PURIFIER == "AirPurifier"
    assert ctx.hap.Char.PM2_5_DENSITY == "PM2.5Density"
    assert ctx.hap.ContactSensorState.CONTACT_NOT_DETECTED == 1
    assert ctx.runtime_data("acme") is None


def test_hap_module_is_up_to_date():
    spec = importlib.util.spec_from_file_location("gen_hap", ROOT / "scripts" / "gen_hap.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    assert (ROOT / "custom_components/hap_bridge/hap.py").read_text() == gen.render(), (
        "hap.py is stale: run `python scripts/gen_hap.py`"
    )


@run
async def test_example_profile_is_valid():
    spec = importlib.util.spec_from_file_location("example", ROOT / "examples" / "hap_bridge.py")
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)

    hass = testing.FakeHass()
    hass.states.set("fan.p", "on", percentage=50, preset_mode="auto")
    hass.states.set("sensor.p_filter", "40")
    hass.states.set("sensor.p_pm25", "20")
    result = await testing.async_describe(example.async_describe_device, [
        testing.entry("fan.p", supported_features=1),
        testing.entry("sensor.p_filter", translation_key="filter_life"),
        testing.entry("button.p_reset", translation_key="reset_filter"),
        testing.entry("sensor.p_pm25", device_class="pm25"),
    ], hass=hass)
    result.assert_valid()
    assert result.warnings() == {}
    values = result.values(result.accessory())
    assert values["AirPurifier.TargetAirPurifierState"] == 1
    assert values["AirQualitySensor.AirQuality"] == 2
    await result.async_write(result.accessory(), "FilterMaintenance", "ResetFilterIndication", 1)
    assert hass.services.calls[-1] == ("button", "press", {"entity_id": "button.p_reset"})


@run
async def test_example_shims_are_valid():
    def load(name):
        spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / "shims" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    hass = testing.FakeHass()
    hass.states.set("fan.s", "on", percentage=20, preset_mode="Auto")
    hass.states.set("sensor.s_pm25", "4")
    hass.states.set("binary_sensor.s_replace_filter", "off")
    result = await testing.async_describe(load("ikea_starkvind").async_describe_device, [
        testing.entry("fan.s", supported_features=1),
        testing.entry("sensor.s_pm25", device_class="pm25"),
        testing.entry("binary_sensor.s_replace_filter"),
    ], hass=hass)
    result.assert_valid()
    assert result.values(result.accessory())["AirPurifier.TargetAirPurifierState"] == 1

    pack = load("hap_bridge_shims")
    hass.states.set("vacuum.robo", "docked")
    hass.states.set("sensor.robo_battery", "80")
    result = await testing.async_describe(pack.SHIMS[0]["describe"], [
        testing.entry("vacuum.robo"),
        testing.entry("sensor.robo_battery", device_class="battery"),
    ], hass=hass)
    result.assert_valid()
