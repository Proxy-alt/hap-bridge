from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
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

    bulb = _hap_accessory(bridge, lamp).get_service("Lightbulb")
    # Lights take whole-service writes; simulate one from HAP-python.
    bulb.setter_callback({"On": False})
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
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "filters"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_EXCLUDE_ENTITIES: [lamp]}
    )
    assert config_entry.options[CONF_EXCLUDE_ENTITIES] == [lamp]


async def _options_step(hass, entry, step):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step})


async def test_rename_accessory(hass, config_entry, lamp):
    bridge = await _setup(hass, config_entry)
    spec = next(s for s, _a in bridge.accessories() if any(
        lamp in svc.watched() for svc in s.services))
    key, default = spec.key, spec.name

    result = await _options_step(hass, config_entry, "rename")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"accessory": key})
    assert result["step_id"] == "rename_accessory"
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "-Lamp!"})
    assert bad["errors"] == {"name": "invalid_name"}
    await hass.config_entries.options.async_configure(bad["flow_id"], {"name": "  Reading   Lamp "})
    await hass.async_block_till_done()
    assert config_entry.options["names"] == {key: "Reading Lamp"}

    bridge = config_entry.runtime_data
    spec, acc = next((s, a) for s, a in bridge.accessories() if s.key == key)
    assert (spec.name, spec.default_name, acc.display_name) == ("Reading Lamp", default, "Reading Lamp")

    # Empty goes back to the profile's name.
    result = await _options_step(hass, config_entry, "rename")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"accessory": key})
    await hass.config_entries.options.async_configure(result["flow_id"], {"name": ""})
    await hass.async_block_till_done()
    assert config_entry.options["names"] == {}
    assert next(s for s, _a in config_entry.runtime_data.accessories() if s.key == key).name == default


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


async def test_color_light_write_is_batched_through_hap(hass, config_entry):
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    ent = er.async_get(hass).async_get_or_create(
        "light", "acme", "strip", original_name="Strip",
        capabilities={"supported_color_modes": ["hs", "color_temp"],
                      "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500},
    )
    hass.states.async_set(ent.entity_id, "on", {"brightness": 255, "hs_color": (30, 80),
                                                 "color_temp_kelvin": 4000})
    on_calls = async_mock_service(hass, "light", "turn_on")
    bridge = await _setup(hass, config_entry)

    acc = _hap_accessory(bridge, ent.entity_id)
    bulb = acc.get_service("Lightbulb")
    assert bulb.get_characteristic("ColorTemperature").properties["minValue"] == 153
    iid = acc.iid_manager.get_iid
    query = {"characteristics": [
        {"aid": acc.aid, "iid": iid(bulb.get_characteristic(name)), "value": value}
        for name, value in (("On", True), ("Brightness", 60), ("Hue", 120), ("Saturation", 50))
    ]}
    bridge._driver.set_characteristics(query, ("127.0.0.1", 50000))
    await hass.async_block_till_done()

    assert len(on_calls) == 1
    assert on_calls[0].data == {"entity_id": ent.entity_id, "brightness_pct": 60,
                                "hs_color": (120, 50)}


async def test_remote_presses_reach_homekit(hass, config_entry):
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("acme", "remote")}, name="Remote"
    )
    eids = []
    for i in (1, 2):
        ent = er.async_get(hass).async_get_or_create(
            "event", "acme", f"btn{i}", device_id=dev.id, original_device_class="button",
            capabilities={"event_types": ["single_press", "double_press"]},
        )
        hass.states.async_set(ent.entity_id, "2026-11-01T10:00:00+00:00", {"event_type": "single_press"})
        eids.append(ent.entity_id)
    bridge = await _setup(hass, config_entry)

    (acc,) = (a for spec, a in bridge._accessories if spec.key.startswith("device:"))
    switches = [s for s in acc.services if s.display_name == "StatelessProgrammableSwitch"]
    assert len(switches) == 2 and acc.get_service("ServiceLabel") is not None

    char = switches[1].get_characteristic("ProgrammableSwitchEvent")
    with patch.object(type(char), "notify") as notify:
        hass.states.async_set(eids[1], "2026-11-03T08:00:00+00:00", {"event_type": "double_press"})
        await hass.async_block_till_done()
    assert notify.call_count == 1


async def test_appliances_materialize(hass, config_entry, caplog):
    MockConfigEntry(domain="acme").add_to_hass(hass)
    reg = er.async_get(hass)
    entities = {
        "climate": ("ac", "cool", {"current_temperature": 25, "temperature": 22,
                                   "fan_mode": "low", "swing_mode": "off"},
                    {"supported_features": 1 | 8 | 32,
                     "capabilities": {"hvac_modes": ["off", "heat", "cool"],
                                      "fan_modes": ["low", "high"],
                                      "swing_modes": ["off", "on"]}}),
        "humidifier": ("hum", "on", {"humidity": 50, "current_humidity": 40}, {}),
        "alarm_control_panel": ("alarm", "disarmed", {}, {"supported_features": 3}),
        "valve": ("valve", "closed", {}, {"supported_features": 3}),
        "water_heater": ("tank", "eco", {"temperature": 55, "current_temperature": 50},
                         {"capabilities": {"min_temp": 40, "max_temp": 65}}),
        "vacuum": ("robot", "docked", {}, {"supported_features": 8192 | 16}),
        "lawn_mower": ("mower", "docked", {}, {}),
        "scene": ("evening", "unknown", {}, {}),
        "script": ("bedtime", "off", {}, {}),
        "button": ("doorbell_chime", "unknown", {}, {}),
        "select": ("mode", "Home", {}, {"capabilities": {"options": ["Home", "Away"]}}),
    }
    ids = {}
    for domain, (uid, state, attrs, kw) in entities.items():
        ent = reg.async_get_or_create(domain, "acme", uid, original_name=uid, **kw)
        hass.states.async_set(ent.entity_id, state, attrs)
        ids[domain] = ent.entity_id
    calls = async_mock_service(hass, "climate", "set_hvac_mode")
    fan_calls = async_mock_service(hass, "climate", "set_fan_mode")

    bridge = await _setup(hass, config_entry)
    assert "Skipping" not in caplog.text
    assert "Error" not in caplog.text
    for domain, entity_id in ids.items():
        assert _accessory(bridge, entity_id) is not None, domain

    acc = _hap_accessory(bridge, ids["climate"])
    hc = acc.get_service("HeaterCooler")
    assert hc.get_characteristic("TargetHeaterCoolerState").properties["ValidValues"] == {
        "Heat": 1, "Cool": 2}
    iid = acc.iid_manager.get_iid
    bridge._driver.set_characteristics({"characteristics": [
        {"aid": acc.aid, "iid": iid(hc.get_characteristic("TargetHeaterCoolerState")), "value": 1},
        {"aid": acc.aid, "iid": iid(hc.get_characteristic("RotationSpeed")), "value": 100},
    ]}, ("127.0.0.1", 50000))
    await hass.async_block_till_done()
    assert calls[0].data["hvac_mode"] == "heat"
    assert fan_calls[0].data["fan_mode"] == "high"

    (select,) = (a for spec, a in bridge._accessories if ids["select"] in spec.services[0].watched())
    names = [s.get_characteristic("Name").value for s in select.services if s.display_name == "Switch"]
    assert names == ["Home", "Away"]


async def test_accessories_past_the_limit_go_to_another_bridge(hass, config_entry):
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    lights = []
    for n in range(3):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=source.entry_id, identifiers={("acme", f"l{n}")}, name=f"Lamp {n}")
        ent = er.async_get(hass).async_get_or_create(
            "light", "acme", f"l{n}", device_id=device.id, original_name=f"Lamp {n}",
            capabilities={"supported_color_modes": ["onoff"]})
        hass.states.async_set(ent.entity_id, "on")
        lights.append(ent.entity_id)

    with patch("custom_components.hap_bridge.bridge.MAX_BRIDGED_ACCESSORIES", 2):
        bridge = await _setup(hass, config_entry)
        overflow = f"{config_entry.entry_id}_bridge2"
        assert set(bridge.units) == {config_entry.entry_id, overflow}
        assert len(bridge.units[config_entry.entry_id].accessories) == 2
        moved = bridge.units[overflow].accessories[0][0].key
        assert bridge.units[overflow].name == "Test Bridge 2"
        assert bridge.units[overflow].driver.state.port != 21099
        # Its own pairing code and reset button.
        notes = persistent_notification._async_get_or_create_notifications(hass)
        assert f"{DOMAIN}_{overflow}_pairing" in notes
        assert er.async_get(hass).async_get_entity_id(
            "button", DOMAIN, f"{overflow}_reset_pairing")

        # After a reload the accessory is still on the second bridge.
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        bridge = config_entry.runtime_data
        assert [s.key for s, _a in bridge.units[overflow].accessories] == [moved]


async def test_alarm_codes_option(hass, config_entry):
    hass.states.async_set("alarm_control_panel.house", "disarmed", {"code_format": "number"})
    hass.states.async_set("alarm_control_panel.open", "disarmed", {"code_format": None})
    await _setup(hass, config_entry)
    result = await _options_step(hass, config_entry, "alarm_codes")
    assert list(result["data_schema"].schema) == ["alarm_control_panel.house"]
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"alarm_control_panel.house": " 1234 "})
    await hass.async_block_till_done()
    assert config_entry.options["alarm_codes"] == {"alarm_control_panel.house": "1234"}

    result = await _options_step(hass, config_entry, "alarm_codes")
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert config_entry.options["alarm_codes"] == {}


async def test_sprinkler_zone_closes_after_its_run_time(hass, config_entry):
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("acme", "yard")}, name="Yard")
    zones = []
    for n in (1, 2):
        ent = er.async_get(hass).async_get_or_create(
            "valve", "acme", f"z{n}", device_id=device.id, original_name=f"Zone {n}",
            original_device_class="water")
        hass.states.async_set(ent.entity_id, "closed")
        zones.append(ent.entity_id)
    opened = async_mock_service(hass, "valve", "open_valve")
    closed = async_mock_service(hass, "valve", "close_valve")
    bridge = await _setup(hass, config_entry)

    acc = next(acc for spec, acc in bridge._accessories if spec.category == "SPRINKLER")
    valve = next(s for s in acc.services if s.display_name == "Valve"
                 and s.get_characteristic("ServiceLabelIndex").value == 1)
    valve.get_characteristic("SetDuration").client_update_value(60)
    valve.get_characteristic("Active").client_update_value(1)
    await hass.async_block_till_done()
    assert opened and opened[0].data["entity_id"] == zones[0]
    hass.states.async_set(zones[0], "open")
    await hass.async_block_till_done()
    assert 0 < valve.get_characteristic("RemainingDuration").value <= 60

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert closed and closed[0].data["entity_id"] == zones[0]

    # Started again, then closed from Home Assistant: the run is forgotten.
    valve.get_characteristic("Active").client_update_value(1)
    await hass.async_block_till_done()
    hass.states.async_set(zones[0], "open")
    await hass.async_block_till_done()
    hass.states.async_set(zones[0], "closed")
    await hass.async_block_till_done()
    assert valve.get_characteristic("RemainingDuration").value == 0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=200))
    await hass.async_block_till_done()
    assert len(closed) == 1


async def test_anyone_home_with_two_people(hass, config_entry):
    people = []
    for name in ("alex", "sam"):
        ent = er.async_get(hass).async_get_or_create("person", "person", name)
        hass.states.async_set(ent.entity_id, "not_home")
        people.append(ent.entity_id)
    bridge = await _setup(hass, config_entry)
    anyone = next(a for a in bridge.diagnostics()["accessories"] if a["name"] == "Anyone Home")
    assert sorted(anyone["watches"]) == people
    acc = next(acc for spec, acc in bridge._accessories if spec.key == "anyone_home")
    char = acc.get_service("OccupancySensor").get_characteristic("OccupancyDetected")
    assert char.value == 0
    hass.states.async_set(people[1], "home")
    await hass.async_block_till_done()
    assert char.value == 1


async def test_backup_and_restore_into_a_new_bridge(hass, config_entry, lamp, tmp_path):
    from pyhap.accessory_driver import AccessoryDriver

    from homeassistant.const import CONF_NAME, CONF_PORT

    hass.config.config_dir = str(tmp_path)
    (tmp_path / ".storage").mkdir()

    hass.config_entries.async_update_entry(config_entry, options={"names": {"x": "Y"}})
    old = await _setup(hass, config_entry)
    old._driver.state.add_paired_client(str(uuid4()).encode(), b"k" * 32, b"\x01")
    await hass.async_add_executor_job(AccessoryDriver.persist, old._driver)
    old_mac, old_aid = old._driver.state.mac, _hap_accessory(old, lamp).aid

    result = await hass.services.async_call(
        DOMAIN, "backup", {"config_entry_id": config_entry.entry_id},
        blocking=True, return_response=True)
    assert result["units"] == 1 and result["path"].startswith("hap_bridge_backups/")

    # The bridge is removed and added again: a new entry, a new identity.
    new_entry = MockConfigEntry(
        domain=DOMAIN, title="New", data={CONF_NAME: "New", CONF_PORT: 21199})
    new_entry.add_to_hass(hass)
    await hass.config_entries.async_unload(config_entry.entry_id)
    new = await _setup(hass, new_entry)
    assert new._driver.state.mac != old_mac

    await hass.services.async_call(
        DOMAIN, "restore", {"config_entry_id": new_entry.entry_id,
                            "file": result["path"].split("/")[-1]},
        blocking=True, return_response=True)
    await hass.async_block_till_done()
    new = new_entry.runtime_data
    assert new._driver.state.mac == old_mac
    assert len(new._driver.state.paired_clients) == 1
    assert _hap_accessory(new, lamp).aid == old_aid
    assert new_entry.options == {"names": {"x": "Y"}}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "restore", {"config_entry_id": new_entry.entry_id,
                                "file": "../../etc/passwd"}, blocking=True)
    await hass.config_entries.async_unload(new_entry.entry_id)
