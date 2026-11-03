"""Adaptive Lighting end to end: Home app writes in, light.turn_on calls out."""

import base64
from datetime import timedelta
import struct

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.hap_bridge.transition import tlv, untlv, write_uint

HOUR = 3_600_000
CLIENT = ("127.0.0.1", 50000)


@pytest.fixture
async def light(hass: HomeAssistant) -> str:
    MockConfigEntry(domain="acme").add_to_hass(hass)
    ent = er.async_get(hass).async_get_or_create(
        "light", "acme", "desk", original_name="Desk",
        capabilities={"supported_color_modes": ["color_temp"],
                      "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500},
    )
    hass.states.async_set(ent.entity_id, "on", {
        "brightness": 128, "color_mode": "color_temp", "color_temp_kelvin": 4000,
    })
    return ent.entity_id


@pytest.fixture
async def setup(hass, config_entry, light):
    calls = async_mock_service(hass, "light", "turn_on")
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    bridge = config_entry.runtime_data
    (acc,) = (a for spec, a in bridge._accessories if light in spec.services[0].watched())
    return bridge, acc, acc.get_service("Lightbulb"), calls


def _iid(acc, service, name):
    return acc.iid_manager.get_iid(service.get_characteristic(name))


def _curve_update(acc, bulb, *, enable=True):
    """A TransitionControl update like the Home app sends: 200, 210, .. mireds hourly."""
    entries = [
        tlv(0x01, struct.pack("<f", -0.5), 0x02, struct.pack("<f", 200 + i * 10),
            0x03, write_uint(0 if i == 0 else HOUR), 0x04, write_uint(0))
        for i in range(5)
    ]
    curve = tlv(
        0x01, entries,
        0x02, write_uint(_iid(acc, bulb, "Brightness")),
        0x03, tlv(0x01, (10).to_bytes(4, "little"), 0x02, (100).to_bytes(4, "little")),
    )
    config = tlv(
        0x01, write_uint(_iid(acc, bulb, "ColorTemperature")),
        *((0x02, tlv(0x01, b"\x01" * 16, 0x02, bytes(8)), 0x03, 1, 0x05, curve,
           0x06, (60_000).to_bytes(2, "little"), 0x08, (600_000).to_bytes(4, "little"))
          if enable else ()),
    )
    return base64.b64encode(tlv(0x02, tlv(0x01, config))).decode()


def _write(bridge, acc, service, values, *, response=False):
    query = {"characteristics": [
        {"aid": acc.aid, "iid": _iid(acc, service, name), "value": value,
         **({"r": True} if response else {})}
        for name, value in values.items()
    ]}
    return bridge._driver.set_characteristics(query, CLIENT)


async def _enable(hass, bridge, acc, bulb):
    result = _write(bridge, acc, bulb, {"TransitionControl": _curve_update(acc, bulb)}, response=True)
    await hass.async_block_till_done()
    return result


def _kelvins(calls):
    return [c.data.get("color_temp_kelvin") for c in calls]


async def test_light_offers_adaptive_lighting(setup):
    _bridge, acc, bulb, _calls = setup
    supported = untlv(base64.b64decode(
        bulb.get_characteristic("SupportedTransitionConfiguration").value
    ))[0x01]
    iids = [untlv(item)[0x01][0] for item in supported]
    assert iids == [_iid(acc, bulb, "Brightness"), _iid(acc, bulb, "ColorTemperature")]
    assert bulb.get_characteristic("ActiveTransitionCount").value == 0


async def test_enabling_answers_and_follows_curve(hass, setup, hass_storage, config_entry):
    bridge, acc, bulb, calls = setup
    result = await _enable(hass, bridge, acc, bulb)

    (char_result,) = result["characteristics"]
    assert char_result["status"] == 0
    status = untlv(untlv(untlv(base64.b64decode(char_result["value"]))[0x02])[0x01])
    assert status[0x01] == bytes([_iid(acc, bulb, "ColorTemperature")])
    assert bulb.get_characteristic("ActiveTransitionCount").value == 1

    # 200 mireds - 0.5 * 50% brightness = 175 mireds = 5714 K
    assert _kelvins(calls) == [5714]
    saved = hass_storage[f"hap_bridge.{config_entry.entry_id}.adaptive"]["data"]
    assert len(saved) == 1

    # The light hasn't changed (mocked service), so the next tick sends again.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_brightness_and_turn_on_carry_the_temperature(hass, setup, light):
    bridge, acc, bulb, calls = setup
    await _enable(hass, bridge, acc, bulb)
    calls.clear()

    _write(bridge, acc, bulb, {"On": True, "Brightness": 100})
    await hass.async_block_till_done()
    # One call: brightness and the curve's temperature for 100% (200 - 50 = 150
    # mireds), clamped to the light's coolest, 6500 K = 153 mireds.
    assert len(calls) == 1
    assert calls[0].data == {"entity_id": light, "brightness_pct": 100, "color_temp_kelvin": 6536}


async def test_off_lights_wait_and_catch_up_when_turned_on(hass, setup, light):
    bridge, acc, bulb, calls = setup
    hass.states.async_set(light, "off", {})
    await hass.async_block_till_done()
    await _enable(hass, bridge, acc, bulb)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert calls == []

    hass.states.async_set(light, "on", {"brightness": 255, "color_mode": "color_temp",
                                        "color_temp_kelvin": 3000})
    await hass.async_block_till_done()
    assert _kelvins(calls) == [6536]  # 100%: 150 mireds, clamped to 153


async def test_manual_color_in_home_assistant_switches_it_off(hass, setup, light, hass_storage,
                                                            config_entry):
    bridge, acc, bulb, calls = setup
    await _enable(hass, bridge, acc, bulb)
    # Our own change coming back is not manual...
    hass.states.async_set(light, "on", {"brightness": 128, "color_mode": "color_temp",
                                        "color_temp_kelvin": 5700})
    await hass.async_block_till_done()
    assert bulb.get_characteristic("ActiveTransitionCount").value == 1

    # ...a warm white picked by hand is.
    hass.states.async_set(light, "on", {"brightness": 128, "color_mode": "color_temp",
                                        "color_temp_kelvin": 2700})
    await hass.async_block_till_done()
    assert bulb.get_characteristic("ActiveTransitionCount").value == 0
    assert hass_storage[f"hap_bridge.{config_entry.entry_id}.adaptive"]["data"] == {}

    calls.clear()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert calls == []


async def test_home_app_color_write_or_disable_switches_it_off(hass, setup):
    bridge, acc, bulb, _calls = setup
    await _enable(hass, bridge, acc, bulb)
    _write(bridge, acc, bulb, {"ColorTemperature": 300})
    await hass.async_block_till_done()
    assert bulb.get_characteristic("ActiveTransitionCount").value == 0

    await _enable(hass, bridge, acc, bulb)
    result = _write(
        bridge, acc, bulb, {"TransitionControl": _curve_update(acc, bulb, enable=False)},
        response=True,
    )
    await hass.async_block_till_done()
    assert result["characteristics"][0]["status"] == 0
    assert bulb.get_characteristic("ActiveTransitionCount").value == 0


async def test_bad_request_fails_without_breaking(hass, setup):
    bridge, acc, bulb, _calls = setup
    result = _write(bridge, acc, bulb, {"TransitionControl": "AAAA"}, response=True)
    assert result["characteristics"][0]["status"] != 0
    assert bulb.get_characteristic("ActiveTransitionCount").value == 0


async def test_schedule_survives_a_reload(hass, setup, config_entry):
    bridge, acc, bulb, calls = setup
    await _enable(hass, bridge, acc, bulb)
    calls.clear()

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    bridge = config_entry.runtime_data
    (adaptive,) = (a for _key, a in bridge._adaptive)
    assert adaptive.active
    assert _kelvins(calls) == [5714]  # resumed and caught up
