from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.hap_bridge.const import (
    CONF_EXCLUDE_ENTITIES,
    DOMAIN,
    REGISTRY_RELOAD_COOLDOWN,
)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _accessory(bridge, entity_id):
    return next(
        (a for a in bridge.diagnostics()["accessories"] if entity_id in a["watches"]), None
    )


def _hap_accessory(bridge, entity_id):
    return next(acc for spec, acc in bridge._accessories if entity_id in spec.services[0].watched())


async def test_light_is_exposed_and_writable(hass, config_entry, lamp):
    calls = async_mock_service(hass, "light", "turn_off")
    bridge = await _setup(hass, config_entry)

    diag = _accessory(bridge, lamp)
    assert diag["name"] == "Desk Lamp"
    bulb = next(s for s in diag["services"] if s["type"] == "Lightbulb")
    assert bulb["characteristics"]["On"]["value"] is True
    assert bulb["characteristics"]["Brightness"]["value"] == 100

    on = _hap_accessory(bridge, lamp).get_service("Lightbulb").get_characteristic("On")
    # Simulate a controller write from HAP-python's thread-agnostic callback.
    on.setter_callback(False)
    await hass.async_block_till_done()
    assert calls and calls[0].data["entity_id"] == lamp


async def test_unavailable_entity_means_no_response(hass, config_entry, lamp):
    bridge = await _setup(hass, config_entry)
    acc = _hap_accessory(bridge, lamp)
    assert acc.available
    hass.states.async_set(lamp, "unavailable")
    assert not acc.available


async def test_pairing_notification_has_working_qr(hass, config_entry, lamp, hass_client):
    bridge = await _setup(hass, config_entry)
    notes = persistent_notification._async_get_or_create_notifications(hass)
    message = notes[f"{DOMAIN}_{config_entry.entry_id}_pairing"]["message"]
    url = message.split("](")[1].rstrip(")")

    client = await hass_client()
    resp = await client.get(url)
    assert resp.status == 200 and resp.content_type == "image/svg+xml"
    assert (await client.get(url[:-4] + "0000")).status == 404

    bridge._on_paired()
    await hass.async_block_till_done()
    assert f"{DOMAIN}_{config_entry.entry_id}_pairing" not in notes
    assert (await client.get(url)).status == 404


async def test_reset_pairing_button(hass, config_entry, lamp):
    bridge = await _setup(hass, config_entry)
    bridge._on_paired()
    bridge._driver.state.add_paired_client(str(uuid4()).encode(), b"k" * 32, b"\x01")
    await hass.async_block_till_done()

    button = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{config_entry.entry_id}_reset_pairing")
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)

    assert not bridge._driver.state.paired_clients
    notes = persistent_notification._async_get_or_create_notifications(hass)
    assert f"{DOMAIN}_{config_entry.entry_id}_pairing" in notes
    # The bridge's own button is never exposed to HomeKit.
    assert _accessory(bridge, button) is None


async def test_excluded_entity_is_not_exposed(hass, config_entry, lamp):
    hass.config_entries.async_update_entry(config_entry, options={CONF_EXCLUDE_ENTITIES: [lamp]})
    bridge = await _setup(hass, config_entry)
    assert _accessory(bridge, lamp) is None


async def test_options_flow_saves_filters(hass, config_entry, lamp):
    await _setup(hass, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXCLUDE_ENTITIES: [lamp]}
    )
    assert config_entry.options[CONF_EXCLUDE_ENTITIES] == [lamp]


async def test_new_entities_trigger_one_debounced_reload(hass, config_entry, lamp):
    await _setup(hass, config_entry)
    reg = er.async_get(hass)
    new = [reg.async_get_or_create("switch", "acme", f"s{i}").entity_id for i in range(3)]
    for eid in new:
        hass.states.async_set(eid, "off")
    await hass.async_block_till_done()
    assert all(_accessory(config_entry.runtime_data, eid) is None for eid in new)

    with patch.object(
        hass.config_entries, "async_schedule_reload", wraps=hass.config_entries.async_schedule_reload
    ) as reload:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=REGISTRY_RELOAD_COOLDOWN + 1))
        await hass.async_block_till_done()
    assert reload.call_count == 1
    assert all(_accessory(config_entry.runtime_data, eid) for eid in new)


async def test_start_failure_raises_repair(hass, config_entry, lamp):
    with patch(
        "custom_components.hap_bridge.bridge.async_collect_specs", side_effect=RuntimeError("boom")
    ):
        bridge = await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert not bridge.started
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"start_failed_{config_entry.entry_id}")
    assert issue is not None and issue.translation_placeholders["error"] == "boom"


async def test_new_profiles_materialize(hass, config_entry, caplog):
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)

    def device(name, *entities):
        dev = dev_reg.async_get_or_create(
            config_entry_id=source.entry_id, identifiers={("acme", name)}, name=name
        )
        ids = []
        for domain, uid, state, attrs, kw in entities:
            ent = ent_reg.async_get_or_create(domain, "acme", uid, device_id=dev.id, **kw)
            hass.states.async_set(ent.entity_id, state, attrs)
            ids.append(ent.entity_id)
        return ids

    climate, _ = device(
        "Hall",
        ("climate", "hall", "heat", {"temperature": 21, "current_temperature": 19.5},
         {"capabilities": {"hvac_modes": ["off", "heat"], "min_temp": 7, "max_temp": 35}}),
        ("sensor", "hall_hum", "40", {}, {"original_device_class": "humidity"}),
    )
    garage, = device(
        "Garage", ("cover", "garage", "closed", {}, {"original_device_class": "garage"})
    )
    bell, = device(
        "Front", ("event", "bell", "2026-11-01T10:00:00+00:00", {}, {"original_device_class": "doorbell"})
    )
    window, _ = device(
        "Window",
        ("binary_sensor", "window", "off", {}, {"original_device_class": "window"}),
        ("sensor", "window_batt", "80", {"unit_of_measurement": "%"},
         {"original_device_class": "battery", "unit_of_measurement": "%"}),
    )

    bridge = await _setup(hass, config_entry)
    assert "Skipping" not in caplog.text

    def services(entity_id):
        return {s["type"]: s for s in _accessory(bridge, entity_id)["services"]}

    thermostat = services(climate)["Thermostat"]["characteristics"]
    assert thermostat["TargetTemperature"]["value"] == 21
    assert thermostat["CurrentRelativeHumidity"]["value"] == 40
    assert services(garage)["GarageDoorOpener"]["characteristics"]["CurrentDoorState"]["value"] == 1
    assert "Doorbell" in services(bell)
    assert services(window)["BatteryService"]["characteristics"]["BatteryLevel"]["value"] == 80

    # HomeKit only offers the modes the entity supports.
    target = (
        _hap_accessory(bridge, climate).get_service("Thermostat")
        .get_characteristic("TargetHeatingCoolingState")
    )
    assert target.properties["ValidValues"] == {"Off": 0, "Heat": 1}
