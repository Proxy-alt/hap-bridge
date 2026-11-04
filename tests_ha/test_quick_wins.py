"""Identify, timed writes, tile names, pairings, camera settings and snapshots."""

from unittest.mock import AsyncMock, patch
import uuid

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.components.camera import Image
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.hap_bridge.const import DOMAIN


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _device(hass, name, *entities):
    """A device with (domain, unique id, registry kwargs, state) entities."""
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("acme", name)}, name=name)
    ids = []
    for domain, unique_id, kwargs, state in entities:
        ent = er.async_get(hass).async_get_or_create(
            domain, "acme", unique_id, device_id=device.id, config_entry=source, **kwargs)
        hass.states.async_set(ent.entity_id, state, {"friendly_name": kwargs.pop(
            "_friendly", None)} if kwargs.get("_friendly") else {})
        ids.append(ent.entity_id)
    return ids


def _acc(bridge, entity_id):
    return next(acc for spec, acc in bridge.accessories()
                if any(entity_id in s.watched() for s in spec.services))


async def test_identify_presses_the_identify_button(hass, config_entry):
    lamp, button = _device(
        hass, "Desk", ("light", "l", {}, "on"),
        ("button", "id", {"original_device_class": "identify"}, "unknown"))
    bridge = await _setup(hass, config_entry)
    # After setup: the bridge's own buttons load the real button integration.
    presses = async_mock_service(hass, "button", "press")
    acc = _acc(bridge, lamp)
    acc.get_service("AccessoryInformation").get_characteristic("Identify").client_update_value(True)
    await hass.async_block_till_done()
    assert presses and presses[0].data["entity_id"] == button
    assert button not in bridge.diagnostics()["excluded"]


async def test_locks_use_timed_writes(hass, config_entry):
    (lock,) = _device(hass, "Front", ("lock", "front", {}, "locked"))
    bridge = await _setup(hass, config_entry)
    char = _acc(bridge, lock).get_service("LockMechanism").get_characteristic("LockTargetState")
    assert "tw" in char.properties["Permissions"]


async def strip(ctx):
    """A power strip profile: both switches as tiles of one accessory."""
    acc = ctx.accessory(category="OUTLET")
    for eid in ctx.entities("switch"):
        ctx.blocks.switch(acc.service("Switch"), eid)


async def test_tiles_get_names_and_keep_home_app_renames(hass, config_entry):
    from tests_ha.test_developer import ACME, _profile, _ship

    source = MockConfigEntry(domain=ACME)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={(ACME, "strip")}, name="Strip")
    one, two = (
        er.async_get(hass).async_get_or_create(
            "switch", ACME, uid, device_id=device.id, config_entry=source).entity_id
        for uid in ("o1", "o2"))
    hass.states.async_set(one, "on", {"friendly_name": "Strip Kettle"})
    hass.states.async_set(two, "off", {"friendly_name": "Strip Lamp"})
    with _ship(_profile(strip)):
        bridge = await _setup(hass, config_entry)
    acc = _acc(bridge, one)
    switches = [s for s in acc.services if s.display_name == "Switch"]
    names = sorted(s.get_characteristic("ConfiguredName").value for s in switches)
    assert names == ["Kettle", "Lamp"]

    kettle = next(s for s in switches
                  if s.get_characteristic("ConfiguredName").value == "Kettle")
    kettle.get_characteristic("ConfiguredName").client_update_value("Tea")
    with _ship(_profile(strip)):
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
    acc = _acc(config_entry.runtime_data, one)
    names = sorted(s.get_characteristic("ConfiguredName").value
                   for s in acc.services if s.display_name == "Switch")
    assert names == ["Lamp", "Tea"]


async def test_single_tile_accessories_are_left_alone(hass, config_entry, lamp):
    bridge = await _setup(hass, config_entry)
    bulb = _acc(bridge, lamp).get_service("Lightbulb")
    with pytest.raises(ValueError):
        bulb.get_characteristic("ConfiguredName")


async def test_list_and_remove_pairings(hass, config_entry, lamp):
    bridge = await _setup(hass, config_entry)
    state = bridge.units[config_entry.entry_id].driver.state
    phone, hub = uuid.uuid4(), uuid.uuid4()
    state.add_paired_client(str(phone).encode(), b"k" * 32, b"\x01")
    state.add_paired_client(str(hub).encode(), b"k" * 32, b"\x00")

    result = await hass.services.async_call(DOMAIN, "list_pairings", {}, blocking=True,
                                            return_response=True)
    bridge_unit = next(u for u in result["units"] if u["unit_id"] == config_entry.entry_id)
    assert {c["id"]: c["admin"] for c in bridge_unit["controllers"]} == {
        str(phone): True, str(hub): False}

    await hass.services.async_call(DOMAIN, "remove_pairing", {"pairing_id": str(hub)},
                                   blocking=True)
    assert set(state.paired_clients) == {phone}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "remove_pairing", {"pairing_id": str(hub)},
                                       blocking=True)


async def test_camera_settings_are_found_and_snapshots_respect_camera_off(hass, config_entry):
    cam, _motion, ir, led = _device(
        hass, "Porch",
        ("camera", "cam", {}, "idle"),
        ("binary_sensor", "motion", {"original_device_class": "motion"}, "off"),
        ("switch", "ir", {"translation_key": "ir_lights"}, "on"),
        ("switch", "led", {"translation_key": "status_light"}, "off"))
    calls = async_mock_service(hass, "switch", "turn_on")
    bridge = await _setup(hass, config_entry)
    acc = next(u.root for u in bridge.units.values() if u.name == "Porch")
    mode = acc.get_service("CameraOperatingMode")
    assert mode.get_characteristic("NightVision").value is True
    assert mode.get_characteristic("CameraOperatingModeIndicator").value == 0
    mode.get_characteristic("CameraOperatingModeIndicator").client_update_value(1)
    await hass.async_block_till_done()
    assert calls and calls[0].data["entity_id"] == led
    # The switches are camera settings now, not tiles of their own.
    assert all(ir not in (a["watches"]) or a["name"] == "Porch"
               for a in bridge.diagnostics()["accessories"])

    image = AsyncMock(return_value=Image("image/jpeg", b"jpeg"))
    with patch("custom_components.hap_bridge.camera.ha_camera.async_get_image", image):
        assert await acc.async_get_snapshot(
            {"image-width": 640, "image-height": 360, "reason": 0}) == b"jpeg"
        mode.get_characteristic("HomeKitCameraActive").client_update_value(0)
        with pytest.raises(PermissionError):
            await acc.async_get_snapshot({"image-width": 640, "image-height": 360, "reason": 1})


async def test_camera_options_save_prebuffer_choice(hass, config_entry):
    _device(hass, "Porch", ("camera", "cam", {}, "idle"),
            ("binary_sensor", "motion", {"original_device_class": "motion"}, "off"))
    await _setup(hass, config_entry)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "camera_audio"})
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera_no_prebuffer": ["camera.porch"]})
    await hass.async_block_till_done()
    assert config_entry.options["camera_no_prebuffer"] == ["camera.porch"]
    acc = next(u.root for u in config_entry.runtime_data.units.values() if u.name == "Porch")
    assert acc.recording._prebuffer_wanted is False
