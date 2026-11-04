"""Video from cameras that only stream over WebRTC (Nest, Ring and others).

Those cameras have no stream URL for ffmpeg to read. Home Assistant's
go2rtc server can receive WebRTC and serve it as RTSP, so this hands each
such camera to go2rtc as a WHEP source pointing back at Home Assistant:

    go2rtc --(WHEP offer)--> /api/hap_bridge/whep/<token>/<camera>
           <-- answer ------  camera.async_handle_async_webrtc_offer(...)
    ffmpeg <-- rtsp://127.0.0.1:18554/hap_bridge_<camera> -- go2rtc

The endpoint has no login; a random token in its URL, made when Home
Assistant starts, is what keeps others out.

go2rtc never says when it's done with a WebRTC connection, and Nest keeps
extending a stream until its session is closed. So the camera's sessions
are closed when go2rtc starts a new one for it, and when go2rtc has had no
one watching it for a while.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import logging
import secrets
from typing import Any
from urllib.parse import quote, urlparse

from aiohttp import web

from homeassistant.components import camera as ha_camera
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util.hass_dict import HassKey
from homeassistant.util.ulid import ulid

from .const import DOMAIN
from .sdp import answer_for_go2rtc, offer_for_camera

_LOGGER = logging.getLogger(__name__)

DATA_BRIDGE: HassKey[WebRtcBridge] = HassKey(f"{DOMAIN}_webrtc")
GO2RTC: HassKey[Any] = HassKey("go2rtc")
MANAGED_RTSP = "rtsp://127.0.0.1:18554"
OTHER_RTSP_PORT = 8554
ANSWER_TIMEOUT = 20.0
# After the answer, how long to wait for more trickled candidates.
CANDIDATE_QUIET = 0.5
CANDIDATE_MAX = 2.0
IDLE_CHECK = 30.0


def native_webrtc(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether the camera streams only over WebRTC, natively."""
    component = hass.data.get(ha_camera.DATA_COMPONENT)
    camera = component.get_entity(entity_id) if component is not None else None
    return camera is not None and (
        type(camera).async_handle_async_webrtc_offer
        is not ha_camera.Camera.async_handle_async_webrtc_offer
    )


def stream_name(entity_id: str) -> str:
    return "hap_bridge_" + entity_id.replace(".", "_")


@dataclass
class _Camera:
    sessions: list[str] = field(default_factory=list)
    unsub_idle: CALLBACK_TYPE | None = None


class WebRtcBridge:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.token = secrets.token_urlsafe(32)
        self._cameras: dict[str, _Camera] = {}
        self._warned = False

    @classmethod
    def get(cls, hass: HomeAssistant) -> WebRtcBridge:
        if (bridge := hass.data.get(DATA_BRIDGE)) is None:
            bridge = hass.data[DATA_BRIDGE] = cls(hass)
            hass.http.register_view(WhepView(bridge))
        return bridge

    # -- ffmpeg's side --------------------------------------------------------------

    async def async_source(self, entity_id: str) -> str | None:
        """An ffmpeg input for the camera, through go2rtc."""
        config = self.hass.data.get(GO2RTC)
        if config is None:
            if not self._warned:
                self._warned = True
                _LOGGER.error(
                    "%s streams only over WebRTC; HomeKit needs Home Assistant's go2rtc "
                    "integration (part of default_config) to convert it", entity_id,
                )
            return None
        name = stream_name(entity_id)
        source = f"webrtc:{self._whep_url(entity_id)}"
        try:
            async with config.session.put(
                f"{config.url.rstrip('/')}/api/streams", params={"name": name, "src": source},
            ) as response:
                response.raise_for_status()
        except Exception as err:  # noqa: BLE001 - reported, then no stream
            _LOGGER.error("%s: couldn't add the camera to go2rtc: %s", entity_id, err)
            return None
        return f"-rtsp_transport tcp -i {self._rtsp_base(config.url)}/{name}"

    def _whep_url(self, entity_id: str) -> str:
        try:
            base = get_url(self.hass, allow_external=False, allow_cloud=False,
                           prefer_external=False)
        except NoURLAvailableError:
            api = self.hass.config.api
            base = f"http://127.0.0.1:{api.port if api else 8123}"
        return f"{base.rstrip('/')}/api/{DOMAIN}/whep/{self.token}/{quote(entity_id)}"

    @staticmethod
    def _rtsp_base(go2rtc_url: str) -> str:
        host = urlparse(go2rtc_url).hostname
        if host in (None, "localhost", "127.0.0.1"):
            return MANAGED_RTSP  # Home Assistant's own go2rtc
        return f"rtsp://{host}:{OTHER_RTSP_PORT}"

    # -- go2rtc's side --------------------------------------------------------------

    async def async_offer(self, entity_id: str, offer: str) -> str:
        """Get the camera's answer to go2rtc's offer."""
        camera = ha_camera.get_camera_from_entity_id(self.hass, entity_id)
        state = self._cameras.setdefault(entity_id, _Camera())
        # go2rtc only reconnects once its last connection is gone.
        self._close_sessions(camera, state)

        camera_offer, rewrite = offer_for_camera(offer)
        session_id = ulid()
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str] = loop.create_future()
        candidates: list[tuple[str, str | None]] = []
        arrived = asyncio.Event()

        @callback
        def send_message(message: Any) -> None:
            if isinstance(message, ha_camera.WebRTCAnswer):
                if not answer.done():
                    answer.set_result(message.answer)
            elif isinstance(message, ha_camera.WebRTCCandidate):
                init = message.candidate
                candidates.append((init.candidate, getattr(init, "sdp_mid", None)))
                arrived.set()
            elif isinstance(message, ha_camera.WebRTCError) and not answer.done():
                answer.set_exception(HomeAssistantError(f"{message.code}: {message.message}"))

        state.sessions.append(session_id)
        try:
            await camera.async_handle_async_webrtc_offer(camera_offer, session_id, send_message)
            sdp = await asyncio.wait_for(answer, ANSWER_TIMEOUT)
        except BaseException:
            self._close_sessions(camera, state)
            raise
        if "a=candidate:" not in sdp or candidates:
            await _settle(arrived, loop.time() + CANDIDATE_MAX)
        self._watch_idle(entity_id)
        return answer_for_go2rtc(sdp, rewrite, candidates)

    def _close_sessions(self, camera: Any, state: _Camera) -> None:
        for session_id in state.sessions:
            try:
                camera.close_webrtc_session(session_id)
            except Exception:  # noqa: BLE001 - already gone is fine
                _LOGGER.debug("closing WebRTC session %s failed", session_id, exc_info=True)
        state.sessions.clear()

    def _watch_idle(self, entity_id: str) -> None:
        state = self._cameras[entity_id]
        if state.unsub_idle is not None:
            state.unsub_idle()

        async def check(_now: Any) -> None:
            state.unsub_idle = None
            if await self._async_watched(entity_id):
                self._watch_idle(entity_id)
                return
            _LOGGER.debug("%s: go2rtc has no viewers; closing the WebRTC stream", entity_id)
            await self.async_release(entity_id)

        state.unsub_idle = async_call_later(self.hass, IDLE_CHECK, check)

    async def _async_watched(self, entity_id: str) -> bool:
        config = self.hass.data.get(GO2RTC)
        if config is None:
            return False
        try:
            async with config.session.get(
                f"{config.url.rstrip('/')}/api/streams", params={"src": stream_name(entity_id)},
            ) as response:
                if response.status == 404:
                    return False
                response.raise_for_status()
                info = await response.json(content_type=None)
        except Exception:  # noqa: BLE001 - assume still in use; try again later
            _LOGGER.debug("%s: couldn't ask go2rtc about the stream", entity_id, exc_info=True)
            return True
        return bool((info or {}).get("consumers"))

    async def async_release(self, entity_id: str) -> None:
        if (state := self._cameras.pop(entity_id, None)) is None:
            return
        if state.unsub_idle is not None:
            state.unsub_idle()
        try:
            camera = ha_camera.get_camera_from_entity_id(self.hass, entity_id)
        except HomeAssistantError:
            return
        self._close_sessions(camera, state)

    async def async_shutdown(self) -> None:
        for entity_id in list(self._cameras):
            await self.async_release(entity_id)


async def _settle(arrived: asyncio.Event, deadline: float) -> None:
    """Wait until candidates stop arriving, or the deadline."""
    loop = asyncio.get_running_loop()
    while (left := deadline - loop.time()) > 0:
        arrived.clear()
        try:
            await asyncio.wait_for(arrived.wait(), min(CANDIDATE_QUIET, left))
        except TimeoutError:
            return


class WhepView(HomeAssistantView):
    """Where go2rtc sends its WebRTC offer for a camera."""

    url = f"/api/{DOMAIN}/whep/{{token}}/{{entity_id}}"
    name = f"api:{DOMAIN}:whep"
    requires_auth = False

    def __init__(self, bridge: WebRtcBridge) -> None:
        self.bridge = bridge

    async def post(self, request: web.Request, token: str, entity_id: str) -> web.Response:
        if not hmac.compare_digest(token.encode(), self.bridge.token.encode()):
            return web.Response(status=401)
        if not entity_id.startswith("camera.") or not native_webrtc(self.bridge.hass, entity_id):
            return web.Response(status=404)
        offer = await request.text()
        try:
            answer = await self.bridge.async_offer(entity_id, offer)
        except (HomeAssistantError, TimeoutError) as err:
            _LOGGER.warning("%s: WebRTC for HomeKit failed: %s", entity_id, err)
            return web.Response(status=502, text=str(err))
        return web.Response(status=201, text=answer, content_type="application/sdp")
