from hb.api import HapContext
from hb.profiles import fallback, generic
from hb.validate import validate_spec

from .conftest import make_device, make_entry


def _run(ctx: HapContext) -> None:
    for describe in generic.PROFILES:
        describe(ctx)
    for ent in ctx.unclaimed():
        fallback.describe_entity(ctx, ent)


def _assert_valid(ctx: HapContext, loader) -> None:
    for spec in ctx.accessories:
        errors, _ = validate_spec(spec, loader)
        assert not errors, (spec.name, errors)


def _purifier_ctx(hass) -> HapContext:
    entries = [
        make_entry("fan.purifier", supported_features=fallback.FAN_SET_SPEED),
        make_entry("sensor.purifier_filter_life", translation_key="filter_life",
                   unit_of_measurement="%", entity_category="diagnostic"),
        make_entry("sensor.purifier_pm25", device_class="pm25"),
        make_entry("sensor.purifier_temperature", device_class="temperature"),
    ]
    hass.states.set("fan.purifier", "on", percentage=60, preset_mode="Auto",
                    preset_modes=["Auto", "Sleep"])
    hass.states.set("sensor.purifier_filter_life", "7")
    hass.states.set("sensor.purifier_pm25", "40")
    hass.states.set("sensor.purifier_temperature", "70", unit_of_measurement="°F")
    return HapContext(hass, make_device(), entries)


def test_purifier_groups_into_one_accessory(hass, loader):
    ctx = _purifier_ctx(hass)
    _run(ctx)
    _assert_valid(ctx, loader)

    purifier, temp = ctx.accessories
    assert purifier.key == "device:dev1"
    assert [s.type_name for s in purifier.services] == [
        "AirPurifier", "FilterMaintenance", "AirQualitySensor"
    ]
    primary = purifier.services[0]
    assert primary.primary and len(primary.linked) == 2

    chars = {f"{s.type_name}.{n}": c for s in purifier.services for n, c in s.chars.items()}
    assert chars["AirPurifier.Active"].get() == 1
    assert chars["AirPurifier.RotationSpeed"].get() == 60
    assert chars["AirPurifier.TargetAirPurifierState"].get() == 1
    assert chars["FilterMaintenance.FilterChangeIndication"].get() == 1
    assert chars["AirQualitySensor.AirQuality"].get() == 3

    # The temperature sensor wasn't claimed, so the fallback exposes it (in °C).
    assert temp.key == "entity:reg-sensor.purifier_temperature"
    assert round(temp.services[0].chars["CurrentTemperature"].get(), 1) == 21.1


def test_purifier_writes_call_services(hass):
    ctx = _purifier_ctx(hass)
    _run(ctx)
    target = ctx.accessories[0].services[0].chars["TargetAirPurifierState"]
    target.set(1)
    target.set(0)
    assert hass.services.calls == [
        ("fan", "set_preset_mode", {"entity_id": "fan.purifier", "preset_mode": "Auto"}),
        ("fan", "set_percentage", {"entity_id": "fan.purifier", "percentage": 60}),
    ]


def test_fan_with_light_links_light(hass, loader):
    ctx = HapContext(hass, make_device(), [
        make_entry("fan.ceiling"),
        make_entry("light.ceiling", capabilities={"supported_color_modes": ["brightness"]}),
    ])
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    fan, light = acc.services
    assert (fan.type_name, light.type_name) == ("Fanv2", "Lightbulb")
    assert fan.linked == [light] and "Brightness" in light.chars


def test_fallback_covers_common_domains(hass, loader):
    entries = [
        make_entry("switch.plug", device_class="outlet"),
        make_entry("binary_sensor.door", device_class="door"),
        make_entry("lock.front"),
        make_entry("cover.blind", supported_features=fallback.COVER_SET_POSITION),
        make_entry("sensor.hidden_humidity", device_class="humidity", hidden_by="user"),
    ]
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    assert [a.services[0].type_name for a in ctx.accessories] == [
        "Outlet", "ContactSensor", "LockMechanism", "WindowCovering"
    ]


def test_validator_rejects_bad_specs(hass, loader):
    ctx = HapContext(hass, None, [])
    acc = ctx.accessory("Bad")
    acc.service("Lightbulb").char("NotAThing", get=lambda: 1)
    acc.service("NoSuchService")
    acc.service("TemperatureSensor").char("CurrentTemperature", set=lambda v: None)
    errors, _ = validate_spec(acc, loader)
    assert any("NotAThing" in e for e in errors)
    assert any("NoSuchService" in e for e in errors)
    assert any("read-only" in e for e in errors)
