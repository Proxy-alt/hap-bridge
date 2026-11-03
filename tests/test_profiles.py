from hb.api import HapContext
from hb.profiles import fallback, generic
from hb.validate import validate_spec

from .conftest import make_device, make_entry


def _run(ctx: HapContext) -> None:
    for describe in generic.PROFILES:
        describe(ctx)
    for ent in ctx.unclaimed():
        fallback.describe_entity(ctx, ent)
    for finish in generic.FINISHERS:
        finish(ctx)


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


def test_device_less_contexts_get_distinct_keys(hass):
    keys = []
    for eid in ("switch.a", "switch.b"):
        ctx = HapContext(hass, None, [make_entry(eid)])
        keys.append(ctx.accessory().key)
    assert keys[0] != keys[1]


def _two_switches(hass, entity_ids):
    ctx = HapContext(hass, make_device(), [make_entry(e) for e in entity_ids])
    acc = ctx.accessory()
    for eid in entity_ids:
        acc.service("Switch").char("On", watch=[eid], get=lambda: True)
    return acc


def test_service_ids_survive_a_sibling_disappearing(hass):
    both = _two_switches(hass, ["switch.a", "switch.b"])
    only_b = _two_switches(hass, ["switch.b"])
    assert both.services[1].unique_id == only_b.services[0].unique_id == "reg-switch.b"
    assert both.services[0].unique_id != both.services[1].unique_id


def test_services_watching_the_same_entity_stay_distinct(hass):
    ctx = HapContext(hass, make_device(), [make_entry("switch.a")])
    acc = ctx.accessory()
    first = acc.service("Switch").char("On", watch=["switch.a"])
    second = acc.service("Switch").char("On", watch=["switch.a"])
    assert first.unique_id != second.unique_id


def test_validator_rejects_duplicate_explicit_unique_ids(hass, loader):
    ctx = HapContext(hass, make_device(), [])
    acc = ctx.accessory()
    acc.service("Switch", unique_id="x").char("On", get=lambda: True)
    acc.service("Switch", unique_id="x").char("On", get=lambda: True)
    errors, _ = validate_spec(acc, loader)
    assert any("unique_id 'x'" in e for e in errors)


def _values(spec) -> dict[str, object]:
    return {
        f"{svc.type_name}.{name}": char.get()
        for svc in spec.services for name, char in svc.chars.items() if char.get
    }


def test_thermostat_folds_in_humidity(hass, loader):
    entries = [
        make_entry("climate.hall", supported_features=fallback.CLIMATE_TARGET_TEMPERATURE_RANGE,
                   capabilities={"hvac_modes": ["off", "heat", "cool", "heat_cool"],
                                 "min_temp": 7, "max_temp": 35}),
        make_entry("sensor.hall_humidity", device_class="humidity"),
    ]
    hass.states.set("climate.hall", "heat_cool", hvac_action="cooling", current_temperature=23.5,
                    target_temp_low=19, target_temp_high=25)
    hass.states.set("sensor.hall_humidity", "41")
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    values = _values(acc)
    assert values["Thermostat.TargetHeatingCoolingState"] == 3
    assert values["Thermostat.CurrentHeatingCoolingState"] == 2
    assert values["Thermostat.TargetTemperature"] == 22
    assert values["Thermostat.HeatingThresholdTemperature"] == 19
    assert values["Thermostat.CurrentRelativeHumidity"] == 41
    target = acc.services[0].chars["TargetHeatingCoolingState"]
    assert target.valid_values == {"Off": 0, "Heat": 1, "Cool": 2, "Auto": 3}

    target.set(3)
    acc.services[0].chars["CoolingThresholdTemperature"].set(26)
    assert hass.services.calls == [
        ("climate", "set_hvac_mode", {"entity_id": "climate.hall", "hvac_mode": "heat_cool"}),
        ("climate", "set_temperature",
         {"entity_id": "climate.hall", "target_temp_high": 26, "target_temp_low": 19}),
    ]


def test_thermostat_converts_fahrenheit(hass, loader):
    from types import SimpleNamespace

    hass.config = SimpleNamespace(units=SimpleNamespace(temperature_unit="°F"))
    hass.states.set("climate.den", "heat", temperature=68, current_temperature=50)
    ctx = HapContext(hass, None, [make_entry("climate.den")])
    _run(ctx)
    _assert_valid(ctx, loader)
    values = _values(ctx.accessories[0])
    assert values["Thermostat.TargetTemperature"] == 20
    assert values["Thermostat.CurrentTemperature"] == 10
    assert values["Thermostat.TemperatureDisplayUnits"] == 1
    ctx.accessories[0].services[0].chars["TargetTemperature"].set(21)
    assert hass.services.calls[-1][2]["temperature"] == 69.8


def test_garage_door(hass, loader):
    ctx = HapContext(hass, make_device(), [make_entry("cover.garage", device_class="garage")])
    hass.states.set("cover.garage", "opening")
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    assert acc.services[0].type_name == "GarageDoorOpener"
    assert _values(acc)["GarageDoorOpener.CurrentDoorState"] == 2
    assert _values(acc)["GarageDoorOpener.TargetDoorState"] == 0
    acc.services[0].chars["TargetDoorState"].set(1)
    assert hass.services.calls[-1][:2] == ("cover", "close_cover")


def test_doorbell_rings_once_per_new_event(hass, loader):
    hass.states.set("event.door", "2026-11-01T10:00:00+00:00")
    ctx = HapContext(hass, make_device(), [make_entry("event.door", device_class="doorbell")])
    _run(ctx)
    _assert_valid(ctx, loader)
    press = ctx.accessories[0].services[0].chars["ProgrammableSwitchEvent"].get

    assert press() is None  # startup: the old event is not a ring
    hass.states.set("event.door", "2026-11-03T08:00:00+00:00")
    assert press() == 0
    assert press() is None  # same event again
    hass.states.set("event.door", "unavailable")
    assert press() is None
    hass.states.set("event.door", "2026-11-03T08:00:00+00:00")
    assert press() is None  # back from unavailable, nothing new


def test_battery_links_to_the_device_tile(hass, loader):
    entries = [
        make_entry("binary_sensor.window", device_class="window"),
        make_entry("sensor.window_battery", device_class="battery", unit_of_measurement="%",
                   entity_category="diagnostic"),
    ]
    hass.states.set("binary_sensor.window", "off")
    hass.states.set("sensor.window_battery", "12")
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    contact, battery = acc.services
    assert battery.type_name == "BatteryService" and contact.linked == [battery]
    values = _values(acc)
    assert values["BatteryService.BatteryLevel"] == 12
    assert values["BatteryService.StatusLowBattery"] == 1
    assert values["BatteryService.ChargingState"] == 2
    # A dead battery sensor doesn't make the window sensor "No Response".
    hass.states.set("sensor.window_battery", "unavailable")
    assert acc.is_available()


def test_battery_alone_adds_nothing(hass):
    ctx = HapContext(hass, make_device(), [make_entry("sensor.b", device_class="battery")])
    _run(ctx)
    assert ctx.accessories == []
