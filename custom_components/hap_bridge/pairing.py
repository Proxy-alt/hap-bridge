"""The pairing notification: setup code plus a QR code the Home app can scan.

Notifications are Markdown and can't embed images inline, so the QR SVG is
served by a small view under a random per-notification token, the same way
the core HomeKit integration does it.
"""

from __future__ import annotations

import io
import secrets

from aiohttp import web
import pyqrcode

from homeassistant.components import persistent_notification
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

QR_URL = f"/api/{DOMAIN}/pairing_qr/{{unit_id}}/{{token}}"

# unit id (the entry ID for the bridge, longer for standalone accessories) -> (token, svg)
_QR_CODES: HassKey[dict[str, tuple[str, bytes]]] = HassKey(f"{DOMAIN}_qr_codes")


@callback
def async_show(hass: HomeAssistant, unit_id: str, name: str, pincode: str, setup_uri: str) -> None:
    codes = _async_codes(hass)
    token = secrets.token_hex(16)
    codes[unit_id] = (token, _qr_svg(setup_uri))
    url = QR_URL.format(unit_id=unit_id, token=token)
    persistent_notification.async_create(
        hass,
        f"Add **{name}** in the Home app: scan the code below, or enter "
        f"`{pincode}`.\n\n![HomeKit setup code]({url})",
        title=f"HAP Bridge pairing: {name}",
        notification_id=notification_id(unit_id),
    )


@callback
def async_dismiss(hass: HomeAssistant, unit_id: str) -> None:
    _async_codes(hass).pop(unit_id, None)
    persistent_notification.async_dismiss(hass, notification_id(unit_id))


def notification_id(unit_id: str) -> str:
    return f"{DOMAIN}_{unit_id}_pairing"


def _qr_svg(setup_uri: str) -> bytes:
    buffer = io.BytesIO()
    pyqrcode.create(setup_uri).svg(buffer, scale=5, module_color="#000", background="#FFF")
    return buffer.getvalue()


@callback
def _async_codes(hass: HomeAssistant) -> dict[str, tuple[str, bytes]]:
    if (codes := hass.data.get(_QR_CODES)) is None:
        codes = hass.data[_QR_CODES] = {}
        hass.http.register_view(PairingQRView())
    return codes


class PairingQRView(HomeAssistantView):
    """Serves a pending pairing QR code to whoever holds its token."""

    url = QR_URL
    name = f"api:{DOMAIN}:pairing_qr"
    # <img> tags can't send auth headers; the unguessable token stands in.
    requires_auth = False

    async def get(self, request: web.Request, unit_id: str, token: str) -> web.Response:
        codes = request.app[KEY_HASS].data.get(_QR_CODES, {})
        pending = codes.get(unit_id)
        if pending is None or not secrets.compare_digest(pending[0], token):
            raise web.HTTPNotFound
        return web.Response(
            body=pending[1], content_type="image/svg+xml", headers={"Cache-Control": "no-store"}
        )
