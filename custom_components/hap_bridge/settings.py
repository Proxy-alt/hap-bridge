"""An accessory's settings and details, for a Home app extension.

HomeKit has nowhere for a device's own settings (LED brightness, power-on
behaviour, motion sensitivity) or details (firmware, signal strength), so
the bridge leaves those entities out. Apple's Home Extensions framework
(iOS 27.2+) lets an app add settings pages to an accessory in the Home app;
these two actions are what such an extension would call:

- `hap_bridge.accessory_settings`: for an accessory (by the serial number the
  extension reads from its `HMAccessory`), the device's configuration
  entities as settings and its diagnostic entities as read-only details.
  Each setting's `kind` matches a Home Extensions setting type: `toggle`
  (HomeAccessoryToggleSetting), `choice` (HomeAccessoryEnumSetting),
  `range` (HomeAccessoryRangeSetting), `text` (HomeAccessoryTextFieldSetting)
  and `info` (HomeAccessoryInformationSetting). `action` (a button) has no
  setting type; an extension would show it as a button in its own view.
- `hap_bridge.set_accessory_setting`: change one of them. Only entities that
  `accessory_settings` lists for that accessory can be changed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .api import AccessorySpec
from .const import DOMAIN

# Domains that are settings, and how to set them.
_SETTERS: dict[str, tuple[str, str | None]] = {
    "switch": ("turn_on", None),  # turn_on / turn_off by value
    "select": ("select_option", "option"),
    "input_select": ("select_option", "option"),
    "number": ("set_value", "value"),
    "input_number": ("set_value", "value"),
    "text": ("set_value", "value"),
    "input_text": ("set_value", "value"),
    "button": ("press", None),
}


def accessory_entities(hass: HomeAssistant, spec: AccessorySpec) -> list[er.RegistryEntry]:
    """The configuration and diagnostic entities of the accessory's device."""
    if spec.device_id is None:
        return []
    return [
        entry for entry in er.async_entries_for_device(er.async_get(hass), spec.device_id)
        if entry.entity_category is not None and entry.disabled_by is None
    ]


def describe(hass: HomeAssistant, spec: AccessorySpec, serial: str) -> dict[str, Any]:
    settings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []
    device = dr.async_get(hass).async_get(spec.device_id) if spec.device_id else None
    if device is not None:
        for label, value in (
            ("Manufacturer", device.manufacturer), ("Model", device.model),
            ("Firmware", device.sw_version), ("Hardware", device.hw_version),
            # The real one: HomeKit gets the bridge's own (HB-...) serial.
            ("Serial number", device.serial_number),
        ):
            if value:
                info.append({"kind": "info", "label": label, "value": str(value)})

    for entry in accessory_entities(hass, spec):
        item = _describe_entity(hass, spec, entry)
        if item is None:
            continue
        (settings if item["kind"] != "info" else info).append(item)

    return {"serial": serial, "name": spec.name, "settings": settings, "info": info}


def _label(hass: HomeAssistant, spec: AccessorySpec, entry: er.RegistryEntry) -> str:
    state = hass.states.get(entry.entity_id)
    name = (state and state.attributes.get("friendly_name")) or entry.name \
        or entry.original_name or entry.entity_id
    name = str(name)
    device = dr.async_get(hass).async_get(spec.device_id) if spec.device_id else None
    prefixes = [spec.name, *([device.name_by_user, device.name] if device else [])]
    for prefix in filter(None, prefixes):
        if name.lower().startswith(prefix.lower()) and len(name) > len(prefix):
            return name[len(prefix):].strip(" -:")
    return name


def _describe_entity(
    hass: HomeAssistant, spec: AccessorySpec, entry: er.RegistryEntry
) -> dict[str, Any] | None:
    state = hass.states.get(entry.entity_id)
    available = state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
    value = state.state if state is not None else None
    attrs = state.attributes if state is not None else {}
    base = {"entity_id": entry.entity_id, "label": _label(hass, spec, entry),
            "available": available}
    domain = entry.domain
    writable = entry.entity_category == er.EntityCategory.CONFIG and domain in _SETTERS

    if writable and domain == "switch":
        return {**base, "kind": "toggle", "value": value == "on"}
    if writable and domain in ("select", "input_select"):
        return {**base, "kind": "choice", "options": list(attrs.get("options") or []),
                "value": value if available else None}
    if writable and domain in ("number", "input_number"):
        try:
            number = float(value) if available else None
        except (TypeError, ValueError):
            number = None
        return {**base, "kind": "range", "min": attrs.get("min"), "max": attrs.get("max"),
                "step": attrs.get("step"), "unit": attrs.get(ATTR_UNIT_OF_MEASUREMENT),
                "value": number}
    if writable and domain in ("text", "input_text"):
        return {**base, "kind": "text", "value": value if available else None,
                "max_length": attrs.get("max"), "pattern": attrs.get("pattern")}
    if writable and domain == "button":
        return {**base, "kind": "action"}
    if domain in ("sensor", "binary_sensor"):
        shown = value
        if available and domain == "binary_sensor":
            shown = "Yes" if value == "on" else "No"
        elif available and (unit := attrs.get(ATTR_UNIT_OF_MEASUREMENT)):
            shown = f"{value} {unit}"
        return {**base, "kind": "info", "value": shown if available else None}
    return None  # e.g. a config light or an update entity: nothing to show


async def async_set(
    hass: HomeAssistant, spec: AccessorySpec, entity_id: str, value: Any
) -> None:
    entry = next((e for e in accessory_entities(hass, spec) if e.entity_id == entity_id), None)
    if (entry is None or entry.entity_category != er.EntityCategory.CONFIG
            or entry.domain not in _SETTERS):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_setting",
            translation_placeholders={"entity_id": entity_id, "name": spec.name},
        )
    service, field = _SETTERS[entry.domain]
    data: dict[str, Any] = {"entity_id": entity_id}
    if entry.domain == "switch":
        service = "turn_on" if _truthy(value) else "turn_off"
    elif field is not None:
        if value is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="setting_needs_value",
                translation_placeholders={"entity_id": entity_id},
            )
        data[field] = value
    await hass.services.async_call(entry.domain, service, data, blocking=True)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)
