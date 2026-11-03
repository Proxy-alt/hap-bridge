"""Load the integration's HA-free modules without running its package __init__.

`hb` aliases custom_components/hap_bridge and `hb.profiles` its profiles
package, both registered as bare packages so `hb.ids`, `hb.api`,
`hb.validate`, `hb.profiles.generic` etc. import without Home Assistant.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "hap_bridge"

for name, path in (("hb", ROOT), ("hb.profiles", ROOT / "profiles")):
    pkg = ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg


class FakeStates:
    def __init__(self) -> None:
        self.data: dict[str, SimpleNamespace] = {}

    def set(self, entity_id: str, state: str, **attributes: Any) -> None:
        self.data[entity_id] = SimpleNamespace(state=state, attributes=attributes)

    def get(self, entity_id: str):
        return self.data.get(entity_id)


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))
        return None


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type, data=None):
        self.events.append((event_type, data or {}))


@pytest.fixture
def hass():
    return SimpleNamespace(states=FakeStates(), services=FakeServices(), bus=FakeBus())


def make_entry(entity_id: str, **kw: Any) -> SimpleNamespace:
    domain = entity_id.split(".")[0]
    defaults = dict(
        id=f"reg-{entity_id}",
        entity_id=entity_id,
        domain=domain,
        platform="acme",
        device_class=None,
        original_device_class=None,
        translation_key=None,
        unit_of_measurement=None,
        supported_features=0,
        capabilities=None,
        entity_category=None,
        hidden_by=None,
        name=None,
        original_name=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def make_device(device_id: str = "dev1") -> SimpleNamespace:
    return SimpleNamespace(
        id=device_id,
        name="Bedroom",
        name_by_user=None,
        manufacturer="Acme",
        model="P1",
        sw_version="1.0",
        serial_number=None,
    )


@pytest.fixture(scope="session")
def loader():
    from pyhap.loader import get_loader

    return get_loader()
