"""Cameras that only stream over WebRTC, through go2rtc."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from webrtc_models import RTCIceCandidateInit

from homeassistant.components.camera import (
    DATA_COMPONENT,
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCCandidate,
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

    def put(self, url, params):
        self.added.append({"url": url, **params})
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
    # Without go2rtc there's no way to convert it.
    assert await bridge.async_source(nest.entity_id) is None

    go2rtc = FakeGo2rtc()
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://localhost:11984/", session=go2rtc)
    source = await bridge.async_source(nest.entity_id)
    assert source == "-rtsp_transport tcp -i rtsp://127.0.0.1:18554/hap_bridge_camera_doorbell"
    (added,) = go2rtc.added
    assert added["url"] == "http://localhost:11984/api/streams"
    assert added["name"] == "hap_bridge_camera_doorbell"
    assert added["src"].startswith("webrtc:http")
    assert added["src"].endswith(f"/api/hap_bridge/whep/{bridge.token}/camera.doorbell")

    # Someone else's go2rtc: its RTSP server is on its own host.
    hass.data[webrtc.GO2RTC] = SimpleNamespace(url="http://192.168.1.9:1984/", session=go2rtc)
    assert (await bridge.async_source(nest.entity_id)).endswith(
        "rtsp://192.168.1.9:8554/hap_bridge_camera_doorbell")


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
