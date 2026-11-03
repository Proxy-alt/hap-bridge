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


from hb import testing  # noqa: E402 - after the package aliases above


@pytest.fixture
def hass():
    return testing.FakeHass()


def make_entry(entity_id: str, **kw: Any) -> SimpleNamespace:
    kw.setdefault("platform", "acme")
    return testing.entry(entity_id, **kw)


def make_device(device_id: str = "dev1") -> SimpleNamespace:
    return testing.device(device_id, name="Bedroom", model="P1")


@pytest.fixture(scope="session")
def loader():
    return testing.loader()
