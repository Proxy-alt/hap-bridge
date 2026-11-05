"""Cameras that only stream over WebRTC, through go2rtc."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from typing import Any

from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    DATA_COMPONENT,
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCClientConfiguration,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.hap_bridge import webrtc
from custom_components.hap_bridge.sdp import Sdp, kind, mid

OFFER = "\r\n".join([
    "v=0", "o=- 1 2 IN IP4 0.0.0.0", "s=-", "t=0 0", "a=group:BUNDLE 0 1",
    "m=video 9 UDP/TLS/RTP/SAVPF 96", "c=IN IP4 0.0.0.0", "a=mid:0",
    "a=ice-ufrag:uf", "a=ice-pwd:pw", "a=fingerprint:sha-256 AA", "a=setup:actpass",
    "a=recvonly",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111", "c=IN IP4 0.0.0.0", "a=mid:1",
    "a=ice-ufrag:uf", "a=ice-pwd:pw", "a=fingerprint:sha-256 AA", "a=setup:actpass",
    "a=recvonly",
]) + "\r\n"


class NestLike(Camera):
    """Answers like a cloud camera: SDP first, candidates trickled after."""

    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_name = "Doorbell"

    def __init__(self) -> None:
        super().__init__()
        self._attr_unique_id = "doorbell"
        self.offers: list[str] = []
        self.closed: list[str] = []

    async def async_handle_async_webrtc_offer(self, offer_sdp, session_id, send_message) -> None:
        self.offers.append(offer_sdp)
        sections = Sdp.parse(offer_sdp).sections
        lines = ["v=0", "o=- 3 4 IN IP4 1.2.3.4", "s=-", "t=0 0",
                 "a=group:BUNDLE " + " ".join(mid(s) for s in sections)]
        for s in sections:
            lines += [f"m={kind(s)} 9 UDP/TLS/RTP/SAVPF 96", f"a=mid:{mid(s)}", "a=setup:passive"]
        send_message(WebRTCAnswer("\r\n".join(lines) + "\r\n"))
        send_message(WebRTCCandidate(RTCIceCandidateInit(
            "candidate:1 1 udp 1 1.2.3.4 19305 typ host", sdp_mid=mid(sections[0]))))

    async def async_on_webrtc_candidate(self, session_id: str, candidate: Any) -> None:
        pass

    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        return WebRTCClientConfiguration(data_channel="dataSendChannel")

    def close_webrtc_session(self, session_id: str) -> None:
        self.closed.append(session_id)

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        return b""


class RtspCamera(Camera):
    _attr_name = "Garage"

    def __init__(self) -> None:
        super().__init__()
        self._attr_unique_id = "garage"


class FakeGo2rtc:
    def __init__(self) -> None:
        self.added: list[dict[str, str]] = []
        self.consumers: list[Any] = []
        self.preloads: list[tuple[str, str]] = []  # ("start" or "stop", stream)

    def put(self, url, params):
        if url.endswith("/api/preload"):
            self.preloads.append(("start", params["src"]))
        else:
            self.added.append({"url": url, **params})
        return _Response({})

    def delete(self, url, params):
        assert url.endswith("/api/preload")
        self.preloads.append(("stop", params["src"]))
        return _Response({})

    def get(self, url, params):
        return _Response({"consumers": self.consumers})


class _Response:
    status = 200

    def __init__(self, body) -> None:
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        pass

    async def json(self, content_type=None):
        return self.body


async def _cameras(hass: HomeAssistant) -> tuple[NestLike, RtspCamera]:
    assert await async_setup_component(hass, "camera", {})
    await hass.async_block_till_done()
    nest, rtsp = NestLike(), RtspCamera()
    await hass.data[DATA_COMPONENT].async_add_entities([nest, rtsp])
    await hass.async_block_till_done()
    return nest, rtsp


async def test_native_webrtc_detection(hass: HomeAssistant):
    nest, rtsp = await _cameras(hass)
    assert webrtc.native_webrtc(hass, nest.entity_id)
    assert not webrtc.native_webrtc(hass, rtsp.entity_id)
    assert not webrtc.native_webrtc(hass, "camera.nowhere")


async def test_source_registers_the_camera_with_go2rtc(hass: HomeAssistant):
    nest, _ = await _cameras(hass)
    bridge = webrtc.WebRtcBridge.get(hass)
    bridge._async_local_port = AsyncMock(return_value=40002)  # no sockets in tests
    # Without go2rtc there's no way to convert it.
    assert await bridge.async_source(nest.entity_id) is None

    go2rtc = FakeGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    source = await bridge.async_source(nest.entity_id)
    assert source == "-rtsp_transport tcp -i rtsp://127.0.0.1:18554/hap_bridge_camera_doorbell"
    (added,) = go2rtc.added
    assert added["url"] == "http://localhost:11984/api/streams"
    assert added["name"] == "hap_bridge_camera_doorbell"
    # Home Assistant's own go2rtc calls our plain-HTTP port on 127.0.0.1, not
    # Home Assistant's web server, which may be HTTPS (and invalid for 127.0.0.1).
    assert added["src"].startswith("webrtc:http://127.0.0.1:40002/")
    assert added["src"].endswith(f"/whep/{bridge.token}/camera.doorbell")
    assert "/api/" not in added["src"]

    # Someone else's go2rtc: its RTSP server is on its own host.
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://192.168.1.9:1984/", session=go2rtc)
    assert (await bridge.async_source(nest.entity_id)).endswith(
        "rtsp://192.168.1.9:8554/hap_bridge_camera_doorbell")
    # ...and reaches us through Home Assistant's web server.
    assert f"/api/hap_bridge/whep/{bridge.token}/camera.doorbell" in go2rtc.added[-1]["src"]
    await bridge.async_shutdown()


async def test_local_endpoint_answers_go2rtc(hass: HomeAssistant, socket_enabled):
    import aiohttp

    nest, _ = await _cameras(hass)
    bridge = webrtc.WebRtcBridge.get(hass)
    go2rtc = FakeGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    await bridge.async_source(nest.entity_id)
    url = go2rtc.added[0]["src"].removeprefix("webrtc:")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=OFFER) as response:
                assert response.status == 201
                answer = Sdp.parse(await response.text())
            async with session.post(url.replace(bridge.token, "nope"), data=OFFER) as response:
                assert response.status == 401
    finally:
        await bridge.async_shutdown()
    assert [mid(s) for s in answer.sections] == ["0", "1"]
    assert len(nest.closed) == 1  # shut down: its session closed too


async def test_whep_endpoint_answers_for_the_camera(hass: HomeAssistant, hass_client_no_auth):
    nest, rtsp = await _cameras(hass)
    assert await async_setup_component(hass, "http", {})
    bridge = webrtc.WebRtcBridge.get(hass)
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=FakeGo2rtc())
    client = await hass_client_no_auth()

    response = await client.post(f"/api/hap_bridge/whep/wrong/{nest.entity_id}", data=OFFER)
    assert response.status == 401
    response = await client.post(f"/api/hap_bridge/whep/{bridge.token}/{rtsp.entity_id}",
                                 data=OFFER)
    assert response.status == 404

    response = await client.post(f"/api/hap_bridge/whep/{bridge.token}/{nest.entity_id}",
                                 data=OFFER)
    assert response.status == 201
    assert response.content_type == "application/sdp"
    answer = Sdp.parse(await response.text())
    # The camera saw audio, video and a data channel, like the frontend sends...
    assert [kind(s) for s in Sdp.parse(nest.offers[0]).sections] == [
        "audio", "video", "application"]
    # ...and go2rtc gets an answer to what it asked, with the trickled candidate.
    assert [mid(s) for s in answer.sections] == ["0", "1"]
    assert "a=candidate:1 1 udp 1 1.2.3.4 19305 typ host" in answer.sections[1]

    # go2rtc reconnecting replaces the old session.
    assert nest.closed == []
    response = await client.post(f"/api/hap_bridge/whep/{bridge.token}/{nest.entity_id}",
                                 data=OFFER)
    assert response.status == 201
    assert len(nest.closed) == 1

    # Releasing the camera closes the rest.
    await bridge.async_release(nest.entity_id)
    assert len(nest.closed) == 2


async def test_idle_streams_are_closed(hass: HomeAssistant, hass_client_no_auth):
    nest, _ = await _cameras(hass)
    assert await async_setup_component(hass, "http", {})
    bridge = webrtc.WebRtcBridge.get(hass)
    go2rtc = FakeGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    client = await hass_client_no_auth()
    go2rtc.consumers = [{"id": 1}]
    await client.post(f"/api/hap_bridge/whep/{bridge.token}/{nest.entity_id}", data=OFFER)

    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.util import dt as dt_util

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=webrtc.IDLE_CHECK + 1))
    await hass.async_block_till_done()
    assert nest.closed == []  # still watched

    go2rtc.consumers = []
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2 * webrtc.IDLE_CHECK + 2))
    await hass.async_block_till_done()
    assert len(nest.closed) == 1


async def test_idle_check_waits_for_a_slow_answer(hass: HomeAssistant):
    """go2rtc has no viewers until it has the answer; that isn't idle."""
    import asyncio
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from homeassistant.util import dt as dt_util

    nest, _ = await _cameras(hass)
    bridge = webrtc.WebRtcBridge.get(hass)
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=FakeGo2rtc())
    await bridge.async_offer(nest.entity_id, OFFER)  # starts the idle timer

    release = asyncio.Event()
    answer_now = NestLike.async_handle_async_webrtc_offer

    async def slow(self, offer_sdp, session_id, send_message):
        await release.wait()
        await answer_now(self, offer_sdp, session_id, send_message)

    nest.async_handle_async_webrtc_offer = slow.__get__(nest)
    offer = hass.async_create_task(bridge.async_offer(nest.entity_id, OFFER))
    await asyncio.sleep(0)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=webrtc.IDLE_CHECK + 1))
    await asyncio.sleep(0)
    closed = list(nest.closed)
    release.set()
    answer = await offer
    assert Sdp.parse(answer).sections
    assert len(closed) == 1  # only the first session, replaced by the new one
    assert nest.entity_id in bridge._cameras
    await bridge.async_shutdown()


H264_OFFER = OFFER.replace(
    "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n",
    "m=video 9 UDP/TLS/RTP/SAVPF 96 98\r\n",
).replace(
    "a=recvonly\r\nm=audio",
    "a=rtpmap:96 H264/90000\r\na=fmtp:96 profile-level-id=42e01f\r\n"
    "a=rtpmap:98 H264/90000\r\na=fmtp:98 profile-level-id=640032\r\na=recvonly\r\nm=audio",
)


class PlainWebRtc(NestLike):
    """A WebRTC camera whose settings ask for no data channel."""

    _attr_name = "Porch"

    def __init__(self) -> None:
        super().__init__()
        self._attr_unique_id = "porch"

    def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
        return WebRTCClientConfiguration()


async def test_offer_follows_the_cameras_settings_and_profile(hass: HomeAssistant):
    nest, _ = await _cameras(hass)
    plain = PlainWebRtc()
    await hass.data[DATA_COMPONENT].async_add_entities([plain])
    await hass.async_block_till_done()
    bridge = webrtc.WebRtcBridge.get(hass)
    bridge._async_local_port = AsyncMock(return_value=40002)
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=FakeGo2rtc())

    # The data channel is there only for the camera that asks for one.
    await bridge.async_offer(plain.entity_id, H264_OFFER)
    assert [kind(s) for s in Sdp.parse(plain.offers[0]).sections] == ["audio", "video"]
    await bridge.async_source(nest.entity_id, video_profile="high")
    await bridge.async_offer(nest.entity_id, H264_OFFER)
    audio, video, _data = Sdp.parse(nest.offers[0]).sections
    # One H.264 payload type: the profile the accessory asked for...
    assert video[0].endswith("SAVPF 98")
    # ...else go2rtc's first.
    video = Sdp.parse(plain.offers[0]).sections[1]
    assert video[0].endswith("SAVPF 96")
    await bridge.async_shutdown()


async def test_profiles_can_ask_for_a_video_profile(hass: HomeAssistant):
    from custom_components.hap_bridge import testing

    async def profile(ctx):
        ctx.blocks.camera("camera.door", video_profile="high")

    result = await testing.async_describe(profile, [testing.entry("camera.door")])
    assert result.accessory().camera_video_profile == "high"

    async def bad(ctx):
        ctx.blocks.camera("camera.door", video_profile="ultra")

    result = await testing.async_describe(bad, [testing.entry("camera.door")])
    assert any("camera_video_profile 'ultra'" in w for w in sum(result.warnings().values(), []))


class FrameGo2rtc(FakeGo2rtc):
    """Knows which streams it has, and takes snapshots of them."""

    def __init__(self, frame: bytes | None = b"jpeg") -> None:
        super().__init__()
        self.frame = frame
        self.frames: list[dict[str, str]] = []

    def get(self, url, params):
        if url.endswith("/api/frame.jpeg"):
            self.frames.append(params)
            if self.frame is None:
                raise OSError("no keyframe")
            return _FrameResponse(raw=self.frame)
        known = any(a["name"] == params["src"] for a in self.added)
        return _FrameResponse({"consumers": self.consumers}, status=200 if known else 404)


class _FrameResponse(_Response):
    def __init__(self, body=None, *, raw: bytes = b"", status: int = 200) -> None:
        super().__init__(body)
        self.raw, self.status = raw, status

    async def read(self) -> bytes:
        return self.raw


class StreamCamera(Camera):
    _attr_name = "Shed"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self) -> None:
        super().__init__()
        self._attr_unique_id = "shed"

    async def stream_source(self) -> str | None:
        return "rtsp://10.0.0.5/live"


async def test_go2rtc_takes_live_snapshots(hass: HomeAssistant):
    nest, _ = await _cameras(hass)
    shed = StreamCamera()
    await hass.data[DATA_COMPONENT].async_add_entities([shed])
    await hass.async_block_till_done()
    bridge = webrtc.WebRtcBridge.get(hass)
    bridge._async_local_port = AsyncMock(return_value=40002)
    assert await bridge.async_frame(nest.entity_id) is None  # no go2rtc

    go2rtc = FrameGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    assert await bridge.async_frame(nest.entity_id, height=720) == b"jpeg"
    (added,) = go2rtc.added
    assert added["src"].startswith("webrtc:")
    name = "hap_bridge_camera_doorbell"
    assert go2rtc.frames == [{"src": name, "cache": "5s", "h": "720"}]
    # A stream go2rtc already has isn't replaced: live video may be reading it.
    assert await bridge.async_frame(nest.entity_id) == b"jpeg"
    assert len(go2rtc.added) == 1 and go2rtc.frames[-1] == {"src": name, "cache": "5s"}

    # A camera with a stream source: go2rtc reads that.
    assert await bridge.async_frame(shed.entity_id) == b"jpeg"
    assert go2rtc.added[-1]["src"] == "rtsp://10.0.0.5/live"

    go2rtc.frame = None  # go2rtc couldn't get a frame
    assert await bridge.async_frame(nest.entity_id) is None
    await bridge.async_shutdown()


async def test_stream_stays_open_while_snapshots_keep_coming(hass: HomeAssistant, freezer):
    from datetime import timedelta

    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    nest, _ = await _cameras(hass)
    bridge = webrtc.WebRtcBridge.get(hass)
    bridge._async_local_port = AsyncMock(return_value=40002)
    go2rtc = FrameGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    name = "hap_bridge_camera_doorbell"

    async def wait(seconds):
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    await bridge.async_frame(nest.entity_id)
    assert go2rtc.preloads == [("start", name)]
    # Another snapshot 20 seconds later (still being watched): kept open.
    await wait(20)
    await bridge.async_frame(nest.entity_id)
    await wait(20)
    assert go2rtc.preloads == [("start", name)]
    # Nothing asked for 30 seconds after the last one: let go.
    await wait(11)
    assert go2rtc.preloads == [("start", name), ("stop", name)]

    # Watching again starts it again; shutting down stops it.
    await bridge.async_frame(nest.entity_id)
    await bridge.async_shutdown()
    assert go2rtc.preloads[-2:] == [("start", name), ("stop", name)]


async def test_live_video_doesnt_replace_a_stream_go2rtc_has(hass: HomeAssistant):
    nest, _ = await _cameras(hass)
    bridge = webrtc.WebRtcBridge.get(hass)
    bridge._async_local_port = AsyncMock(return_value=40002)
    go2rtc = FrameGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    await bridge.async_frame(nest.entity_id)  # snapshots added it
    assert await bridge.async_source(nest.entity_id)
    assert len(go2rtc.added) == 1
    # go2rtc lost it (restarted): added again.
    go2rtc.added = []
    assert await bridge.async_source(nest.entity_id)
    assert len(go2rtc.added) == 1
    await bridge.async_shutdown()


def test_logged_errors_leave_out_request_headers():
    from aiohttp import ClientResponseError, RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy
    from yarl import URL

    url = URL("http://localhost:11984/api/frame.jpeg")
    headers = CIMultiDictProxy(CIMultiDict({"Authorization": "Basic c2VjcmV0"}))
    err = ClientResponseError(RequestInfo(url, "GET", headers, url), (), status=500,
                              message="Internal Server Error")
    described = webrtc._describe(err)
    assert "500" in described and "Basic" not in described
    assert webrtc._describe(TimeoutError()) == "TimeoutError"


async def test_frame_doesnt_wait_for_go2rtc_to_connect(hass: HomeAssistant):
    """Starting a preload, or anything else go2rtc is slow to answer, waits
    for go2rtc to reach the camera. A camera that can't connect mustn't hold
    up the snapshot past FRAME_TIMEOUT."""
    import asyncio
    from unittest.mock import patch

    nest, _ = await _cameras(hass)
    bridge = webrtc.WebRtcBridge.get(hass)
    bridge._async_local_port = AsyncMock(return_value=40002)
    stuck = asyncio.Event()

    class SlowPreload(FrameGo2rtc):
        def put(self, url, params):
            if not url.endswith("/api/preload"):
                return super().put(url, params)
            self.preloads.append(("start", params["src"]))
            return _Hang(stuck)

    go2rtc = SlowPreload()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    # The preload hangs, but the frame doesn't wait for it.
    assert await asyncio.wait_for(bridge.async_frame(nest.entity_id), 1) == b"jpeg"
    assert go2rtc.preloads == [("start", "hap_bridge_camera_doorbell")]

    # go2rtc itself hanging: given up on after FRAME_TIMEOUT.
    go2rtc.get = lambda url, params: _Hang(stuck)
    with patch.object(webrtc, "FRAME_TIMEOUT", 0.05):
        assert await asyncio.wait_for(bridge.async_frame(nest.entity_id), 1) is None
    stuck.set()
    await bridge.async_shutdown()


class _Hang:
    """A response that doesn't come until `until` is set."""

    def __init__(self, until) -> None:
        self.until = until

    async def __aenter__(self):
        await self.until.wait()
        return _FrameResponse({})

    async def __aexit__(self, *exc):
        return False
