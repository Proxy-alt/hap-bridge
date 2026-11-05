from hb.api import HapContext
from hb.profiles import fallback

from .conftest import make_entry
from .test_profiles import _assert_valid, _run, _values


def _single(hass, loader, entity_id, state, attrs=None, **entry):
    hass.states.set(entity_id, state, **(attrs or {}))
    ctx = HapContext(hass, None, [make_entry(entity_id, **entry)])
    _run(ctx)
    _assert_valid(ctx, loader)
    return ctx


def test_person_and_device_tracker_are_occupancy_sensors(hass, loader):
    for eid in ("person.alex", "device_tracker.phone"):
        ctx = _single(hass, loader, eid, "home")
        assert _values(ctx.accessories[0]) == {"OccupancySensor.OccupancyDetected": 1}
        hass.states.set(eid, "not_home")
        assert _values(ctx.accessories[0]) == {"OccupancySensor.OccupancyDetected": 0}


def test_siren_is_a_switch(hass, loader):
    ctx = _single(hass, loader, "siren.hall", "off")
    on = ctx.accessories[0].services[0].chars["On"]
    on.set(True)
    assert hass.services.calls[-1][:2] == ("siren", "turn_on")


def test_weather_temperature_and_humidity(hass, loader):
    ctx = _single(hass, loader, "weather.home", "sunny",
                  {"temperature": 50, "temperature_unit": "°F", "humidity": 70})
    assert _values(ctx.accessories[0]) == {
        "TemperatureSensor.CurrentTemperature": 10.0,
        "HumiditySensor.CurrentRelativeHumidity": 70,
    }


def test_speaker_switch_powers_or_plays(hass, loader):
    ctx = _single(hass, loader, "media_player.kitchen", "playing",
                  supported_features=fallback.MEDIA_PLAY | fallback.MEDIA_PAUSE)
    on = ctx.accessories[0].services[0].chars["On"]
    assert on.get() is True
    on.set(False)
    assert hass.services.calls[-1][:2] == ("media_player", "media_pause")

    ctx = _single(hass, loader, "media_player.den", "standby",
                  supported_features=fallback.MEDIA_TURN_ON | fallback.MEDIA_TURN_OFF)
    on = ctx.accessories[0].services[0].chars["On"]
    assert on.get() is False
    on.set(True)
    assert hass.services.calls[-1][:2] == ("media_player", "turn_on")

    # Nothing to control: left out.
    assert not _single(hass, loader, "media_player.dumb", "idle").accessories


def test_fan_with_few_speeds_gets_a_stepped_slider(hass, loader):
    ctx = _single(hass, loader, "fan.ceiling", "on",
                  {"percentage": 67, "percentage_step": 33.333333}, supported_features=1)
    speed = ctx.accessories[0].services[0].chars["RotationSpeed"]
    assert speed.props == {"minStep": 33.3333}
    speed.set(66.6)
    assert hass.services.calls[-1] == (
        "fan", "set_percentage", {"entity_id": "fan.ceiling", "percentage": 67})

    # A fan with fine steps keeps the default slider.
    ctx = _single(hass, loader, "fan.fine", "on",
                  {"percentage": 50, "percentage_step": 1}, supported_features=1)
    assert ctx.accessories[0].services[0].chars["RotationSpeed"].props == {}
