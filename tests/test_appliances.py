import asyncio

from hb.api import HapContext
from hb.profiles import appliances

from .conftest import make_device, make_entry
from .test_profiles import _assert_valid, _run, _values

AC_FEATURES = (appliances.CLIMATE_TARGET_TEMPERATURE_RANGE | appliances.CLIMATE_FAN_MODE
               | appliances.CLIMATE_SWING_MODE)
AC_CAPS = {
    "hvac_modes": ["off", "heat", "cool", "heat_cool", "dry", "fan_only"],
    "fan_modes": ["auto", "low", "medium", "high"],
    "swing_modes": ["off", "vertical"],
    "min_temp": 16, "max_temp": 30,
}


def _single(hass, loader, entity_id, state, attrs=None, device=None, **entry):
    hass.states.set(entity_id, state, **(attrs or {}))
    ctx = HapContext(hass, device, [make_entry(entity_id, **entry)])
    _run(ctx)
    _assert_valid(ctx, loader)
    return ctx


def _calls(hass):
    return [(d, s, {k: v for k, v in data.items() if k != "entity_id"})
            for d, s, data in hass.services.calls]


def test_ac_unit_becomes_heater_cooler(hass, loader):
    ctx = _single(hass, loader, "climate.ac", "cool",
                  {"hvac_action": "cooling", "current_temperature": 26, "target_temp_low": 20,
                   "target_temp_high": 24, "fan_mode": "medium", "swing_mode": "vertical"},
                  supported_features=AC_FEATURES, capabilities=AC_CAPS)
    (acc,) = ctx.accessories
    assert acc.category == "AIR_CONDITIONER"
    svc = acc.services[0]
    assert svc.type_name == "HeaterCooler"
    values = _values(acc)
    assert values["HeaterCooler.Active"] == 1
    assert values["HeaterCooler.CurrentHeaterCoolerState"] == 3
    assert values["HeaterCooler.TargetHeaterCoolerState"] == 2
    assert values["HeaterCooler.CoolingThresholdTemperature"] == 24
    assert values["HeaterCooler.RotationSpeed"] == 67
    assert values["HeaterCooler.SwingMode"] == 1
    assert svc.chars["TargetHeaterCoolerState"].valid_values == {"Auto": 0, "Heat": 1, "Cool": 2}

    asyncio.run(svc.write({"Active": 1, "TargetHeaterCoolerState": 1,
                           "HeatingThresholdTemperature": 21, "RotationSpeed": 100,
                           "SwingMode": 0}))
    assert _calls(hass) == [
        ("climate", "set_hvac_mode", {"hvac_mode": "heat"}),
        ("climate", "set_temperature", {"target_temp_low": 21, "target_temp_high": 24}),
        ("climate", "set_fan_mode", {"fan_mode": "high"}),
        ("climate", "set_swing_mode", {"swing_mode": "off"}),
    ]


def test_heater_cooler_power_without_turn_on_off_features(hass, loader):
    ctx = _single(hass, loader, "climate.ac", "off", {}, supported_features=AC_FEATURES,
                  capabilities=AC_CAPS)
    svc = ctx.accessories[0].services[0]
    asyncio.run(svc.write({"Active": 1}))
    asyncio.run(svc.write({"Active": 0}))
    assert _calls(hass) == [
        ("climate", "set_hvac_mode", {"hvac_mode": "heat_cool"}),
        ("climate", "set_hvac_mode", {"hvac_mode": "off"}),
    ]


def test_climate_with_humidity_target_stays_thermostat(hass, loader):
    ctx = _single(hass, loader, "climate.hall", "heat", {"temperature": 20},
                  supported_features=AC_FEATURES | appliances.CLIMATE_TARGET_HUMIDITY,
                  capabilities=AC_CAPS)
    assert ctx.accessories[0].services[0].type_name == "Thermostat"


def test_dehumidifier_with_device_humidity(hass, loader):
    entries = [
        make_entry("humidifier.dry", device_class="dehumidifier",
                   capabilities={"min_humidity": 30, "max_humidity": 80}),
        make_entry("sensor.dry_humidity", device_class="humidity"),
    ]
    hass.states.set("humidifier.dry", "on", action="drying", humidity=45)
    hass.states.set("sensor.dry_humidity", "60")
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    values = _values(acc)
    assert values["HumidifierDehumidifier.CurrentHumidifierDehumidifierState"] == 3
    assert values["HumidifierDehumidifier.TargetHumidifierDehumidifierState"] == 2
    assert values["HumidifierDehumidifier.CurrentRelativeHumidity"] == 60
    assert values["HumidifierDehumidifier.RelativeHumidityDehumidifierThreshold"] == 45
    acc.services[0].chars["RelativeHumidityDehumidifierThreshold"].set(50)
    assert _calls(hass)[-1] == ("humidifier", "set_humidity", {"humidity": 50})


def test_alarm_panel_offers_supported_modes(hass, loader):
    features = appliances.ALARM_ARM_AWAY | appliances.ALARM_ARM_NIGHT
    ctx = _single(hass, loader, "alarm_control_panel.house", "armed_night",
                  supported_features=features)
    svc = ctx.accessories[0].services[0]
    assert svc.chars["SecuritySystemTargetState"].valid_values == {
        "AwayArm": 1, "NightArm": 2, "Disarm": 3}
    assert _values(ctx.accessories[0])["SecuritySystem.SecuritySystemCurrentState"] == 2
    svc.chars["SecuritySystemTargetState"].set(3)
    assert _calls(hass)[-1][:2] == ("alarm_control_panel", "alarm_disarm")
    hass.states.set("alarm_control_panel.house", "triggered")
    assert svc.chars["SecuritySystemCurrentState"].get() == 4


def test_valve_and_water_heater(hass, loader):
    ctx = _single(hass, loader, "valve.main", "opening",
                  supported_features=appliances.VALVE_OPEN | appliances.VALVE_CLOSE)
    values = _values(ctx.accessories[0])
    assert (values["Valve.Active"], values["Valve.InUse"]) == (1, 0)

    ctx = _single(hass, loader, "water_heater.tank", "eco",
                  {"temperature": 55, "current_temperature": 50},
                  capabilities={"min_temp": 40, "max_temp": 65})
    svc = ctx.accessories[0].services[0]
    assert svc.chars["TargetHeatingCoolingState"].valid_values == {"Heat": 1}
    assert svc.chars["TargetTemperature"].props == {"minValue": 40, "maxValue": 65}
    assert _values(ctx.accessories[0])["Thermostat.TargetTemperature"] == 55


def test_vacuum_mower_scene_script_button(hass, loader):
    ctx = _single(hass, loader, "vacuum.robot", "docked",
                  supported_features=appliances.VACUUM_START | appliances.VACUUM_RETURN_HOME)
    on = ctx.accessories[0].services[0].chars["On"]
    assert on.get() is False
    on.set(True)
    on.set(False)
    assert [c[1] for c in _calls(hass)] == ["start", "return_to_base"]

    ctx = _single(hass, loader, "lawn_mower.lawn", "mowing")
    assert ctx.accessories[0].services[0].chars["On"].get() is True

    for domain, service in (("scene", "turn_on"), ("button", "press")):
        ctx = _single(hass, loader, f"{domain}.thing", "2026-11-01T10:00:00+00:00")
        on = ctx.accessories[0].services[0].chars["On"]
        assert on.get() is False
        on.set(True)
        assert _calls(hass)[-1][:2] == (domain, service)
        on.set(False)
        assert _calls(hass)[-1][:2] == (domain, service)  # turning off does nothing

    ctx = _single(hass, loader, "script.bedtime", "on")
    assert ctx.accessories[0].services[0].chars["On"].get() is True

    ctx = _single(hass, loader, "button.restart", "unknown", device_class="restart")
    assert ctx.accessories == []


def test_select_becomes_switch_per_option(hass, loader):
    ctx = _single(hass, loader, "input_select.mode", "Away",
                  capabilities={"options": ["Home", "Away", "Night"]})
    (acc,) = ctx.accessories
    switches = acc.services
    assert [s.chars["Name"].get() for s in switches] == ["Home", "Away", "Night"]
    assert [s.chars["On"].get() for s in switches] == [False, True, False]
    assert switches[0].unique_id == "option:Home"
    switches[2].chars["On"].set(True)
    assert _calls(hass)[-1] == ("input_select", "select_option", {"option": "Night"})


def test_doorbell_links_motion_and_garage_uses_obstruction(hass, loader):
    hass.states.set("event.front", "2026-11-01T10:00:00+00:00")
    hass.states.set("binary_sensor.front_motion", "on")
    ctx = HapContext(hass, make_device(), [
        make_entry("event.front", device_class="doorbell"),
        make_entry("binary_sensor.front_motion", device_class="motion"),
    ])
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    bell, motion = acc.services
    assert bell.linked == [motion] and motion.chars["MotionDetected"].get() is True

    hass.states.set("cover.garage", "closed")
    hass.states.set("binary_sensor.garage_obstruction", "on")
    ctx = HapContext(hass, make_device(), [
        make_entry("cover.garage", device_class="garage"),
        make_entry("binary_sensor.garage_obstruction", device_class="safety"),
    ])
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    assert _values(acc)["GarageDoorOpener.ObstructionDetected"] is True


def test_purifier_filter_change_and_reset(hass, loader):
    entries = [
        make_entry("fan.purifier"),
        make_entry("binary_sensor.purifier_replace_filter", translation_key="replace_filter",
                   device_class="problem", entity_category="diagnostic"),
        make_entry("button.purifier_reset_filter", translation_key="reset_filter",
                   entity_category="config"),
    ]
    hass.states.set("fan.purifier", "on")
    hass.states.set("binary_sensor.purifier_replace_filter", "on")
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    _assert_valid(ctx, loader)
    (acc,) = ctx.accessories
    purifier, filt = acc.services
    assert purifier.type_name == "AirPurifier" and filt.type_name == "FilterMaintenance"
    assert filt.chars["FilterChangeIndication"].get() == 1
    filt.chars["ResetFilterIndication"].set(1)
    assert _calls(hass)[-1] == ("button", "press", {})
