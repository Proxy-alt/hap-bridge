"""The HAP-python accessory classes the bridge builds from specs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pyhap.accessory import Accessory

if TYPE_CHECKING:
    from .api import AccessorySpec

_LOGGER = logging.getLogger(__name__)


class SpecAvailability:
    """Reachability that follows the accessory's spec ("No Response" when False)."""

    spec: AccessorySpec

    @property
    def available(self) -> bool:
        try:
            return self.spec.is_available()
        except Exception:  # noqa: BLE001 - a broken check must not break reads
            _LOGGER.exception("%s: availability check failed", self.spec.name)
            return True


class SpecAccessory(SpecAvailability, Accessory):
    """A plain accessory built from a spec."""

    def __init__(self, spec: AccessorySpec, *args: Any, **kwargs: Any) -> None:
        self.spec = spec
        super().__init__(*args, **kwargs)


def diagnose(spec: AccessorySpec, acc: Accessory) -> dict[str, Any]:
    """The accessory as HomeKit sees it: services, IIDs, values, watched entities."""
    iid = acc.iid_manager.get_iid
    services = []
    for svc in acc.services:
        services.append(
            {
                "type": svc.display_name,
                "iid": iid(svc),
                "unique_id": svc.unique_id,
                "primary": bool(svc.is_primary_service),
                "linked": [iid(s) for s in svc.linked_services],
                "characteristics": {
                    # Camera stream configuration is long binary TLV; skip it.
                    c.display_name: {"iid": iid(c), "value": c.value}
                    for c in svc.characteristics
                    if c.properties.get("Format") != "tlv8"
                },
            }
        )
    watched = sorted({eid for s in spec.services for eid in s.watched()})
    return {
        "aid": acc.aid,
        "key": spec.key,
        "name": spec.name,
        "category": acc.category,
        "standalone": spec.standalone,
        "available": acc.available,
        "watches": watched,
        "services": services,
    }
