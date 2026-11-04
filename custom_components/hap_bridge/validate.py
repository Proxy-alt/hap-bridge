"""Check accessory specs against HAP-python's service/characteristic catalogue.

A spec the Home app can't render shows up as "Not Supported" or a broken
tile, so this catches mistakes at startup with a readable log line instead.
"""

from __future__ import annotations

from typing import Any

from .api import AccessorySpec

# Characteristics HomeKit accepts on a service although HAP-python's bundled
# catalogue (which predates them) doesn't list them there.
EXTRA_CHARACTERISTICS: dict[str, set[str]] = {
    "Lightbulb": {"ColorTemperature"},
}


def validate_spec(spec: AccessorySpec, loader: Any) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Accessories with errors are not exposed."""
    errors: list[str] = []
    warnings: list[str] = []

    if not spec.services and spec.camera is None:  # a camera brings its own
        errors.append("no services")
    if sum(1 for s in spec.services if s.primary) > 1:
        errors.append("more than one primary service")

    explicit: set[tuple[str, str]] = set()
    for svc in spec.services:
        if (uid := svc.explicit_unique_id) is not None:
            if (svc.type_name, uid) in explicit:
                errors.append(f"two {svc.type_name} services with unique_id {uid!r}")
            explicit.add((svc.type_name, uid))
        elif not svc.watched() and any(
            s is not svc and s.type_name == svc.type_name for s in spec.services
        ):
            warnings.append(
                f"{svc.type_name} repeats but watches nothing and has no unique_id; "
                "its HomeKit IDs depend on service order"
            )

    for svc in spec.services:
        info = loader.serv_types.get(svc.type_name)
        if info is None:
            errors.append(f"unknown service {svc.type_name!r}")
            continue
        required = info.get("RequiredCharacteristics", [])
        allowed = (
            set(required)
            | set(info.get("OptionalCharacteristics", []))
            | EXTRA_CHARACTERISTICS.get(svc.type_name, set())
        )

        for name, char in svc.chars.items():
            if name not in allowed:
                errors.append(f"{svc.type_name} has no characteristic {name!r}")
                continue
            perms = loader.char_types[name].get("Permissions", [])
            if char.set is not None and "pw" not in perms:
                errors.append(f"{svc.type_name}.{name} is read-only but has a setter")
            if char.get is not None and not char.watch:
                warnings.append(f"{svc.type_name}.{name} has a getter but watches nothing")

        for name in required:
            if name != "Name" and (name not in svc.chars or svc.chars[name].get is None):
                warnings.append(f"{svc.type_name}.{name} is required but never set")

        if svc.adaptive_wanted and svc.adaptive_light is None:
            errors.append(
                "Adaptive Lighting needs a light: ColorTemperature watches none that "
                "supports color temperature, so pass one to adaptive_lighting()"
            )
        if (
            svc.type_name == "Lightbulb"
            and svc.adaptive_wanted is None
            and svc.adaptive_light is None
            and {"Brightness", "ColorTemperature"} <= set(svc.chars)
            and svc.write is None
        ):
            warnings.append(
                "Lightbulb has ColorTemperature but no on_write, so it can't offer "
                "Adaptive Lighting"
            )
        if svc.adaptive_light is not None and (
            svc.type_name != "Lightbulb"
            or not {"Brightness", "ColorTemperature"} <= set(svc.chars)
            or svc.write is None
        ):
            errors.append(
                "Adaptive Lighting needs a Lightbulb with Brightness, ColorTemperature and on_write"
            )

        for linked in svc.linked:
            if linked not in spec.services:
                errors.append(f"{svc.type_name} links a service from another accessory")

    return errors, warnings
