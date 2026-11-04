"""Video from cameras that only stream over WebRTC (Nest, Ring and others).

Those cameras have no stream URL for ffmpeg to read. Home Assistant's
go2rtc server can receive WebRTC and serve it as RTSP, so this hands each
such camera to go2rtc as a WHEP source pointing back at Home Assistant:

    go2rtc --(WHEP offer)--> /whep/<token>/<camera>
           <-- answer ------  camera.async_handle_async_webrtc_offer(...)
    ffmpeg <-- rtsp://127.0.0.1:18554/hap_bridge_<camera> -- go2rtc

For Home Assistant's own go2rtc, the endpoint is a small plain-HTTP server
of ours on 127.0.0.1: Home Assistant's own web server may be HTTPS with a
certificate that isn't valid for 127.0.0.1, which go2rtc would reject. A
go2rtc elsewhere uses the same endpoint on Home Assistant's web server, at
its internal URL. Neither has a login; a random token in the URL, made
when Home Assistant starts, is what keeps others out.

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
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
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
# How long go2rtc gets to take a snapshot (open the stream, wait for a keyframe).
FRAME_TIMEOUT = 10.0
# A snapshot go2rtc took this recently is reused (several controllers poll).
FRAME_CACHE = "5s"
# While snapshots keep coming (the Home app or the home hub is showing the
# camera), go2rtc keeps the stream open ("preload") so each one doesn't
# reconnect; this long after the last one, it lets go.
WARM_SECONDS = 30.0
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
    pending: int = 0  # offers waiting for the camera's answer


class WebRtcBridge:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.token = secrets.token_urlsafe(32)
        self._cameras: dict[str, _Camera] = {}
        self._warned = False
        self._runner: web.AppRunner | None = None
        self._local_port: int | None = None
        self._local_lock = asyncio.Lock()
        self._video_profiles: dict[str, str | None] = {}
        # The source we last gave go2rtc for each camera's stream.
        self._sources: dict[str, str] = {}
        # Cameras go2rtc is keeping open for snapshots: stops the preload.
        self._warm: dict[str, CALLBACK_TYPE] = {}

    @classmethod
    def get(cls, hass: HomeAssistant) -> WebRtcBridge:
        if (bridge := hass.data.get(DATA_BRIDGE)) is None:
            bridge = hass.data[DATA_BRIDGE] = cls(hass)
            hass.http.register_view(WhepView(bridge))
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, bridge._async_on_stop)
        return bridge

    async def _async_on_stop(self, _event: Any) -> None:
        await self.async_shutdown()

    # -- ffmpeg's side --------------------------------------------------------------

    async def async_source(
        self, entity_id: str, *, video_profile: str | None = None
    ) -> str | None:
        """An ffmpeg input for the camera, through go2rtc. `video_profile`:
        see AccessorySpec.camera_video_profile."""
        self._video_profiles[entity_id] = video_profile
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
        try:
            source = f"webrtc:{await self._async_whep_url(config.url, entity_id)}"
        except OSError as err:
            _LOGGER.error("%s: couldn't listen for go2rtc: %s", entity_id, err)
            return None
        # Replacing a stream go2rtc already has would cut off its viewers
        # (another HomeKit stream, a snapshot preload); only add a new one.
        if not (self._sources.get(entity_id) == source
                and await self._async_has_stream(config, entity_id)):
            if not await self._async_put_stream(config, entity_id, source):
                return None
        return f"-rtsp_transport tcp -i {self._rtsp_base(config.url)}/{name}"

    async def _async_put_stream(self, config: Any, entity_id: str, source: str) -> bool:
        try:
            async with config.session.put(
                f"{config.url.rstrip('/')}/api/streams",
                params={"name": stream_name(entity_id), "src": source},
            ) as response:
                response.raise_for_status()
        except Exception as err:  # noqa: BLE001 - reported, then no stream
            _LOGGER.error("%s: couldn't add the camera to go2rtc: %s", entity_id, err)
            return False
        self._sources[entity_id] = source
        return True

    async def async_frame(
        self, entity_id: str, *, height: int | None = None, video_profile: str | None = None
    ) -> bytes | None:
        """A JPEG frame of the camera's live stream, taken by go2rtc; None if
        there's no go2rtc or it couldn't get one.

        The camera's go2rtc stream is added if go2rtc doesn't have it yet
        (a stream it has isn't replaced: live video may be reading it). A
        WebRTC-only camera's stream comes through us and is closed once go2rtc
        has no viewers; others use the camera's stream source.
        """
        config = self.hass.data.get(GO2RTC)
        if config is None:
            return None
        if not await self._async_has_stream(config, entity_id):
            if native_webrtc(self.hass, entity_id):
                if await self.async_source(entity_id, video_profile=video_profile) is None:
                    return None
            else:
                try:
                    source = await ha_camera.async_get_stream_source(self.hass, entity_id)
                except Exception:  # noqa: BLE001 - no frame; the caller falls back
                    _LOGGER.debug("%s: no stream source", entity_id, exc_info=True)
                    return None
                if not source or not await self._async_put_stream(config, entity_id, source):
                    return None
        await self._async_keep_warm(config, entity_id)
        params = {"src": stream_name(entity_id), "cache": FRAME_CACHE}
        if height:
            params["h"] = str(height)
        try:
            async with asyncio.timeout(FRAME_TIMEOUT), config.session.get(
                f"{config.url.rstrip('/')}/api/frame.jpeg", params=params,
            ) as response:
                response.raise_for_status()
                image = await response.read()
        except Exception as err:  # noqa: BLE001 - the caller falls back
            _LOGGER.debug("%s: go2rtc couldn't take a snapshot: %r", entity_id, err)
            return None
        return image or None

    async def _async_keep_warm(self, config: Any, entity_id: str) -> None:
        """Keep go2rtc's stream open until WARM_SECONDS after this snapshot."""
        if (cancel := self._warm.pop(entity_id, None)) is not None:
            cancel()
        else:
            await self._async_preload(config, entity_id, start=True)

        async def cool(_now: Any) -> None:
            if self._warm.pop(entity_id, None) is None:
                return
            if (current := self.hass.data.get(GO2RTC)) is not None:
                await self._async_preload(current, entity_id, start=False)

        self._warm[entity_id] = async_call_later(self.hass, WARM_SECONDS, cool)

    async def _async_preload(self, config: Any, entity_id: str, *, start: bool) -> None:
        """Start or stop go2rtc's preload of the stream: a viewer of its own,
        which keeps the stream open without anyone watching."""
        request = config.session.put if start else config.session.delete
        try:
            async with request(
                f"{config.url.rstrip('/')}/api/preload", params={"src": stream_name(entity_id)},
            ) as response:
                response.raise_for_status()
        except Exception as err:  # noqa: BLE001 - snapshots just reconnect each time
            _LOGGER.debug("%s: go2rtc preload %s failed: %r",
                          entity_id, "start" if start else "stop", err)
            return
        _LOGGER.debug("%s: go2rtc %s the stream open for snapshots",
                      entity_id, "keeps" if start else "no longer keeps")

    async def _async_has_stream(self, config: Any, entity_id: str) -> bool:
        try:
            async with config.session.get(
                f"{config.url.rstrip('/')}/api/streams", params={"src": stream_name(entity_id)},
            ) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 - add it again; harmless
            return False

    async def _async_whep_url(self, go2rtc_url: str, entity_id: str) -> str:
        path = f"whep/{self.token}/{quote(entity_id)}"
        if _is_local(go2rtc_url):
            port = await self._async_local_port()
            return f"http://127.0.0.1:{port}/{path}"
        try:
            base = get_url(self.hass, allow_external=False, allow_cloud=False,
                           prefer_external=False)
        except NoURLAvailableError:
            api = self.hass.config.api
            scheme = "https" if api and api.use_ssl else "http"
            base = f"{scheme}://127.0.0.1:{api.port if api else 8123}"
        return f"{base.rstrip('/')}/api/{DOMAIN}/{path}"

    async def _async_local_port(self) -> int:
        async with self._local_lock:
            if self._local_port is None:
                app = web.Application()
                app.router.add_post("/whep/{token}/{entity_id}", self._async_local_post)
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                try:
                    await site.start()
                except OSError:
                    await runner.cleanup()
                    raise
                self._runner = runner
                self._local_port = runner.addresses[0][1]
            return self._local_port

    async def _async_local_post(self, request: web.Request) -> web.Response:
        info = request.match_info
        return await self.async_handle_post(request, info["token"], info["entity_id"])

    async def async_handle_post(
        self, request: web.Request, token: str, entity_id: str
    ) -> web.Response:
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            return web.Response(status=401)
        if not entity_id.startswith("camera.") or not native_webrtc(self.hass, entity_id):
            return web.Response(status=404)
        offer = await request.text()
        try:
            answer = await self.async_offer(entity_id, offer)
        except (HomeAssistantError, TimeoutError) as err:
            _LOGGER.warning("%s: WebRTC for HomeKit failed: %s", entity_id, err)
            return web.Response(status=502, text=str(err))
        return web.Response(status=201, text=answer, content_type="application/sdp")

    @staticmethod
    def _rtsp_base(go2rtc_url: str) -> str:
        if _is_local(go2rtc_url):
            return MANAGED_RTSP  # Home Assistant's own go2rtc
        return f"rtsp://{urlparse(go2rtc_url).hostname}:{OTHER_RTSP_PORT}"

    # -- go2rtc's side --------------------------------------------------------------

    async def async_offer(self, entity_id: str, offer: str) -> str:
        """Get the camera's answer to go2rtc's offer."""
        camera = ha_camera.get_camera_from_entity_id(self.hass, entity_id)
        state = self._cameras.setdefault(entity_id, _Camera())
        # go2rtc only reconnects once its last connection is gone.
        self._close_sessions(camera, state)
        # Until go2rtc has the answer it has no viewers; don't count that as idle.
        if state.unsub_idle is not None:
            state.unsub_idle()
            state.unsub_idle = None
        state.pending += 1
        try:
            return await self._async_answer(camera, state, offer)
        finally:
            state.pending -= 1
            self._watch_idle(entity_id)

    async def _async_answer(self, camera: Any, state: _Camera, offer: str) -> str:
        # What the frontend would do: a data channel if the camera asks for one.
        config = camera.async_get_webrtc_client_configuration()
        camera_offer, rewrite = offer_for_camera(
            offer, data_channel=config.data_channel is not None,
            video_profile=self._video_profiles.get(camera.entity_id),
        )
        _LOGGER.debug("%s: go2rtc's offer:\n%s\noffer sent to the camera:\n%s",
                      camera.entity_id, offer, camera_offer)
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
        result = answer_for_go2rtc(sdp, rewrite, candidates)
        _LOGGER.debug("%s: camera's answer:\n%s\ntrickled candidates: %s\nanswer sent to go2rtc:\n%s",
                      camera.entity_id, sdp, candidates, result)
        return result

    def _close_sessions(self, camera: Any, state: _Camera) -> None:
        for session_id in state.sessions:
            try:
                camera.close_webrtc_session(session_id)
            except Exception:  # noqa: BLE001 - already gone is fine
                _LOGGER.debug("closing WebRTC session %s failed", session_id, exc_info=True)
        state.sessions.clear()

    def _watch_idle(self, entity_id: str) -> None:
        if (state := self._cameras.get(entity_id)) is None:
            return  # released meanwhile
        if state.unsub_idle is not None:
            state.unsub_idle()

        async def check(_now: Any) -> None:
            state.unsub_idle = None
            if self._cameras.get(entity_id) is not state:
                return
            if state.pending or await self._async_watched(entity_id):
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
        config = self.hass.data.get(GO2RTC)
        for entity_id, cancel in list(self._warm.items()):
            cancel()
            if config is not None:
                await self._async_preload(config, entity_id, start=False)
        self._warm.clear()
        for entity_id in list(self._cameras):
            await self.async_release(entity_id)
        if self._runner is not None:
            runner, self._runner, self._local_port = self._runner, None, None
            await runner.cleanup()


def _is_local(url: str) -> bool:
    return urlparse(url).hostname in (None, "localhost", "127.0.0.1", "::1")


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
        return await self.bridge.async_handle_post(request, token, entity_id)
