"""Check accessory specs against HAP-python's service/characteristic catalogue.

A spec the Home app can't render shows up as "Not Supported" or a broken
tile, so this catches mistakes at startup with a readable log line instead.
"""

from __future__ import annotations

from typing import Any

from .api import AccessorySpec


def validate_spec(spec: AccessorySpec, loader: Any) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Accessories with errors are not exposed."""
    errors: list[str] = []
    warnings: list[str] = []

    if not spec.services:
        errors.append("no services")
    if sum(1 for s in spec.services if s.primary) > 1:
        errors.append("more than one primary service")

    for svc in spec.services:
        info = loader.serv_types.get(svc.type_name)
        if info is None:
            errors.append(f"unknown service {svc.type_name!r}")
            continue
        required = info.get("RequiredCharacteristics", [])
        allowed = set(required) | set(info.get("OptionalCharacteristics", []))

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

        for linked in svc.linked:
            if linked not in spec.services:
                errors.append(f"{svc.type_name} links a service from another accessory")

    return errors, warnings
