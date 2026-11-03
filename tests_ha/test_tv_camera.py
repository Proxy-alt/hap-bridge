"""TVs and cameras as standalone accessories, end to end."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.components import persistent_notification
from homeassistant.components.camera import Image
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.hap_bridge.const import DOMAIN

CLIENT = ("127.0.0.1", 50000)


@pytest.fixture
async def devices(hass):
    source = MockConfigEntry(domain="androidtv_remote")
    source.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)

    tv_dev = dev_reg.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("x", "tv")}, name="Living Room TV")
    tv = ent_reg.async_get_or_create(
        "media_player", "androidtv_remote", "tv", device_id=tv_dev.id,
        original_device_class="tv", supported_features=2048 | 16384 | 1 | 8 | 1024,
        capabilities={"source_list": ["HDMI 1", "YouTube"]})
    remote = ent_reg.async_get_or_create("remote", "androidtv_remote", "tv", device_id=tv_dev.id)
    hass.states.async_set(tv.entity_id, "on", {"source": "YouTube"})
    hass.states.async_set(remote.entity_id, "on")

    cam_dev = dev_reg.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("x", "cam")}, name="Front Door")
    cam = ent_reg.async_get_or_create("camera", "acme", "front", device_id=cam_dev.id)
    bell = ent_reg.async_get_or_create(
        "event", "acme", "ring", device_id=cam_dev.id, original_device_class="doorbell")
    hass.states.async_set(cam.entity_id, "idle")
    hass.states.async_set(bell.entity_id, "2026-11-01T10:00:00+00:00")

    light = ent_reg.async_get_or_create("switch", "acme", "lamp")
    hass.states.async_set(light.entity_id, "on")
    return {"tv": tv.entity_id, "remote": remote.entity_id, "camera": cam.entity_id,
            "switch": light.entity_id}


async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _unit(bridge, name):
    return next(u for u in bridge.units.values() if u.name == name)


async def test_tv_and_camera_get_their_own_servers(hass, config_entry, devices, caplog):
    bridge = await _setup(hass, config_entry)
    assert "Skipping" not in caplog.text

    tv, cam = _unit(bridge, "Living Room TV"), _unit(bridge, "Front Door")
    bridge_unit = bridge.units[config_entry.entry_id]
    assert tv.standalone and cam.standalone and not bridge_unit.standalone
    ports = {u.driver.state.port for u in (tv, cam, bridge_unit)}
    assert len(ports) == 3 and 21099 in ports
    assert tv.root.aid == 1 and tv.root.get_service("Television") is not None
    # The switch stays on the bridge; TV and camera are not bridged.
    assert [spec.key for spec, _ in bridge_unit.accessories] == [
        f"entity:{er.async_get(hass).async_get(devices['switch']).id}"]

    notes = persistent_notification._async_get_or_create_notifications(hass)
    for unit in (tv, cam, bridge_unit):
        assert f"{DOMAIN}_{unit.id}_pairing" in notes

    reg = er.async_get(hass)
    button = reg.async_get_entity_id("button", DOMAIN, f"{tv.id}_reset_pairing")
    assert button is not None
    tv.driver.state.add_paired_client(str(uuid4()).encode(), b"k" * 32, b"\x01")
    tv.driver.async_persist = MagicMock()
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert not tv.driver.state.paired_clients


async def test_ports_survive_reload(hass, config_entry, devices):
    bridge = await _setup(hass, config_entry)
    before = {u.name: u.driver.state.port for u in bridge.units.values()}
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    after = {u.name: u.driver.state.port for u in config_entry.runtime_data.units.values()}
    assert before == after


async def test_tv_remote_keys_reach_the_remote(hass, config_entry, devices):
    commands = async_mock_service(hass, "remote", "send_command")
    sources = async_mock_service(hass, "media_player", "select_source")
    bridge = await _setup(hass, config_entry)
    tv = _unit(bridge, "Living Room TV")
    acc = tv.root
    television = acc.get_service("Television")
    iid = acc.iid_manager.get_iid
    tv.driver.set_characteristics({"characteristics": [
        {"aid": 1, "iid": iid(television.get_characteristic("RemoteKey")), "value": 7},
        {"aid": 1, "iid": iid(television.get_characteristic("ActiveIdentifier")), "value": 0},
    ]}, CLIENT)
    await hass.async_block_till_done()
    assert commands[0].data["command"] == ["DPAD_RIGHT"]
    assert sources[0].data["source"] == "HDMI 1"
    assert television.get_characteristic("ActiveIdentifier").value == 1  # YouTube


async def test_camera_snapshot_and_stream(hass, config_entry, devices):
    bridge = await _setup(hass, config_entry)
    cam = _unit(bridge, "Front Door").root
    assert cam.category == 18  # video doorbell
    assert cam.get_service("Doorbell") is not None
    assert cam.get_service("CameraRTPStreamManagement") is not None

    with patch("custom_components.hap_bridge.camera.ha_camera.async_get_image",
               AsyncMock(return_value=Image("image/jpeg", b"jpeg"))) as get_image:
        assert await cam.async_get_snapshot({"image-width": 640, "image-height": 360}) == b"jpeg"
    assert get_image.call_args.kwargs == {"width": 640, "height": 360}

    stream = MagicMock()
    stream.open = AsyncMock(return_value=True)
    stream.close = AsyncMock()
    stream.process.pid = os.getpid()  # "alive"
    reader = asyncio.StreamReader()
    reader.feed_eof()
    stream.get_reader = AsyncMock(return_value=reader)
    session = {"id": "s1", "stream_idx": 0}
    config = {"v_profile_id": b"\x01", "fps": 30, "v_max_bitrate": 300, "v_ssrc": 1,
              "v_srtp_key": "KEY", "address": "192.168.1.5", "v_port": 51000}
    with (
        patch("custom_components.hap_bridge.camera.ha_camera.async_get_stream_source",
              AsyncMock(return_value="rtsp://cam/stream")),
        patch("custom_components.hap_bridge.camera.HAFFmpeg", return_value=stream),
    ):
        assert await cam.start_stream(session, config)
    kwargs = stream.open.call_args.kwargs
    assert kwargs["input_source"] == "-i rtsp://cam/stream"
    assert "-profile:v main" in kwargs["output"]
    assert "srtp://192.168.1.5:51000" in kwargs["output"]

    await cam.stop_stream(session)
    stream.close.assert_awaited_once()
    assert "watcher" not in session


async def test_camera_without_stream_source_fails_cleanly(hass, config_entry, devices):
    bridge = await _setup(hass, config_entry)
    cam = _unit(bridge, "Front Door").root
    with patch("custom_components.hap_bridge.camera.ha_camera.async_get_stream_source",
               AsyncMock(return_value=None)):
        assert not await cam.start_stream({"id": "s", "stream_idx": 0}, {})
