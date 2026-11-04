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


VIDEO = {"v_profile_id": b"\x01", "fps": 30, "v_max_bitrate": 300, "v_ssrc": 1,
         "v_srtp_key": "KEY", "address": "192.168.1.5", "v_port": 51000}
AUDIO = {"a_port": 51002, "a_srtp_key": "AKEY", "a_ssrc": 7, "a_sample_rate": 16,
         "a_max_bitrate": 24, "a_payload_type": b"\x6e"}


def test_ffmpeg_audio_output():
    from custom_components.hap_bridge.camera import ffmpeg_output

    with_audio = ffmpeg_output({**VIDEO, **AUDIO}, audio=True)
    assert "-c:a libopus" in with_audio and "-ar 16k" in with_audio
    assert "-payload_type 110" in with_audio and "-srtp_out_params AKEY" in with_audio
    assert "srtp://192.168.1.5:51002" in with_audio
    # Off, or the Home app asked for none: video only.
    assert "-c:a" not in ffmpeg_output({**VIDEO, **AUDIO})
    assert "-c:a" not in ffmpeg_output({**VIDEO, "a_port": 51002}, audio=True)


async def test_camera_audio_from_options(hass, config_entry, devices):
    bridge = await _setup(hass, config_entry)
    assert not _unit(bridge, "Front Door").root.audio

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "camera_audio"})
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera_audio": [devices["camera"]]})
    await hass.async_block_till_done()
    assert _unit(config_entry.runtime_data, "Front Door").root.audio


async def test_profile_can_turn_camera_audio_on(hass):
    from custom_components.hap_bridge import testing

    async def profile(ctx):
        ctx.blocks.camera("camera.door", audio=True)

    result = await testing.async_describe(profile, [testing.entry("camera.door")])
    assert result.accessory().camera_audio
    assert not result.accessory().camera_live_snapshots

    async def live(ctx):
        ctx.blocks.camera("camera.door", live_snapshots=True)

    result = await testing.async_describe(live, [testing.entry("camera.door")])
    assert result.accessory().camera_live_snapshots


async def test_video_doorbell_offers_secure_video(hass, config_entry, devices):
    bridge = await _setup(hass, config_entry)
    cam = _unit(bridge, "Front Door").root
    rec = cam.get_service("CameraRecordingManagement")
    assert rec is not None and cam.recording.triggers.bits == 0x02  # doorbell only
    assert cam.get_service("DataStreamTransportManagement") in rec.linked_services
    assert cam.recording.server.port == 40001

    # The Home app writes the transport setup through the driver; the answer
    # comes back as the write response, keyed to that HAP connection.
    import base64

    from pyhap import tlv

    from custom_components.hap_bridge import recording

    driver = _unit(bridge, "Front Door").driver
    driver.http_server = MagicMock()
    driver.http_server.connections = {
        CLIENT: MagicMock(hap_crypto=MagicMock(shared_key=b"k" * 32))}
    setup = cam.get_service("DataStreamTransportManagement").get_characteristic(
        "SetupDataStreamTransport")
    request = base64.b64encode(recording._tlv(1, 0, 2, 0, 3, b"s" * 32)).decode()
    aid = cam.aid
    iid = cam.iid_manager.get_iid(setup)
    result = driver.set_characteristics(
        {"characteristics": [{"aid": aid, "iid": iid, "value": request, "r": True}]}, CLIENT)
    (answer,) = result["characteristics"]
    decoded = tlv.decode(base64.b64decode(answer["value"]))
    assert decoded[b"\x01"] == b"\x00" and len(decoded[b"\x03"]) == 32
    assert driver.writing_client is None

    # The Home app turning streaming off stops live video too.
    cam.recording.state["streaming"] = 0
    assert not await cam.start_stream({"id": "s", "stream_idx": 0}, {})


async def test_recording_triggers_from_a_profile(hass):
    from custom_components.hap_bridge import testing

    async def profile(ctx):
        ctx.blocks.camera("camera.drive", motion="binary_sensor.motion", recording={
            "motion": ["binary_sensor.person", "binary_sensor.vehicle"]})

    hass_ = testing.FakeHass()
    hass_.states.set("binary_sensor.motion", "on")
    hass_.states.set("binary_sensor.person", "off")
    hass_.states.set("binary_sensor.vehicle", "off")
    entries = [testing.entry("camera.drive"), testing.entry("binary_sensor.motion"),
               testing.entry("binary_sensor.person"), testing.entry("binary_sensor.vehicle")]
    result = await testing.async_describe(profile, entries, hass=hass_)
    result.assert_valid()
    acc = result.accessory()
    assert acc.recording.motion == ("binary_sensor.person", "binary_sensor.vehicle")
    # The camera's Motion Sensor follows the triggers, not plain motion.
    assert result.values(acc)["MotionSensor.MotionDetected"] is False

    async def off(ctx):
        ctx.blocks.camera("camera.drive", motion="binary_sensor.motion", recording=False)

    assert (await testing.async_describe(off, entries, hass=hass_)).accessory().recording is None

    async def bad(ctx):
        ctx.blocks.camera("camera.drive", recording={"motion": ["binary_sensor.motion"],
                                                     "people": True})

    with pytest.raises(ValueError, match="recording must be a dict"):
        await testing.async_describe(bad, entries, hass=hass_)


async def test_motion_event_turns_off_by_itself(hass, config_entry):
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.util import dt as dt_util

    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("x", "porch")}, name="Porch")
    cam = ent_reg.async_get_or_create("camera", "acme", "porch", device_id=dev.id)
    event = ent_reg.async_get_or_create(
        "event", "acme", "porch_motion", device_id=dev.id, original_device_class="motion")
    hass.states.async_set(cam.entity_id, "idle")
    hass.states.async_set(event.entity_id, "2026-11-01T10:00:00+00:00", {"event_type": "motion"})

    bridge = await _setup(hass, config_entry)
    porch = _unit(bridge, "Porch").root
    detected = porch.get_service("MotionSensor").get_characteristic("MotionDetected")
    assert porch.recording.triggers.bits == 0x01  # motion
    assert detected.get_value() is False

    hass.states.async_set(event.entity_id, dt_util.utcnow().isoformat(), {"event_type": "motion"})
    await hass.async_block_till_done()
    assert detected.get_value() is True and porch.recording._triggered()

    with patch("custom_components.hap_bridge.profiles.cameras.datetime") as clock:
        later = dt_util.utcnow() + timedelta(seconds=21)
        clock.now.return_value = later
        clock.fromisoformat = __import__("datetime").datetime.fromisoformat
        async_fire_time_changed(hass, later)
        await hass.async_block_till_done()
    assert detected.get_value() is False


async def test_event_motion_triggers_from_a_profile(hass):
    from custom_components.hap_bridge import testing

    async def profile(ctx):
        ctx.blocks.camera("camera.cam", doorbell="event.cam", recording={
            "motion": ["event.cam"], "doorbell": "event.cam",
            "motion_events": ["motion", "person"]})

    entries = [testing.entry("camera.cam"), testing.entry("event.cam")]
    result = await testing.async_describe(profile, entries)
    result.assert_valid()
    rec = result.accessory().recording
    assert rec.motion == ("event.cam",) and rec.doorbell == "event.cam"
    assert rec.motion_events == ("motion", "person")


async def test_live_snapshots_come_from_go2rtc(hass, config_entry, devices):
    from custom_components.hap_bridge.webrtc import WebRtcBridge

    bridge = await _setup(hass, config_entry)
    cam = _unit(bridge, "Front Door").root
    size = {"image-width": 640, "image-height": 360}
    stale = AsyncMock(return_value=Image("image/jpeg", b"event"))
    gate = asyncio.Event()

    async def frame(entity_id, *, height=None, video_profile=None):
        await gate.wait()
        return b"live"

    with patch("homeassistant.components.camera.async_get_image", stale), \
            patch.object(WebRtcBridge, "async_frame", side_effect=frame) as grab:
        # Off by default: the camera entity's own image.
        assert await cam.async_get_snapshot(size) == b"event"
        assert not grab.called

        cam.spec.camera_live_snapshots = True
        # Controllers asking at once share one frame.
        first = asyncio.create_task(cam.async_get_snapshot(size))
        second = asyncio.create_task(cam.async_get_snapshot(size))
        await asyncio.sleep(0)
        gate.set()
        assert await first == b"live" and await second == b"live"
        assert grab.call_count == 1
        assert grab.call_args.kwargs["height"] == 360

        # go2rtc couldn't take one: the camera entity's image after all.
        grab.side_effect = None
        grab.return_value = None
        assert await cam.async_get_snapshot(size) == b"event"
