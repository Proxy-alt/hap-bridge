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


def _light_ctx(hass, **attrs):
    ent = make_entry("light.strip", capabilities={
        "supported_color_modes": ["hs", "color_temp"],
        "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500,
    })
    hass.states.set("light.strip", "on", **attrs)
    ctx = HapContext(hass, None, [ent])
    _run(ctx)
    return ctx, ctx.accessories[0].services[0]


def test_color_light_reads_and_validates(hass, loader):
    ctx, bulb = _light_ctx(hass, brightness=128, hs_color=(30, 80), color_temp_kelvin=4000)
    _assert_valid(ctx, loader)
    values = _values(ctx.accessories[0])
    assert values["Lightbulb.Brightness"] == 50
    assert (values["Lightbulb.Hue"], values["Lightbulb.Saturation"]) == (30, 80)
    assert values["Lightbulb.ColorTemperature"] == 250
    assert bulb.chars["ColorTemperature"].props == {"minValue": 153, "maxValue": 500}


def test_color_light_batches_one_write(hass):
    _, bulb = _light_ctx(hass, brightness=255, hs_color=(30, 80))
    bulb.write({"On": True, "Brightness": 40, "Hue": 200})
    bulb.write({"ColorTemperature": 400, "Hue": 10, "Saturation": 10})
    bulb.write({"Brightness": 0})
    calls = hass.services.calls
    assert calls[0] == ("light", "turn_on",
                        {"entity_id": "light.strip", "brightness_pct": 40, "hs_color": (200, 80)})
    assert calls[1][2] == {"entity_id": "light.strip", "color_temp_kelvin": 2500}
    assert calls[2][:2] == ("light", "turn_off")


def test_off_light_keeps_last_brightness(hass):
    _, bulb = _light_ctx(hass)
    assert bulb.chars["Brightness"].get() is None
    assert bulb.chars["Hue"].get() is None


def _remote(hass, *event_types):
    entries = [
        make_entry(f"event.remote_{i}", device_class="button",
                   capabilities={"event_types": list(event_types)})
        for i in (1, 2)
    ]
    for e in entries:
        hass.states.set(e.entity_id, "2026-11-01T10:00:00+00:00", event_type="single_press")
    return HapContext(hass, make_device(), entries)


def test_remote_becomes_one_labelled_accessory(hass, loader):
    ctx = _remote(hass, "single_press", "long_press", "rotate")
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    label, first, second = acc.services
    assert label.type_name == "ServiceLabel"
    assert [s.chars["ServiceLabelIndex"].get() for s in (first, second)] == [1, 2]
    assert first.chars["ProgrammableSwitchEvent"].valid_values == {"SinglePress": 0, "LongPress": 2}

    press = second.chars["ProgrammableSwitchEvent"].get
    assert press() is None
    hass.states.set("event.remote_2", "2026-11-03T08:00:00+00:00", event_type="long_press")
    assert press() == 2
    hass.states.set("event.remote_2", "2026-11-03T08:00:05+00:00", event_type="rotate")
    assert press() is None  # not something HomeKit can show


def test_single_button_and_unmappable_buttons(hass, loader):
    ctx = HapContext(hass, None, [make_entry(
        "event.btn", device_class="button", capabilities={"event_types": ["press"]})])
    _run(ctx)
    _assert_valid(ctx, loader)
    assert ctx.accessories[0].services[0].type_name == "StatelessProgrammableSwitch"

    ctx = HapContext(hass, None, [make_entry(
        "event.knob", device_class="button", capabilities={"event_types": ["rotate"]})])
    _run(ctx)
    assert ctx.accessories == []


def test_air_quality_uses_worst_pollutant(hass, loader):
    entries = [
        make_entry("sensor.aq_pm25", device_class="pm25"),
        make_entry("sensor.aq_voc", device_class="volatile_organic_compounds"),
        make_entry("sensor.aq_ozone", device_class="ozone"),
    ]
    hass.states.set("sensor.aq_pm25", "5")      # excellent
    hass.states.set("sensor.aq_voc", "700")     # fair
    hass.states.set("sensor.aq_ozone", "40")    # no bands: density only
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    values = _values(acc)
    assert values["AirQualitySensor.AirQuality"] == 3
    assert values["AirQualitySensor.VOCDensity"] == 700
    assert values["AirQualitySensor.OzoneDensity"] == 40


def test_gas_and_light_sensors(hass, loader):
    entries = [
        make_entry("sensor.co2", device_class="carbon_dioxide"),
        make_entry("sensor.lux", device_class="illuminance"),
        make_entry("binary_sensor.gas", device_class="gas"),
    ]
    hass.states.set("sensor.co2", "1200")
    hass.states.set("sensor.lux", "0")
    hass.states.set("binary_sensor.gas", "on")
    ctx = HapContext(hass, None, entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    values = {}
    for acc in ctx.accessories:
        values.update(_values(acc))
    assert values["CarbonDioxideSensor.CarbonDioxideDetected"] == 1
    assert values["CarbonDioxideSensor.CarbonDioxideLevel"] == 1200
    assert values["LightSensor.CurrentAmbientLightLevel"] == 0.0001
    assert values["CarbonMonoxideSensor.CarbonMonoxideDetected"] == 1


def test_status_flags_on_sensor_tiles(hass, loader):
    entries = [
        make_entry("binary_sensor.door", device_class="door"),
        make_entry("binary_sensor.door_tamper", device_class="tamper", entity_category="diagnostic"),
        make_entry("binary_sensor.door_battery_low", device_class="battery",
                   entity_category="diagnostic"),
    ]
    hass.states.set("binary_sensor.door", "off")
    hass.states.set("binary_sensor.door_tamper", "on")
    hass.states.set("binary_sensor.door_battery_low", "off")
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    values = _values(acc)
    assert values["ContactSensor.StatusTampered"] == 1
    assert values["ContactSensor.StatusLowBattery"] == 0
    assert "BatteryService" not in {s.type_name for s in acc.services}  # no level sensor
    hass.states.set("binary_sensor.door_tamper", "unavailable")
    assert acc.is_available()


def test_fan_extras_depend_on_service(hass, loader):
    features = (fallback.FAN_SET_SPEED | fallback.FAN_OSCILLATE | fallback.FAN_DIRECTION
                | fallback.FAN_PRESET_MODE)
    hass.states.set("fan.ceiling", "on", oscillating=True, direction="reverse",
                    preset_mode="Auto", preset_modes=["Auto", "Breeze"])
    ctx = HapContext(hass, None, [make_entry("fan.ceiling", supported_features=features)])
    _run(ctx)
    _assert_valid(ctx, loader)
    fan = ctx.accessories[0].services[0]
    assert {"SwingMode", "RotationDirection", "TargetFanState"} <= set(fan.chars)
    values = _values(ctx.accessories[0])
    assert (values["Fanv2.SwingMode"], values["Fanv2.RotationDirection"],
            values["Fanv2.TargetFanState"]) == (1, 1, 1)
    fan.chars["RotationDirection"].set(0)
    assert hass.services.calls[-1] == ("fan", "set_direction",
                                       {"entity_id": "fan.ceiling", "direction": "forward"})

    # The purifier profile reuses bind_fan on AirPurifier, which has no direction.
    ctx = _purifier_ctx(hass)
    ctx.entries[0].supported_features = features
    _run(ctx)
    _assert_valid(ctx, loader)
    purifier = ctx.accessories[0].services[0]
    assert "SwingMode" in purifier.chars and "RotationDirection" not in purifier.chars


def test_cover_tilt(hass, loader):
    features = fallback.COVER_SET_POSITION | fallback.COVER_SET_TILT_POSITION
    hass.states.set("cover.blind", "open", current_position=100, current_tilt_position=25)
    ctx = HapContext(hass, None, [make_entry("cover.blind", supported_features=features)])
    _run(ctx)
    _assert_valid(ctx, loader)
    svc = ctx.accessories[0].services[0]
    assert svc.chars["CurrentHorizontalTiltAngle"].get() == -45
    svc.chars["TargetHorizontalTiltAngle"].set(90)
    assert hass.services.calls[-1][2]["tilt_position"] == 100
