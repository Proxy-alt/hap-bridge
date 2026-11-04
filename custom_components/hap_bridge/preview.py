"""The `hap_bridge.preview` action: what HomeKit would get for a device.

Runs the same steps as a bridge start for one device or entity, without
touching HomeKit IDs, pairing or running accessories, and returns:
- every accessory: which step built it, its services and current values,
  and validation errors/warnings;
- entities left out, and why;
- problems with integrations' own profiles and with shims.
"""

from __future__ import annotations

from typing import Any

from pyhap.loader import get_loader

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import AccessorySpec, Settings
from .profiles import async_collect_specs
from .shims import async_load_shims
from .validate import validate_spec


async def async_preview(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    device_id: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    # Shim files are read fresh, so a preview shows edits straight away.
    collection = await async_collect_specs(
        hass, entry, Settings(), device_id=device_id, entity_id=entity_id,
        shims=await async_load_shims(hass),
    )
    loader = get_loader()
    return {
        "accessories": [describe(spec, loader) for spec in collection.specs],
        "excluded": dict(sorted(collection.excluded.items())),
        "problems": [
            {("shim" if p.shim else "integration"): p.integration, "problem": p.message}
            for p in collection.problems
        ],
    }


def describe(spec: AccessorySpec, loader: Any) -> dict[str, Any]:
    errors, warnings = validate_spec(spec, loader)
    return {
        "name": spec.name,
        "key": spec.key,
        "source": spec.source,
        "category": spec.category,
        "standalone": spec.standalone,
        **({"camera": spec.camera, "camera_audio": spec.camera_audio,
            "recording": {"motion": list(spec.recording.motion),
                          "doorbell": spec.recording.doorbell} if spec.recording else None}
           if spec.camera else {}),
        **({"default_name": spec.default_name} if spec.default_name != spec.name else {}),
        "available": _safe(spec.is_available),
        "errors": errors,
        "warnings": warnings,
        "services": [
            {
                "type": svc.type_name,
                **({"primary": True} if svc.primary else {}),
                **({"linked": [s.type_name for s in svc.linked]} if svc.linked else {}),
                **({"adaptive_lighting": True} if svc.adaptive_light else {}),
                "characteristics": {
                    name: {
                        "value": _safe(char.get) if char.get else None,
                        "writable": char.set is not None or svc.write is not None,
                        "watches": list(char.watch),
                    }
                    for name, char in svc.chars.items()
                },
            }
            for svc in spec.services
        ],
    }


def _safe(func: Any) -> Any:
    try:
        value = func()
    except Exception as err:  # noqa: BLE001 - show it rather than fail the preview
        return f"error: {type(err).__name__}: {err}"
    # Enums and other non-JSON values come out as plain data.
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)
