"""Camera accessories: live streaming and snapshots in the Home app.

HAP-python's Camera does the HomeKit side (stream negotiation, SRTP keys);
this supplies snapshots from Home Assistant's camera entity and runs ffmpeg
to turn its stream source into the SRTP stream the Home app asked for. The
ffmpeg command and resolutions follow core HomeKit's camera accessory.

Audio from the camera to the Home app is Opus, as a second ffmpeg output.
It's opt-in per camera, because asking ffmpeg for an audio track the source
doesn't have stops the video too. Talking back through the camera isn't
supported: Home Assistant has no way to send audio to a camera.

Cameras with a motion sensor or doorbell also offer HomeKit Secure Video;
see recording.py. Cameras that only stream over WebRTC (Nest, Ring) go
through Home Assistant's go2rtc; see webrtc.py.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import timedelta
import logging
import os
from typing import TYPE_CHECKING, Any

from haffmpeg.core import FFMPEG_STDERR, HAFFmpeg
from pyhap.camera import (
    VIDEO_CODEC_PARAM_LEVEL_TYPES,
    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES,
    Camera as PyhapCamera,
)

from homeassistant.components import camera as ha_camera
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .accessories import SpecAvailability
from .recording import RecordingManager, Triggers
from .webrtc import DATA_BRIDGE, WebRtcBridge, native_webrtc

if TYPE_CHECKING:
    from .api import AccessorySpec, Settings

_LOGGER = logging.getLogger(__name__)

VIDEO_OUTPUT = (
    "-map 0:v:0 -an "
    "-c:v libx264 "
    "-profile:v {v_profile} "
    "-tune zerolatency -pix_fmt yuv420p "
    "-r {fps} "
    "-b:v {v_max_bitrate}k -bufsize {v_bufsize}k -maxrate {v_max_bitrate}k "
    "-payload_type 99 "
    "-ssrc {v_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {v_srtp_key} "
    "srtp://{address}:{v_port}?rtcpport={v_port}&"
    "localrtpport={v_port}&pkt_size=1316"
)
AUDIO_OUTPUT = (
    "-map 0:a:0 -vn "
    "-c:a libopus -application lowdelay "
    "-ac 1 -ar {a_sample_rate}k "
    "-b:a {a_max_bitrate}k -bufsize {a_bufsize}k "
    "-payload_type {a_payload} "
    "-ssrc {a_ssrc} -f rtp "
    "-srtp_out_suite AES_CM_128_HMAC_SHA1_80 -srtp_out_params {a_srtp_key} "
    "srtp://{address}:{a_port}?rtcpport={a_port}&"
    "localrtpport={a_port}&pkt_size=188"
)
# What the Home app uses for Opus when it doesn't say.
DEFAULT_AUDIO_PAYLOAD = 110
# Indexed by the H.264 profile the Home app picks (baseline, main, high).
VIDEO_PROFILES = ["baseline", "main", "high"]
MAX_WIDTH, MAX_HEIGHT, MAX_FPS = 1920, 1080, 30
RESOLUTIONS = [
    (320, 180, 15), (320, 240, 15),
    (320, 180, MAX_FPS), (320, 240, MAX_FPS), (480, 270, MAX_FPS), (480, 360, MAX_FPS),
    (640, 360, MAX_FPS), (640, 480, MAX_FPS), (1024, 576, MAX_FPS), (1024, 768, MAX_FPS),
    (1280, 720, MAX_FPS), (1280, 960, MAX_FPS), (1920, 1080, MAX_FPS),
]
STREAM_COUNT = 3
WATCH_INTERVAL = timedelta(seconds=5)
# Lines of ffmpeg's output to show when it stops unexpectedly.
FFMPEG_TAIL = 15


def camera_options(address: str) -> dict[str, Any]:
    return {
        "video": {
            "codec": {
                "profiles": [
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["BASELINE"],
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"],
                    VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["HIGH"],
                ],
                "levels": [
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_1"],
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE3_2"],
                    VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"],
                ],
            },
            "resolutions": [list(r) for r in RESOLUTIONS],
        },
        # Offered on every camera (the Home app requires it); only streamed
        # when the camera has audio turned on.
        "audio": {"codecs": [{"type": "OPUS", "samplerate": 24},
                             {"type": "OPUS", "samplerate": 16}]},
        "address": address,
        "srtp": True,
        "stream_count": STREAM_COUNT,
    }


def ffmpeg_output(stream_config: dict[str, Any], *, audio: bool = False) -> str:
    """The ffmpeg output arguments for a negotiated stream."""
    profile = int.from_bytes(stream_config["v_profile_id"], "big")
    video = VIDEO_OUTPUT.format(
        **stream_config,
        v_profile=VIDEO_PROFILES[min(profile, len(VIDEO_PROFILES) - 1)],
        v_bufsize=stream_config["v_max_bitrate"] * 4,
    )
    # No audio settings means the Home app didn't ask for any (e.g. muted).
    if not audio or "a_port" not in stream_config or "a_sample_rate" not in stream_config:
        return video
    payload = stream_config.get("a_payload_type")
    if isinstance(payload, bytes | bytearray):
        payload = int.from_bytes(payload, "big")
    max_bitrate = stream_config.get("a_max_bitrate") or 24
    return video + " " + AUDIO_OUTPUT.format(
        **{**stream_config, "a_max_bitrate": max_bitrate},
        a_payload=payload or DEFAULT_AUDIO_PAYLOAD,
        a_bufsize=max_bitrate * 4,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class CameraAccessory(SpecAvailability, PyhapCamera):
    """A Home Assistant camera entity as a HomeKit camera."""

    def __init__(
        self, hass: HomeAssistant, spec: AccessorySpec, driver: Any, *args: Any, **kwargs: Any
    ) -> None:
        self.hass = hass
        self.spec = spec
        assert spec.camera is not None
        self.entity_id = spec.camera
        self.audio = spec.camera_audio
        self.recording: RecordingManager | None = None
        super().__init__(camera_options(driver.state.address), driver, *args, **kwargs)

    def setup_recording(self, settings: Settings) -> None:
        """Offer HomeKit Secure Video, triggered by the spec's recording entities."""
        triggers = self.spec.recording
        assert triggers is not None
        key = f"hksv:{self.spec.key}"

        def triggered() -> bool:
            if self.spec.motion_active is not None:
                return self.spec.motion_active()
            return any(
                (state := self.hass.states.get(eid)) is not None and state.state == "on"
                for eid in triggers.motion
            )

        self.recording = RecordingManager(
            self,
            name=self.display_name,
            triggers=Triggers(motion=bool(triggers.motion), doorbell=triggers.doorbell is not None),
            triggered=triggered,
            get_source=self._async_source,
            ffmpeg=get_ffmpeg_manager(self.hass).binary,
            audio=self.audio,
            # Keeping a cloud camera's WebRTC stream open all day would drain
            # batteries and run into the service's limits; record on demand.
            prebuffer=lambda: (self.spec.camera_prebuffer
                               and not native_webrtc(self.hass, self.entity_id)),
            load=lambda: settings.get(key),
            save=lambda state: settings.set(key, state),
        )

    async def async_start_recording(self) -> None:
        if self.recording is None:
            return
        try:
            await self.recording.async_start()
        except OSError as err:
            # Live video still works; only HomeKit Secure Video can't connect.
            _LOGGER.error("%s: can't listen for HomeKit Secure Video: %s", self.display_name, err)

    async def _async_source(self) -> str | None:
        try:
            source = await ha_camera.async_get_stream_source(self.hass, self.entity_id)
        except Exception:  # noqa: BLE001 - reported as a failed recording
            _LOGGER.exception("%s: couldn't get the camera's stream source", self.display_name)
            return None
        if not source and native_webrtc(self.hass, self.entity_id):
            return await WebRtcBridge.get(self.hass).async_source(
                self.entity_id, video_profile=self.spec.camera_video_profile)
        return source

    async def async_get_snapshot(self, image_size: dict[str, int]) -> bytes:
        if self.recording is not None and not self.recording.snapshot_allowed(
            image_size.get("reason")
        ):
            # Turned off in the Home app (camera off, or that kind of snapshot).
            raise PermissionError("snapshots are turned off in the Home app")
        image = await ha_camera.async_get_image(
            self.hass,
            self.entity_id,
            width=image_size["image-width"],
            height=image_size["image-height"],
        )
        return image.content

    async def start_stream(self, session_info: dict[str, Any], stream_config: dict[str, Any]) -> bool:
        if self.recording is not None and not self.recording.streaming_allowed:
            _LOGGER.debug("%s: streaming is turned off in the Home app", self.display_name)
            return False
        source = await self._async_source()
        if not source:
            _LOGGER.error("%s: camera has no stream source to send to HomeKit", self.display_name)
            return False
        if "-i " not in source:
            source = f"-i {source}"

        stream = HAFFmpeg(get_ffmpeg_manager(self.hass).binary)
        opened = await stream.open(
            cmd=[],
            input_source=source,
            output=ffmpeg_output(stream_config, audio=self.audio),
            extra_cmd="-hide_banner -nostats",
            stderr_pipe=True,
            stdout_pipe=False,
        )
        if not opened:
            _LOGGER.error("%s: ffmpeg didn't start", self.display_name)
            return False
        session_info["stream"] = stream
        session_info["pid"] = stream.process.pid
        stderr = await stream.get_reader(source=FFMPEG_STDERR)
        session_info["output"] = output = deque(maxlen=FFMPEG_TAIL)
        session_info["logger"] = self.hass.async_create_background_task(
            self._async_log(stderr, output), f"hap_bridge camera log {self.entity_id}"
        )

        async def _watch(_now: Any) -> None:
            self._async_check(session_info)

        session_info["watcher"] = async_track_time_interval(self.hass, _watch, WATCH_INTERVAL)
        return self._async_check(session_info)

    def _async_check(self, session_info: dict[str, Any]) -> bool:
        """Free the stream slot if ffmpeg has died."""
        if _pid_alive(session_info["pid"]):
            return True
        output = "\n".join(session_info.get("output") or ()) or "(no output)"
        _LOGGER.warning("%s: ffmpeg stopped unexpectedly. Its last output:\n%s",
                        self.display_name, output)
        self._stop_watching(session_info)
        self.set_streaming_available(session_info["stream_idx"])
        return False

    def _stop_watching(self, session_info: dict[str, Any]) -> None:
        unsub: CALLBACK_TYPE | None = session_info.pop("watcher", None)
        if unsub is not None:
            unsub()
        if (task := session_info.pop("logger", None)) is not None:
            task.cancel()

    async def _async_log(self, reader: asyncio.StreamReader, output: deque[str]) -> None:
        while line := await reader.readline():
            text = line.decode(errors="replace").rstrip()
            output.append(text)
            _LOGGER.debug("%s: ffmpeg: %s", self.display_name, text)

    async def stop_stream(self, session_info: dict[str, Any]) -> None:
        if (stream := session_info.get("stream")) is None:
            return
        self._stop_watching(session_info)
        if not _pid_alive(stream.process.pid):
            return
        for method in ("close", "kill"):
            try:
                await getattr(stream, method)()
            except Exception:  # noqa: BLE001 - try the next, harder way
                _LOGGER.debug("%s: ffmpeg %s failed", self.display_name, method, exc_info=True)
            else:
                return

    async def reconfigure_stream(self, session_info: dict[str, Any], stream_config: dict[str, Any]) -> bool:
        # The Home app asks for a lower bitrate on bad networks; keep streaming.
        return True

    async def async_stop_streams(self) -> None:
        await asyncio.gather(*(self.stop_stream(s) for s in list(self.sessions.values())))
        if self.recording is not None:
            await self.recording.async_stop()
        if (bridge := self.hass.data.get(DATA_BRIDGE)) is not None:
            await bridge.async_release(self.entity_id)
