"""Stable accessory (AID) and instance (IID) allocation.

The Home app keys rooms, scenes and automations on AID/IID pairs, so they
must never change for the same logical accessory, service or characteristic.
`IdRegistry` is pure Python so it can be unit tested without Home Assistant;
`StableIIDManager` plugs it into HAP-python.
"""

from __future__ import annotations

from typing import Any

from pyhap.characteristic import Characteristic
from pyhap.iid_manager import IIDManager
from pyhap.service import Service

from .const import BRIDGE_AID

FIRST_ACCESSORY_AID = BRIDGE_AID + 1


class IdRegistry:
    """Hands out AIDs and IIDs that stay the same for the same keys."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self._aids: dict[str, int] = dict(data.get("aids", {}))
        # aid (as str, for JSON) -> {iid key -> iid}
        self._iids: dict[str, dict[str, int]] = {
            aid: dict(iids) for aid, iids in data.get("iids", {}).items()
        }
        self.dirty = False

    def aid(self, key: str) -> int:
        """Return the AID for an accessory key, allocating one if new."""
        if (aid := self._aids.get(key)) is not None:
            return aid
        used = set(self._aids.values())
        aid = FIRST_ACCESSORY_AID
        while aid in used:
            aid += 1
        self._aids[key] = aid
        self.dirty = True
        return aid

    def iid(self, aid: int, key: str) -> int:
        """Return the IID for a service/characteristic key within an accessory."""
        iids = self._iids.setdefault(str(aid), {})
        if (iid := iids.get(key)) is not None:
            return iid
        iid = max(iids.values(), default=0) + 1
        iids[key] = iid
        self.dirty = True
        return iid

    def as_dict(self) -> dict[str, Any]:
        return {"aids": dict(self._aids), "iids": {a: dict(i) for a, i in self._iids.items()}}


def service_key(type_id: Any, unique_id: str | None) -> str:
    return f"s:{type_id}:{unique_id or ''}"


def char_key(service_type_id: Any, service_unique_id: str | None, type_id: Any) -> str:
    return f"c:{service_type_id}:{service_unique_id or ''}:{type_id}"


class StableIIDManager(IIDManager):
    """Allocates IIDs from the persisted registry instead of a counter."""

    def __init__(self, registry: IdRegistry, aid: int) -> None:
        super().__init__()
        self._registry = registry
        self._aid = aid

    def get_iid_for_obj(self, obj: Service | Characteristic) -> int:
        if isinstance(obj, Service):
            key = service_key(obj.type_id, obj.unique_id)
        else:
            svc = getattr(obj, "service", None)
            key = char_key(
                svc.type_id if svc else None, svc.unique_id if svc else None, obj.type_id
            )
        return self._registry.iid(self._aid, key)
