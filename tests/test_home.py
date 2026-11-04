from hb.api import HapContext
from hb.home import describe_home

from .conftest import make_device, make_entry
from .test_profiles import _purifier_ctx, _run


def _names(hass):
    return lambda eid: hass.states.get(eid).attributes.get("friendly_name", eid)


def test_purifier_is_one_tile_with_details(hass):
    ctx = _purifier_ctx(hass)
    _run(ctx)
    purifier, temp = ctx.accessories
    home = describe_home(purifier, _names(hass))
    (tile,) = home["tiles"]
    assert tile["kind"] == "Air Purifier" and tile["state"] == "On"
    assert [d["kind"] for d in tile["details"]] == ["Filter", "Air Quality"]
    assert "notes" not in home
    assert describe_home(temp, _names(hass))["tiles"][0]["state"] == "21.1°C"


def test_sprinkler_zones_are_details_named_after_entities(hass):
    entries = []
    for n in (1, 2):
        eid = f"valve.yard_zone_{n}"
        hass.states.set(eid, "open" if n == 1 else "closed", friendly_name=f"Front {n}")
        entries.append(make_entry(eid, device_class="water"))
    ctx = HapContext(hass, make_device(), entries)
    _run(ctx)
    (tile,) = describe_home(ctx.accessories[0], _names(hass))["tiles"]
    assert tile["kind"] == "Sprinkler" and tile["state"] == "Running"
    assert [(d["name"], d["state"]) for d in tile["details"]] == [
        ("Front 1", "Running"), ("Front 2", "Idle")]


def test_power_strip_tiles_drop_the_device_name(hass):
    entries = []
    for n in (1, 2):
        eid = f"switch.strip_{n}"
        hass.states.set(eid, "on", friendly_name=f"Bedroom Outlet {n}")
        entries.append(make_entry(eid, device_class="outlet"))
    ctx = HapContext(hass, make_device(), entries)
    ctx.accessory(category="OUTLET")
    acc = ctx.accessories[0]
    for eid in ("switch.strip_1", "switch.strip_2"):
        acc.service("Outlet").char("On", watch=[eid], get=lambda: True)
    home = describe_home(acc, _names(hass))
    assert [t["name"] for t in home["tiles"]] == ["Outlet 1", "Outlet 2"]
    assert "Separate Tiles" in home["notes"][0]
