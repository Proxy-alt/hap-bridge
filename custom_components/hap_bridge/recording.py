"""HomeKit Secure Video: recording clips to the home hub.

The camera advertises what it can record (H.264 in fragmented MP4, AAC
audio) and what starts a recording (motion, doorbell). Once the user turns
recording on in the Home app, the hub picks a configuration. When a trigger
fires (the camera's linked Motion Sensor or its Doorbell), the hub opens a
HomeKit Data Stream (see hds.py) and asks for the clip with
`dataSend/open`. The accessory then sends the MP4 initialization segment and
a fragment every few seconds until the hub has enough and closes it.

The hub runs its own analysis on the clips (people, animals, vehicles,
packages). Accessories can't tell it what's in them, only what starts them.

While recording is on in the Home app, ffmpeg runs all the time and the last
few seconds are kept (the prebuffer), so a clip starts before the trigger.
That costs a constant ffmpeg per camera; with the prebuffer turned off, ffmpeg
starts when the hub asks and the clip begins a second or two after the trigger.

Apple doesn't publish this. It follows HAP-NodeJS's reverse-engineered
RecordingManagement. Nothing here depends on Home Assistant.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
import hashlib
import logging
import shlex
import struct
from typing import Any

from pyhap import hap_protocol, tlv as pyhap_tlv
from pyhap.hap_crypto import HAPCrypto
from pyhap.loader import get_loader

from . import hds

_LOGGER = logging.getLogger(__name__)

# -- HAP definitions HAP-python doesn't ship ----------------------------------------

_BASE = "-0000-1000-8000-0026BB765291"
CHARACTERISTICS = {
    "SupportedCameraRecordingConfiguration": ("205", "tlv8", ["pr", "ev"]),
    "SupportedVideoRecordingConfiguration": ("206", "tlv8", ["pr", "ev"]),
    "SupportedAudioRecordingConfiguration": ("207", "tlv8", ["pr", "ev"]),
    "SelectedCameraRecordingConfiguration": ("209", "tlv8", ["pr", "pw", "ev"]),
    "RecordingAudioActive": ("226", "uint8", ["pr", "pw", "ev"]),
    "HomeKitCameraActive": ("21B", "uint8", ["pr", "pw", "ev"]),
    "EventSnapshotsActive": ("223", "uint8", ["pr", "pw", "ev"]),
    "PeriodicSnapshotsActive": ("225", "uint8", ["pr", "pw", "ev"]),
    "SupportedDataStreamTransportConfiguration": ("130", "tlv8", ["pr"]),
    "SetupDataStreamTransport": ("131", "tlv8", ["pr", "pw", "wr"]),
    "CameraOperatingModeIndicator": ("21D", "uint8", ["pr", "pw", "ev"]),
    "ManuallyDisabled": ("227", "uint8", ["pr", "ev"]),
}
# The camera settings the Home app shows, which profiles may bind.
OPERATING_MODE_OPTIONAL = ["NightVision", "CameraOperatingModeIndicator", "ManuallyDisabled"]
SERVICES = {
    "CameraRecordingManagement": ("204", [
        "Active", "SupportedCameraRecordingConfiguration",
        "SupportedVideoRecordingConfiguration", "SupportedAudioRecordingConfiguration",
        "SelectedCameraRecordingConfiguration", "RecordingAudioActive",
    ]),
    "CameraOperatingMode": ("21A", [
        "EventSnapshotsActive", "HomeKitCameraActive", "PeriodicSnapshotsActive",
    ], OPERATING_MODE_OPTIONAL),
    "DataStreamTransportManagement": ("129", [
        "SetupDataStreamTransport", "SupportedDataStreamTransportConfiguration", "Version",
    ]),
}
# Characteristics the bridge itself sets (not the profile).
MANAGED_SERVICES = {"CameraRecordingManagement", "CameraOperatingMode",
                    "DataStreamTransportManagement"}


def register_definitions(loader: Any) -> None:
    """Teach a HAP-python loader the HKSV services and characteristics."""
    for name, (short, fmt, perms) in CHARACTERISTICS.items():
        entry: dict[str, Any] = {"Format": fmt, "Permissions": perms, "UUID": _uuid(short)}
        if fmt == "uint8":
            entry.update(minValue=0, maxValue=1)
        loader.char_types.setdefault(name, entry)
    for name, (short, chars, *optional) in SERVICES.items():
        loader.serv_types.setdefault(
            name,
            {"UUID": _uuid(short), "RequiredCharacteristics": chars,
             "OptionalCharacteristics": optional[0] if optional else []},
        )


def _uuid(short: str) -> str:
    return f"{short.rjust(8, '0')}{_BASE}"


# Profiles, validation and previews use the shared catalogue.
register_definitions(get_loader())


# -- session keys --------------------------------------------------------------------


class _KeyedHAPCrypto(HAPCrypto):
    """HAPCrypto that remembers the session's shared secret, which HDS keys
    are derived from. HAP-python otherwise throws it away after pair-verify."""

    def reset(self, shared_key: bytes) -> None:
        self.shared_key = shared_key
        super().reset(shared_key)


def capture_session_keys() -> None:
    """Keep each HAP connection's shared secret (idempotent, process-wide)."""
    if not issubclass(hap_protocol.HAPCrypto, _KeyedHAPCrypto):
        hap_protocol.HAPCrypto = _KeyedHAPCrypto


def session_secret(driver: Any, client_addr: Any) -> bytes | None:
    """The shared secret of the HAP connection `client_addr` is on."""
    server = getattr(driver, "http_server", None)
    connection = getattr(server, "connections", {}).get(client_addr) if server else None
    crypto = getattr(connection, "hap_crypto", None)
    return getattr(crypto, "shared_key", None)


# -- TLVs ------------------------------------------------------------------------------


def _tlv(*pairs: Any) -> bytes:
    """TLV8 as HAP-NodeJS writes it: ints are one byte, lists repeat the tag
    with an empty 0x00 item between entries."""
    out = bytearray()
    for tag, value in zip(pairs[::2], pairs[1::2], strict=True):
        if isinstance(value, list):
            for index, item in enumerate(value):
                if index:
                    out += b"\x00\x00"
                out += _tlv(tag, item)
            if not value:
                out += bytes([tag, 0])
            continue
        if isinstance(value, int):
            value = bytes([value])
        out += pyhap_tlv.encode(bytes([tag]), bytes(value))
    return bytes(out)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


TRIGGER_MOTION = 0x01
TRIGGER_DOORBELL = 0x02
PREBUFFER_MS = 4000
FRAGMENT_MS = 4000
H264_PROFILES = ["baseline", "main", "high"]
H264_LEVELS = ["3.1", "3.2", "4.0"]
RESOLUTIONS = [(1920, 1080, 30), (1280, 720, 30), (640, 360, 30)]
AAC_LC = 0
SAMPLE_RATES = [8000, 16000, 24000, 32000, 44100, 48000]  # indexed as HAP sends them
OFFERED_SAMPLE_RATES = [3, 4, 5]  # 32, 44.1 and 48 kHz


def supported_camera_configuration(triggers: int) -> str:
    return _b64(_tlv(
        1, struct.pack("<i", PREBUFFER_MS),
        2, struct.pack("<q", triggers),
        3, _tlv(1, 0, 2, _tlv(1, struct.pack("<i", FRAGMENT_MS))),  # fragmented MP4
    ))


def supported_video_configuration() -> str:
    params = _tlv(1, list(range(len(H264_PROFILES))), 2, list(range(len(H264_LEVELS))))
    attributes = [
        _tlv(1, struct.pack("<H", w), 2, struct.pack("<H", h), 3, fps)
        for w, h, fps in RESOLUTIONS
    ]
    return _b64(_tlv(1, _tlv(1, 0, 2, params, 3, attributes)))  # H.264


def supported_audio_configuration() -> str:
    params = _tlv(1, 1, 2, 0, 3, OFFERED_SAMPLE_RATES)  # mono, variable bitrate
    return _b64(_tlv(1, _tlv(1, AAC_LC, 2, params)))


def supported_transport_configuration() -> str:
    return _b64(_tlv(1, _tlv(1, 0)))  # HomeKit Data Stream over TCP


@dataclass(frozen=True)
class RecordingConfig:
    """What the hub chose."""

    prebuffer_ms: int
    triggers: int
    fragment_ms: int
    profile: str
    level: str
    bitrate: int  # kbit/s
    iframe_ms: int
    width: int
    height: int
    fps: int
    audio_channels: int
    sample_rate: int  # Hz
    audio_bitrate: int  # kbit/s


def parse_selected(value: str) -> RecordingConfig:
    top = pyhap_tlv.decode(base64.b64decode(value))
    recording = pyhap_tlv.decode(top[b"\x01"])
    video = pyhap_tlv.decode(top[b"\x02"])
    audio = pyhap_tlv.decode(top[b"\x03"])
    container = pyhap_tlv.decode(recording[b"\x03"])
    container_params = pyhap_tlv.decode(container[b"\x02"])
    video_params = pyhap_tlv.decode(video[b"\x02"])
    attributes = pyhap_tlv.decode(video[b"\x03"])
    audio_params = pyhap_tlv.decode(audio[b"\x02"])

    def i32(data: bytes) -> int:
        return int.from_bytes(data[:4], "little", signed=True)

    profile, level = video_params[b"\x01"][0], video_params[b"\x02"][0]
    rate = audio_params[b"\x03"][0]
    return RecordingConfig(
        prebuffer_ms=i32(recording[b"\x01"]),
        triggers=int.from_bytes(recording[b"\x02"], "little"),
        fragment_ms=i32(container_params[b"\x01"]),
        profile=H264_PROFILES[min(profile, len(H264_PROFILES) - 1)],
        level=H264_LEVELS[min(level, len(H264_LEVELS) - 1)],
        bitrate=i32(video_params[b"\x03"]),
        iframe_ms=i32(video_params[b"\x04"]),
        width=int.from_bytes(attributes[b"\x01"], "little"),
        height=int.from_bytes(attributes[b"\x02"], "little"),
        fps=attributes[b"\x03"][0],
        audio_channels=audio_params[b"\x01"][0],
        sample_rate=SAMPLE_RATES[min(rate, len(SAMPLE_RATES) - 1)],
        audio_bitrate=int.from_bytes(audio_params[b"\x04"][:4], "little"),
    )


# -- recording -----------------------------------------------------------------------


def ffmpeg_command(
    binary: str, source: str, config: RecordingConfig, *, audio: bool
) -> list[str]:
    """ffmpeg turning the camera's stream into fragmented MP4 on stdout."""
    if "-i " not in source:
        source = f"-i {source}"
    seconds = max(config.fragment_ms, 1000) / 1000
    w, h = config.width, config.height
    command = [
        binary, "-hide_banner", "-loglevel", "error", *shlex.split(source),
        "-map", "0:v:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", config.profile, "-level:v", config.level, "-pix_fmt", "yuv420p",
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(config.fps),
        "-b:v", f"{config.bitrate}k", "-maxrate", f"{config.bitrate}k",
        "-bufsize", f"{config.bitrate * 2}k",
        # Each fragment must start with a key frame and be no longer than the
        # fragment length the hub chose.
        "-g", str(int(config.fps * seconds)),
        "-force_key_frames", f"expr:gte(t,n_forced*{seconds:g})", "-sc_threshold", "0",
    ]
    if audio:
        command += [
            "-map", "0:a:0", "-c:a", "aac", "-profile:a", "aac_low",
            "-ac", str(max(config.audio_channels, 1)), "-ar", str(config.sample_rate),
            "-b:a", f"{max(config.audio_bitrate, 16)}k",
        ]
    else:
        command += ["-an"]
    return command + [
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
    ]


async def read_boxes(reader: asyncio.StreamReader) -> AsyncIterator[tuple[bytes, bytes]]:
    """(type, whole box) for each top-level MP4 box."""
    while True:
        try:
            header = await reader.readexactly(8)
        except asyncio.IncompleteReadError:
            return
        size, kind = struct.unpack(">I4s", header)
        if size == 1:
            extended = await reader.readexactly(8)
            size = struct.unpack(">Q", extended)[0]
            header += extended
        if size < len(header):
            raise ValueError(f"bad MP4 box size {size}")
        yield kind, header + await reader.readexactly(size - len(header))


async def mp4_segments(reader: asyncio.StreamReader) -> AsyncIterator[tuple[bool, bytes]]:
    """(is initialization, data): first ftyp+moov, then each moof+mdat."""
    pending = bytearray()
    init_done = False
    async for kind, box in read_boxes(reader):
        pending += box
        if not init_done and kind == b"moov":
            init_done = True
            yield True, bytes(pending)
            pending.clear()
        elif init_done and kind == b"mdat":
            yield False, bytes(pending)
            pending.clear()


# -- prebuffer -------------------------------------------------------------------------

SegmentSource = Callable[[], AsyncIterator[tuple[bool, bytes]]]
RESTART_DELAYS = (5.0, 15.0, 30.0, 60.0)


class Prebuffer:
    """Keeps one ffmpeg running and the last few fragments, for clips that
    start before their trigger. Clips also follow it live from there."""

    def __init__(self, source: SegmentSource, keep_fragments: int, name: str) -> None:
        self._source = source
        self._keep = max(keep_fragments, 1)
        self._name = name
        self.init: bytes | None = None
        self.fragments: list[bytes] = []
        self._listeners: set[asyncio.Queue[tuple[bool, bytes] | None]] = set()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.get_running_loop().create_task(self._async_run())

    async def async_stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._reset()

    def _reset(self) -> None:
        self.init = None
        self.fragments.clear()
        for queue in self._listeners:
            queue.put_nowait(None)  # the clip can't continue on a new init segment
        self._listeners.clear()

    async def _async_run(self) -> None:
        failures = 0
        while True:
            started = asyncio.get_running_loop().time()
            try:
                async for is_init, data in self._source():
                    if is_init:
                        self._reset()
                        self.init = data
                        continue
                    self.fragments = [*self.fragments, data][-self._keep:]
                    for queue in list(self._listeners):
                        if queue.qsize() > 30:  # that clip stopped reading
                            self._listeners.discard(queue)
                            queue.put_nowait(None)
                        else:
                            queue.put_nowait((False, data))
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - retried below
                _LOGGER.warning("%s: prebuffer ffmpeg failed: %s", self._name, err)
            self._reset()
            # Ran for a while: a hiccup. Died at once: back off.
            failures = 0 if asyncio.get_running_loop().time() - started > 60 else failures + 1
            delay = RESTART_DELAYS[min(failures, len(RESTART_DELAYS) - 1)]
            _LOGGER.debug("%s: prebuffer restarting in %ss", self._name, delay)
            await asyncio.sleep(delay)

    async def clip(self) -> AsyncIterator[tuple[bool, bytes]]:
        """The init segment, the kept fragments, then new ones as they come."""
        if self.init is None:
            return
        queue: asyncio.Queue[tuple[bool, bytes] | None] = asyncio.Queue()
        self._listeners.add(queue)
        try:
            yield True, self.init
            for fragment in list(self.fragments):
                yield False, fragment
            while (item := await queue.get()) is not None:
                yield item
        finally:
            self._listeners.discard(queue)


# -- the services and the stream -----------------------------------------------------

CHUNK = 0x40000  # largest piece of a segment per dataSend/data event
TAIL_SECONDS = 10.0  # keep recording this long after the trigger clears
MIN_SECONDS = 15.0
MAX_SECONDS = 300.0


@dataclass
class Triggers:
    motion: bool
    doorbell: bool

    @property
    def bits(self) -> int:
        return (TRIGGER_MOTION if self.motion else 0) | (TRIGGER_DOORBELL if self.doorbell else 0)


class RecordingManager:
    """The HKSV services on one camera, and the clips it sends."""

    def __init__(
        self,
        acc: Any,
        *,
        name: str,
        triggers: Triggers,
        triggered: Callable[[], bool],
        get_source: Callable[[], Awaitable[str | None]],
        ffmpeg: str,
        audio: bool,
        prebuffer: bool | Callable[[], bool] = True,
        load: Callable[[], dict[str, Any] | None],
        save: Callable[[dict[str, Any]], None],
    ) -> None:
        self.acc = acc
        self.name = name
        self.triggers = triggers
        self._triggered = triggered
        self._get_source = get_source
        self._ffmpeg = ffmpeg
        self.camera_audio = audio
        self._prebuffer_wanted = prebuffer
        self.prebuffer: Prebuffer | None = None
        self._prebuffer_key: tuple[Any, ...] | None = None
        self._save = save
        self.server = hds.DataStreamServer()
        self.server.on_request("dataSend", "open", self._async_open)
        self.server.on_event("dataSend", "close", self._on_close)
        self.server.on_event("dataSend", "ack", self._on_ack)
        self._stream: asyncio.Task[None] | None = None
        self._stream_id: int | None = None
        self.config: RecordingConfig | None = None

        self._supported = (
            supported_camera_configuration(triggers.bits),
            supported_video_configuration(),
            supported_audio_configuration(),
        )
        self.state: dict[str, Any] = {
            "active": 0, "selected": "", "audio": 0,
            "camera_active": 1, "event_snapshots": 1, "periodic_snapshots": 1,
            "streaming": 1,
        }
        saved = load() or {}
        if saved.get("hash") == self._hash():
            self.state.update({k: v for k, v in saved.items() if k in self.state})
        elif saved:
            # What the camera offers changed (e.g. a new trigger), so the hub's
            # earlier choice may not fit; it picks again.
            self.state.update({k: v for k, v in saved.items() if k in self.state and k != "selected"})
        if self.state["selected"]:
            try:
                self.config = parse_selected(self.state["selected"])
            except Exception:  # noqa: BLE001 - start without it; the hub writes it again
                self.state["selected"] = ""
        self._build()

    def _hash(self) -> str:
        return hashlib.sha256("".join(self._supported).encode()).hexdigest()

    def _persist(self) -> None:
        self._save({**self.state, "hash": self._hash()})

    # building

    def _add(self, name: str, unique_id: Any = None) -> Any:
        # A profile may have built it already (e.g. CameraOperatingMode with
        # night vision); the bridge then fills in what it manages.
        if (existing := self.acc.get_service(name)) is not None:
            return existing
        service = self.acc.driver.loader.get_service(name)
        if unique_id is not None:
            service.unique_id = unique_id
        self.acc.add_service(service)
        return service

    def _ensure_char(self, service: Any, name: str) -> Any:
        try:
            return service.get_characteristic(name)
        except ValueError:
            char = self.acc.driver.loader.get_char(name)
            service.add_characteristic(char)
            char.broker = self.acc
            self.acc.iid_manager.assign(char)
            return char

    def _setting(self, key: str, on_change: Callable[[int], None] | None = None) -> Callable[[Any], None]:
        def _set(value: Any) -> None:
            self.state[key] = int(value)
            self._persist()
            if on_change is not None:
                on_change(int(value))

        return _set

    def _build(self) -> None:
        supported_camera, supported_video, supported_audio = self._supported
        rec = self._add("CameraRecordingManagement")
        rec.configure_char("Active", value=self.state["active"],
                           setter_callback=self._setting("active", self._on_active))
        rec.configure_char("SupportedCameraRecordingConfiguration", value=supported_camera)
        rec.configure_char("SupportedVideoRecordingConfiguration", value=supported_video)
        rec.configure_char("SupportedAudioRecordingConfiguration", value=supported_audio)
        rec.configure_char("SelectedCameraRecordingConfiguration", value=self.state["selected"],
                           setter_callback=self._on_selected)
        rec.configure_char("RecordingAudioActive", value=self.state["audio"],
                           setter_callback=self._setting("audio", self._on_audio))

        mode = self._add("CameraOperatingMode")
        mode.configure_char("HomeKitCameraActive", value=self.state["camera_active"],
                            setter_callback=self._setting("camera_active", self._on_camera_active))
        mode.configure_char("EventSnapshotsActive", value=self.state["event_snapshots"],
                            setter_callback=self._setting("event_snapshots"))
        mode.configure_char("PeriodicSnapshotsActive", value=self.state["periodic_snapshots"],
                            setter_callback=self._setting("periodic_snapshots"))

        transport = self._add("DataStreamTransportManagement")
        transport.configure_char("SupportedDataStreamTransportConfiguration",
                                 value=supported_transport_configuration())
        transport.configure_char("Version", value=hds.VERSION)
        self._setup_char = transport.configure_char(
            "SetupDataStreamTransport", value="", setter_callback=self._on_setup_transport)
        rec.add_linked_service(transport)

        # The Motion Sensor the hub watches for motion triggers.
        self.motion_service = self.acc.get_service("MotionSensor")
        if self.motion_service is not None:
            rec.add_linked_service(self.motion_service)
            status = self._ensure_char(self.motion_service, "StatusActive")
            status.set_value(bool(self.state["camera_active"]), should_notify=False)
            self._motion_status = status
        else:
            self._motion_status = None

        # Streaming on/off from the Home app ("Stream" vs "Off").
        for service in self.acc.services:
            if service.display_name == "CameraRTPStreamManagement":
                active = self._ensure_char(service, "Active")
                active.set_value(self.state["streaming"], should_notify=False)
                active.setter_callback = self._setting("streaming")
        self.recording_service = rec
        self.mode_service = mode
        self.transport_service = transport

    @property
    def streaming_allowed(self) -> bool:
        return bool(self.state["camera_active"]) and bool(self.state["streaming"])

    def snapshot_allowed(self, reason: int | None) -> bool:
        """Whether the Home app's settings allow a snapshot. HAP says why it
        wants one: 0 for the tile's periodic refresh, 1 for a notification."""
        if not self.state["camera_active"]:
            return False
        if reason == 0:
            return bool(self.state["periodic_snapshots"])
        if reason == 1:
            return bool(self.state["event_snapshots"])
        return True

    # characteristic writes

    def _on_active(self, active: int) -> None:
        if not active:
            self._cancel_stream()
        self._update_prebuffer()

    def _on_audio(self, _active: int) -> None:
        self._update_prebuffer()

    def _on_camera_active(self, active: int) -> None:
        if self._motion_status is not None:
            self._motion_status.set_value(bool(active))
        if not active:
            self._cancel_stream()
        self._update_prebuffer()

    def _on_selected(self, value: str) -> None:
        try:
            self.config = parse_selected(value)
        except Exception:  # noqa: BLE001 - reject; the hub sees a failed write
            _LOGGER.warning("%s: couldn't read the hub's recording configuration", self.name)
            raise
        self.state["selected"] = value
        self._persist()
        self._update_prebuffer()
        _LOGGER.debug("%s: recording configuration %s", self.name, self.config)

    def _on_setup_transport(self, value: str) -> str:
        """Start an HDS session for the controller writing this; the write
        response carries the port and the accessory's key salt."""
        request = pyhap_tlv.decode(base64.b64decode(value))
        command = request.get(b"\x01", b"\x00")[0]
        transport = request.get(b"\x02", b"\x00")[0]
        salt = request.get(b"\x03", b"")
        client = getattr(self.acc.driver, "writing_client", None)
        secret = session_secret(self.acc.driver, client)
        if command != 0 or transport != 0 or len(salt) != 32 or secret is None:
            _LOGGER.debug("%s: can't set up a data stream (secret known: %s)", self.name,
                          secret is not None)
            return _b64(_tlv(1, 1))  # generic error
        if self.server.port is None:
            return _b64(_tlv(1, 2))  # busy: not listening
        accessory_salt = self.server.prepare(secret, salt, client)
        response = _tlv(1, 0, 2, _tlv(1, struct.pack("<H", self.server.port)))
        self._setup_char.set_value(_b64(response), should_notify=False)
        return _b64(response + _tlv(3, accessory_salt))

    # lifecycle

    async def async_start(self, host: str | None = None) -> None:
        await self.server.async_start(host)
        self._update_prebuffer()

    async def async_stop(self) -> None:
        self._cancel_stream()
        if self.prebuffer is not None:
            await self.prebuffer.async_stop()
            self.prebuffer = None
        await self.server.async_stop()

    # the stream

    def _reject(self, connection: hds.Connection, request_id: int, reason: int) -> None:
        connection.send_response("dataSend", "open", request_id,
                                 hds.STATUS_PROTOCOL_SPECIFIC_ERROR, {"status": reason})

    async def _async_open(self, connection: hds.Connection, request_id: int,
                          message: dict[str, Any]) -> None:
        if message.get("target") != "controller" or message.get("type") != "ipcamera.recording":
            self._reject(connection, request_id, hds.REASON_UNEXPECTED_FAILURE)
            return
        if not self.state["active"] or not self.state["camera_active"]:
            self._reject(connection, request_id, hds.REASON_NOT_ALLOWED)
            return
        if self._stream is not None and not self._stream.done():
            self._reject(connection, request_id, hds.REASON_BUSY)
            return
        if self.config is None:
            self._reject(connection, request_id, hds.REASON_INVALID_CONFIGURATION)
            return
        stream_id = int(message.get("streamId", 0))
        connection.send_response("dataSend", "open", request_id, hds.STATUS_SUCCESS,
                                 {"status": hds.STATUS_SUCCESS})
        self._stream_id = stream_id
        self._stream = asyncio.get_running_loop().create_task(
            self._async_stream(connection, stream_id, self.config))
        connection.on_close.append(self._cancel_stream)

    def _on_close(self, _connection: hds.Connection, message: dict[str, Any]) -> None:
        if message.get("streamId") == self._stream_id:
            _LOGGER.debug("%s: hub closed recording %s (reason %s)", self.name,
                          self._stream_id, message.get("reason"))
            self._cancel_stream()

    def _on_ack(self, _connection: hds.Connection, message: dict[str, Any]) -> None:
        _LOGGER.debug("%s: hub has the whole recording %s", self.name, message.get("streamId"))

    def _cancel_stream(self) -> None:
        if self._stream is not None and not self._stream.done():
            self._stream.cancel()

    def _recording_audio(self) -> bool:
        return self.camera_audio and bool(self.state["audio"])

    @property
    def prebuffer_wanted(self) -> bool:
        wanted = self._prebuffer_wanted
        return bool(wanted() if callable(wanted) else wanted)

    def _update_prebuffer(self) -> None:
        """Run the prebuffer while recording could be asked for, with the
        configuration the hub chose; stop it otherwise."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not started yet; async_start calls this again
        wanted = (self.prebuffer_wanted and self.server.port is not None
                  and bool(self.state["active"]) and bool(self.state["camera_active"])
                  and self.config is not None)
        key = (self.config, self._recording_audio()) if wanted else None
        if key == self._prebuffer_key:
            return
        old, self.prebuffer, self._prebuffer_key = self.prebuffer, None, key
        if old is not None:
            loop.create_task(old.async_stop())
        if key is None:
            return
        config, audio = key
        keep = -(-max(config.prebuffer_ms, PREBUFFER_MS) // max(config.fragment_ms, 1000))
        self.prebuffer = Prebuffer(
            lambda: self._async_ffmpeg_segments(config, audio), keep, self.name)
        self.prebuffer.start()

    async def _async_segments(self, config: RecordingConfig) -> AsyncIterator[tuple[bool, bytes]]:
        prebuffer = self.prebuffer
        if (prebuffer is not None and prebuffer.init is not None
                and self._prebuffer_key == (config, self._recording_audio())):
            async for segment in prebuffer.clip():
                yield segment
            return
        async for segment in self._async_ffmpeg_segments(config, self._recording_audio()):
            yield segment

    async def _async_ffmpeg_segments(
        self, config: RecordingConfig, audio: bool
    ) -> AsyncIterator[tuple[bool, bytes]]:
        source = await self._get_source()
        if not source:
            raise RuntimeError("camera has no stream source")
        command = ffmpeg_command(self._ffmpeg, source, config, audio=audio)
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert process.stdout is not None
            async for segment in mp4_segments(process.stdout):
                yield segment
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _async_stream(self, connection: hds.Connection, stream_id: int,
                            config: RecordingConfig) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        quiet_since: float | None = None
        sequence = 1
        try:
            async for is_init, data in self._async_segments(config):
                now = loop.time()
                if self._triggered():
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = now
                last = not is_init and (
                    now - started >= MAX_SECONDS
                    or (quiet_since is not None and now - quiet_since >= TAIL_SECONDS
                        and now - started >= MIN_SECONDS)
                )
                self._send(connection, stream_id, sequence, is_init, data, last)
                await connection.drain()
                if last:
                    return
                sequence += 1
            # ffmpeg stopped by itself: tell the hub the clip is over.
            connection.send_event("dataSend", "close",
                                  {"streamId": stream_id, "reason": hds.REASON_UNEXPECTED_FAILURE})
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            pass
        except Exception:  # noqa: BLE001 - report it to the hub and the log
            _LOGGER.exception("%s: recording failed", self.name)
            if not connection.closed:
                connection.send_event("dataSend", "close",
                                      {"streamId": stream_id,
                                       "reason": hds.REASON_UNEXPECTED_FAILURE})

    @staticmethod
    def _send(connection: hds.Connection, stream_id: int, sequence: int, is_init: bool,
              data: bytes, last: bool) -> None:
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        for index, chunk in enumerate(chunks, start=1):
            final_chunk = index == len(chunks)
            connection.send_event("dataSend", "data", {
                "streamId": stream_id,
                "packets": [{
                    "data": chunk,
                    "metadata": {
                        "dataType": "mediaInitialization" if is_init else "mediaFragment",
                        "dataSequenceNumber": sequence,
                        "dataChunkSequenceNumber": index,
                        "isLastDataChunk": final_chunk,
                        "dataTotalSize": len(data) if index == 1 else None,
                    },
                }],
                "endOfStream": last if final_chunk else None,
            })
