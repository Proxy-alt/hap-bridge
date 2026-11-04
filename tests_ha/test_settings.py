"""Accessory settings for a Home app extension."""

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.hap_bridge.const import DOMAIN
from tests_ha.test_quick_wins import _acc, _device, _setup

CONFIG, DIAG = EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC


@pytest.fixture
async def bulb(hass, config_entry):
    lamp, led, power_on, level, ident, rssi, update = _device(
        hass, "Desk",
        ("light", "lamp", {}, "on"),
        ("switch", "led", {"entity_category": CONFIG, "original_name": "LED"}, "on"),
        ("select", "pon", {"entity_category": CONFIG, "original_name": "Power on"}, "previous"),
        ("number", "lvl", {"entity_category": CONFIG, "original_name": "Start level"}, "40"),
        ("button", "id", {"entity_category": CONFIG, "original_device_class": "identify",
                          "original_name": "Identify"}, "unknown"),
        ("sensor", "rssi", {"entity_category": DIAG, "original_name": "Signal"}, "-61"),
        ("update", "fw", {"entity_category": CONFIG}, "off"))
    hass.states.async_set(power_on, "previous", {"options": ["on", "off", "previous"],
                                                 "friendly_name": "Desk Power on"})
    hass.states.async_set(level, "40", {"min": 1, "max": 100, "step": 1,
                                        "unit_of_measurement": "%"})
    hass.states.async_set(rssi, "-61", {"unit_of_measurement": "dBm"})
    device_id = er.async_get(hass).async_get(lamp).device_id
    dr.async_get(hass).async_update_device(
        device_id, manufacturer="Acme", model="B1", sw_version="2.1", serial_number="REAL-42")
    bridge = await _setup(hass, config_entry)
    acc = _acc(bridge, lamp)
    serial = acc.get_service("AccessoryInformation").get_characteristic("SerialNumber").value
    return {"serial": serial, "led": led, "power_on": power_on, "level": level,
            "identify": ident, "rssi": rssi, "update": update, "lamp": lamp}


async def _settings(hass, serial):
    return await hass.services.async_call(DOMAIN, "accessory_settings", {"serial": serial},
                                          blocking=True, return_response=True)


async def test_settings_are_listed_in_home_extension_shapes(hass, bulb):
    result = await _settings(hass, bulb["serial"])
    kinds = {s["entity_id"]: s for s in result["settings"]}
    assert kinds[bulb["led"]]["kind"] == "toggle" and kinds[bulb["led"]]["value"] is True
    assert kinds[bulb["power_on"]] | {} == {
        "entity_id": bulb["power_on"], "label": "Power on", "available": True,
        "kind": "choice", "options": ["on", "off", "previous"], "value": "previous"}
    assert kinds[bulb["level"]]["kind"] == "range"
    assert (kinds[bulb["level"]]["min"], kinds[bulb["level"]]["max"],
            kinds[bulb["level"]]["value"], kinds[bulb["level"]]["unit"]) == (1, 100, 40.0, "%")
    assert kinds[bulb["identify"]]["kind"] == "action"
    assert bulb["update"] not in kinds and bulb["lamp"] not in kinds

    info = {i["label"]: i["value"] for i in result["info"]}
    assert info["Serial number"] == "REAL-42" and info["Firmware"] == "2.1"
    assert info["Signal"] == "-61 dBm"


async def test_settings_can_be_changed(hass, bulb):
    off = async_mock_service(hass, "switch", "turn_off")
    select = async_mock_service(hass, "select", "select_option")
    number = async_mock_service(hass, "number", "set_value")
    press = async_mock_service(hass, "button", "press")

    async def put(entity_id, value=None):
        data = {"serial": bulb["serial"], "entity_id": entity_id}
        if value is not None:
            data["value"] = value
        await hass.services.async_call(DOMAIN, "set_accessory_setting", data, blocking=True)

    await put(bulb["led"], "off")
    await put(bulb["power_on"], "on")
    await put(bulb["level"], 75)
    await put(bulb["identify"])
    assert off[0].data == {"entity_id": bulb["led"]}
    assert select[0].data == {"entity_id": bulb["power_on"], "option": "on"}
    assert number[0].data == {"entity_id": bulb["level"], "value": 75}
    assert press[0].data == {"entity_id": bulb["identify"]}

    # Only the accessory's own settings: not its light, not diagnostics.
    for entity_id in (bulb["lamp"], bulb["rssi"]):
        with pytest.raises(ServiceValidationError):
            await put(entity_id, "on")
    with pytest.raises(ServiceValidationError):
        await put(bulb["power_on"])  # a choice needs a value


async def test_unknown_serial(hass, bulb):
    with pytest.raises(ServiceValidationError):
        await _settings(hass, "HB-NOPE")
