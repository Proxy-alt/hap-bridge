"""HomeKit Secure Video: the data stream, the recording TLVs, and the clip stream."""

import asyncio
import base64
import functools
import os
import struct
from types import SimpleNamespace
import uuid

from pyhap import tlv
from pyhap.accessory import Accessory
from pyhap.loader import Loader
import pytest

from hb import hds, recording


def run(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


# -- encoding ---------------------------------------------------------------------------


def test_codec_round_trip():
    value = {
        "protocol": "dataSend", "id": hds.Int64(123456789), "n": 7, "neg": -1, "big": 70000,
        "huge": 2**40, "f": 1.5, "flag": True, "off": False, "s": "x" * 40, "d": b"\x01" * 300,
        "list": list(range(15)), "nested": {"a": [1, "two", b"3"]},
        "u": uuid.UUID("12345678-1234-5678-1234-567812345678"),
    }
    assert hds.decode(hds.encode(value)) == {**value, "id": 123456789}


def test_codec_wire_format():
    # Small ints and short strings have one-byte tags; Int64 is forced to 8 bytes.
    assert hds.encode(5) == b"\x0d"
    assert hds.encode("hi") == b"\x42hi"
    assert hds.encode(hds.Int64(1)) == b"\x33" + struct.pack("<q", 1)
    assert hds.encode({"a": 1}) == b"\xe1\x41a\x09"
    # None values are left out, like undefined in HAP-NodeJS.
    assert hds.encode({"a": None}) == b"\xe0"


def test_codec_back_references():
    # 0xA0 refers to the first tracked value ("streamId" here).
    data = b"\xe2" + b"\x48streamId" + b"\x30\x64" + b"\xa0" + b"\x09"
    assert hds.decode(data) == {"streamId": 1}  # second key repeats the first, overwriting


# -- the encrypted channel ----------------------------------------------------------------


class Controller:
    """Plays the home hub's side of an HDS connection."""

    def __init__(self, reader, writer, read_key, write_key):
        self.reader, self.writer = reader, writer
        self.read_key, self.write_key = read_key, write_key
        self.out = self.inn = 0

    def send(self, header, message):
        self.writer.write(hds.seal_frame(self.write_key, self.out, hds.pack_message(header, message)))
        self.out += 1

    async def receive(self):
        header = await self.reader.readexactly(4)
        body = await self.reader.readexactly(int.from_bytes(header[1:], "big") + 16)
        payload = hds.open_frame(self.read_key, self.inn, header, body)
        self.inn += 1
        return hds.unpack_message(payload)


async def _connect(server, secret=b"s" * 32):
    controller_salt = os.urandom(32)
    accessory_salt = server.prepare(secret, controller_salt)
    read_key, write_key = hds.derive_keys(secret, controller_salt, accessory_salt)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    controller = Controller(reader, writer, read_key, write_key)
    controller.send({"protocol": "control", "request": "hello", "id": hds.Int64(1)}, {})
    head, _ = await controller.receive()
    assert head == {"protocol": "control", "response": "hello", "id": 1, "status": 0}
    return controller


@run
async def test_hello_and_requests():
    server = hds.DataStreamServer()
    seen = []

    def on_open(connection, request_id, message):
        seen.append(message)
        connection.send_response("dataSend", "open", request_id, message={"status": 0})

    server.on_request("dataSend", "open", on_open)
    await server.async_start("127.0.0.1")
    try:
        controller = await _connect(server)
        controller.send({"protocol": "dataSend", "request": "open", "id": hds.Int64(9)},
                        {"streamId": 3, "type": "ipcamera.recording"})
        head, message = await controller.receive()
        assert head["response"] == "open" and head["id"] == 9 and message == {"status": 0}
        assert seen == [{"streamId": 3, "type": "ipcamera.recording"}]

        # A request nobody handles gets MISSING_PROTOCOL.
        controller.send({"protocol": "nope", "request": "x", "id": hds.Int64(2)}, {})
        head, _ = await controller.receive()
        assert head["status"] == hds.STATUS_MISSING_PROTOCOL
    finally:
        await server.async_stop()


@run
async def test_wrong_keys_are_dropped():
    server = hds.DataStreamServer()
    await server.async_start("127.0.0.1")
    try:
        server.prepare(b"s" * 32, os.urandom(32))
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        wrong = os.urandom(32)
        writer.write(hds.seal_frame(wrong, 0, hds.pack_message(
            {"protocol": "control", "request": "hello", "id": hds.Int64(1)}, {})))
        assert await reader.read() == b""  # closed without answering
    finally:
        await server.async_stop()


# -- recording configuration --------------------------------------------------------------


def _selected(profile=1, level=2, width=1280, height=720, fps=30, rate=4):
    t = recording._tlv
    rec = t(1, struct.pack("<i", 4000), 2, struct.pack("<q", 1),
            3, t(1, 0, 2, t(1, struct.pack("<i", 4000))))
    video = t(1, 0, 2, t(1, profile, 2, level, 3, struct.pack("<i", 2000),
                         4, struct.pack("<i", 4000)),
              3, t(1, struct.pack("<H", width), 2, struct.pack("<H", height), 3, fps))
    audio = t(1, 0, 2, t(1, 1, 2, 0, 3, rate, 4, struct.pack("<I", 64)))
    return base64.b64encode(t(1, rec, 2, video, 3, audio)).decode()


def test_parse_selected_configuration():
    config = recording.parse_selected(_selected())
    assert config == recording.RecordingConfig(
        prebuffer_ms=4000, triggers=1, fragment_ms=4000, profile="main", level="4.0",
        bitrate=2000, iframe_ms=4000, width=1280, height=720, fps=30,
        audio_channels=1, sample_rate=44100, audio_bitrate=64,
    )


def test_supported_configuration_tlvs():
    camera = tlv.decode(base64.b64decode(recording.supported_camera_configuration(3)))
    assert int.from_bytes(camera[b"\x02"], "little") == 3  # motion | doorbell
    assert struct.unpack("<i", camera[b"\x01"])[0] == recording.PREBUFFER_MS

    video = base64.b64decode(recording.supported_video_configuration())
    # Three resolutions, separated by empty 0x00 items.
    assert video.count(struct.pack("<H", 1280)) == 1 and b"\x00\x00" in video


def test_ffmpeg_command():
    config = recording.parse_selected(_selected())
    command = recording.ffmpeg_command("ffmpeg", "rtsp://cam/1", config, audio=False)
    assert command[:6] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "rtsp://cam/1"]
    assert "-an" in command and command[-1] == "pipe:1"
    assert "expr:gte(t,n_forced*4)" in command
    with_audio = recording.ffmpeg_command("ffmpeg", "-rtsp_transport tcp -i rtsp://x", config,
                                          audio=True)
    assert with_audio[4:8] == ["-rtsp_transport", "tcp", "-i", "rtsp://x"]
    assert "aac" in with_audio and "44100" in with_audio


def _box(kind, payload=b""):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


@run
async def test_mp4_segments():
    reader = asyncio.StreamReader()
    init = _box(b"ftyp", b"isom") + _box(b"moov", b"m" * 20)
    frag1 = _box(b"moof", b"a") + _box(b"mdat", b"x" * 100)
    frag2 = _box(b"styp") + _box(b"moof", b"b") + _box(b"mdat", b"y")
    reader.feed_data(init + frag1 + frag2)
    reader.feed_eof()
    segments = [s async for s in recording.mp4_segments(reader)]
    assert segments == [(True, init), (False, frag1), (False, frag2)]


# -- the services and the clip stream -------------------------------------------------------


def _camera_accessory():
    loader = Loader()
    recording.register_definitions(loader)
    driver = SimpleNamespace(loader=loader, writing_client=None, http_server=None,
                             publish=lambda *a, **k: None)
    acc = Accessory(driver, "Front Door")
    acc.add_preload_service("MotionSensor")
    acc.add_preload_service("CameraRTPStreamManagement", unique_id=0)
    return acc


def _manager(acc=None, saved=None, triggered=lambda: True, **kw):
    acc = acc or _camera_accessory()
    saves = []

    async def source():
        return "rtsp://cam"

    manager = recording.RecordingManager(
        acc, name="Front Door", triggers=recording.Triggers(motion=True, doorbell=False),
        triggered=triggered, get_source=source, ffmpeg="ffmpeg", audio=False,
        load=lambda: saved, save=saves.append, **kw)
    return manager, saves


def test_services_are_built_and_linked():
    manager, _ = _manager()
    acc = manager.acc
    rec = acc.get_service("CameraRecordingManagement")
    linked = {s.display_name for s in rec.linked_services}
    assert linked == {"DataStreamTransportManagement", "MotionSensor"}
    assert acc.get_service("MotionSensor").get_characteristic("StatusActive").value is True
    assert acc.get_service("CameraRTPStreamManagement").get_characteristic("Active").value == 1
    # Everything serializes with IIDs, as HAP-python will at startup.
    assert acc.to_HAP()


def test_state_is_saved_and_restored():
    manager, saves = _manager()
    rec = manager.recording_service
    rec.get_characteristic("SelectedCameraRecordingConfiguration").client_update_value(_selected())
    rec.get_characteristic("Active").client_update_value(1)
    assert saves[-1]["active"] == 1 and saves[-1]["selected"]

    restored, _ = _manager(saved=saves[-1])
    assert restored.config == manager.config
    assert restored.recording_service.get_characteristic("Active").value == 1

    # A different set of triggers changes what's offered: the hub must choose again.
    other, _ = _manager(saved={**saves[-1], "hash": "old"})
    assert other.config is None and other.state["active"] == 1


def test_setup_data_stream_transport():
    acc = _camera_accessory()
    secret = b"k" * 32
    connection = SimpleNamespace(hap_crypto=SimpleNamespace(shared_key=secret))
    acc.driver.http_server = SimpleNamespace(connections={("1.2.3.4", 5): connection})
    acc.driver.writing_client = ("1.2.3.4", 5)
    manager, _ = _manager(acc)
    manager.server.port = 40000
    prepared = []
    manager.server.prepare = lambda s, salt, client: prepared.append((s, salt)) or b"a" * 32

    salt = os.urandom(32)
    request = base64.b64encode(recording._tlv(1, 0, 2, 0, 3, salt)).decode()
    char = manager.transport_service.get_characteristic("SetupDataStreamTransport")
    response = tlv.decode(base64.b64decode(char.client_update_value(request)))
    assert response[b"\x01"] == b"\x00"
    assert tlv.decode(response[b"\x02"])[b"\x01"] == struct.pack("<H", 40000)
    assert response[b"\x03"] == b"a" * 32
    assert prepared == [(secret, salt)]

    # Unknown HAP connection (no shared secret): an error status, not a crash.
    acc.driver.writing_client = ("9.9.9.9", 1)
    assert tlv.decode(base64.b64decode(char.client_update_value(request)))[b"\x01"] == b"\x01"


class FakeConnection:
    def __init__(self):
        self.responses, self.events, self.on_close, self.closed = [], [], [], False

    def send_response(self, protocol, topic, request_id, status=0, message=None):
        self.responses.append((topic, request_id, status, message))

    def send_event(self, protocol, topic, message):
        self.events.append((topic, message))

    async def drain(self):
        await asyncio.sleep(0)


def _open(stream_id=1):
    return {"target": "controller", "type": "ipcamera.recording", "streamId": stream_id}


@run
async def test_open_is_refused_until_recording_is_on():
    manager, _ = _manager()
    connection = FakeConnection()
    await manager._async_open(connection, 5, _open())
    assert connection.responses == [("open", 5, 6, {"status": hds.REASON_NOT_ALLOWED})]

    manager.state["active"] = 1
    await manager._async_open(connection, 6, _open())
    assert connection.responses[-1][3] == {"status": hds.REASON_INVALID_CONFIGURATION}


@run
async def test_clip_is_streamed_in_order(monkeypatch):
    clock = {"t": 0.0}
    motion = {"on": True}
    manager, _ = _manager(triggered=lambda: motion["on"])
    manager.state["active"] = 1
    manager.config = recording.parse_selected(_selected())
    monkeypatch.setattr(recording, "CHUNK", 10)

    async def segments(_config):
        yield True, b"I" * 15  # two chunks
        for step in range(30):
            clock["t"] = step * 4.0
            motion["on"] = step < 3
            yield False, b"F%02d" % step

    manager._async_segments = segments
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", lambda: clock["t"])
    connection = FakeConnection()
    await manager._async_open(connection, 7, _open(stream_id=42))
    assert connection.responses == [("open", 7, 0, {"status": 0})]
    await manager._stream

    data = [m for topic, m in connection.events if topic == "data"]
    first, second = (d["packets"][0] for d in data[:2])
    assert first["metadata"] == {"dataType": "mediaInitialization", "dataSequenceNumber": 1,
                                 "dataChunkSequenceNumber": 1, "isLastDataChunk": False,
                                 "dataTotalSize": 15}
    assert second["metadata"]["isLastDataChunk"] and "endOfStream" in data[1]
    assert data[2]["packets"][0]["metadata"]["dataSequenceNumber"] == 2
    assert all(d["streamId"] == 42 for d in data)
    # Motion stops at 12 s; the clip ends once it's been quiet for the tail
    # (and is at least the minimum length), with endOfStream on the last one.
    assert data[-1]["endOfStream"] is True
    assert [d.get("endOfStream") for d in data[2:-1]] == [False] * (len(data) - 3)
    ended = int(data[-1]["packets"][0]["data"][1:])
    assert ended * 4.0 >= 12 + recording.TAIL_SECONDS


@run
async def test_hub_closing_stops_the_clip():
    manager, _ = _manager()
    manager.state["active"] = 1
    manager.config = recording.parse_selected(_selected())
    stopped = asyncio.Event()

    async def segments(_config):
        try:
            yield True, b"init"
            while True:
                await asyncio.sleep(0.01)
                yield False, b"frag"
        finally:
            stopped.set()

    manager._async_segments = segments
    connection = FakeConnection()
    await manager._async_open(connection, 1, _open(stream_id=3))
    await asyncio.sleep(0.05)
    manager._on_close(connection, {"streamId": 3, "reason": 0})
    with pytest.raises(asyncio.CancelledError):
        await manager._stream
    assert stopped.is_set()


def test_session_keys_are_kept():
    from pyhap import hap_protocol

    recording.capture_session_keys()
    crypto = hap_protocol.HAPCrypto(b"z" * 32)
    assert crypto.shared_key == b"z" * 32


# -- prebuffer ------------------------------------------------------------------------------


@run
async def test_prebuffer_keeps_the_last_fragments_and_follows_live():
    feed: asyncio.Queue = asyncio.Queue()

    async def source():
        while (item := await feed.get()) is not None:
            yield item

    prebuffer = recording.Prebuffer(source, keep_fragments=2, name="cam")
    prebuffer.start()
    for item in [(True, b"init"), (False, b"f1"), (False, b"f2"), (False, b"f3")]:
        feed.put_nowait(item)
    await asyncio.sleep(0.01)
    assert prebuffer.init == b"init" and prebuffer.fragments == [b"f2", b"f3"]

    clip = prebuffer.clip()
    got = [await anext(clip) for _ in range(3)]
    assert got == [(True, b"init"), (False, b"f2"), (False, b"f3")]
    feed.put_nowait((False, b"f4"))
    assert await anext(clip) == (False, b"f4")

    # ffmpeg restarting means a new init segment: the clip can't continue.
    feed.put_nowait((True, b"init2"))
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(clip), 1)
    await prebuffer.async_stop()
    assert not prebuffer.running and prebuffer.init is None


@run
async def test_prebuffer_restarts_ffmpeg_with_backoff(monkeypatch):
    runs = []
    monkeypatch.setattr(recording, "RESTART_DELAYS", (0.01,))

    async def source():
        runs.append(1)
        yield True, b"init"
        raise RuntimeError("ffmpeg died")

    prebuffer = recording.Prebuffer(source, keep_fragments=1, name="cam")
    prebuffer.start()
    await asyncio.sleep(0.1)
    await prebuffer.async_stop()
    assert len(runs) >= 3


@run
async def test_clips_come_from_the_prebuffer_while_recording_is_on():
    manager, _ = _manager()
    manager.server.port = 40000
    starts = []

    async def ffmpeg(config, audio):
        starts.append(audio)
        yield True, b"init"
        for n in range(3):
            yield False, b"pre%d" % n
        await asyncio.Event().wait()  # keep running

    manager._async_ffmpeg_segments = ffmpeg
    rec = manager.recording_service
    rec.get_characteristic("SelectedCameraRecordingConfiguration").client_update_value(_selected())
    assert manager.prebuffer is None  # recording not on yet
    rec.get_characteristic("Active").client_update_value(1)
    assert manager.prebuffer is not None
    await asyncio.sleep(0.01)
    assert manager.prebuffer.fragments == [b"pre2"]  # 4 s prebuffer = one 4 s fragment

    clip = manager._async_segments(manager.config)
    assert [await anext(clip), await anext(clip)] == [(True, b"init"), (False, b"pre2")]
    await clip.aclose()

    # Turning recording off stops it; with the prebuffer off, clips start ffmpeg.
    rec.get_characteristic("Active").client_update_value(0)
    assert manager.prebuffer is None
    assert starts == [False]
    await asyncio.sleep(0)


def test_snapshots_follow_the_home_app_settings():
    manager, _ = _manager()
    assert manager.snapshot_allowed(0) and manager.snapshot_allowed(1)
    manager.mode_service.get_characteristic("PeriodicSnapshotsActive").client_update_value(0)
    assert not manager.snapshot_allowed(0) and manager.snapshot_allowed(1)
    manager.mode_service.get_characteristic("HomeKitCameraActive").client_update_value(0)
    assert not manager.snapshot_allowed(1) and not manager.snapshot_allowed(None)


def test_profile_built_operating_mode_is_reused():
    acc = _camera_accessory()
    mode = acc.add_preload_service("CameraOperatingMode", chars=["NightVision"])
    manager, _ = _manager(acc)
    assert manager.mode_service is mode
    assert mode.get_characteristic("HomeKitCameraActive").value == 1
    assert [s.display_name for s in acc.services].count("CameraOperatingMode") == 1
