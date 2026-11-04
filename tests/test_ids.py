from types import SimpleNamespace

from pyhap.accessory import Accessory
from pyhap.loader import get_loader

from hb.ids import FIRST_ACCESSORY_AID, IdRegistry, StableIIDManager


def test_aids_are_stable_and_distinct():
    reg = IdRegistry()
    a, b = reg.aid("device:a"), reg.aid("device:b")
    assert a == FIRST_ACCESSORY_AID and b == a + 1
    assert reg.aid("device:a") == a

    reloaded = IdRegistry(reg.as_dict())
    assert reloaded.aid("device:b") == b
    assert reloaded.aid("device:c") == b + 1
    assert not IdRegistry(reg.as_dict()).dirty


def _build(reg: IdRegistry, aid: int, *, with_speed: bool, with_light: bool) -> Accessory:
    loader = get_loader()
    acc = Accessory(SimpleNamespace(loader=loader), "Fan", aid=aid, iid_manager=StableIIDManager(reg, aid))
    fan = loader.get_service("Fanv2")
    if with_speed:
        fan.add_characteristic(loader.get_char("RotationSpeed"))
    if with_light:
        light = loader.get_service("Lightbulb")
        light.unique_id = None
        acc.add_service(light)
    fan.unique_id = None
    acc.add_service(fan)
    return acc


def _iids(acc: Accessory) -> dict[str, int]:
    out = {}
    for svc in acc.services:
        out[svc.display_name] = acc.iid_manager.get_iid(svc)
        for char in svc.characteristics:
            out[f"{svc.display_name}.{char.display_name}"] = acc.iid_manager.get_iid(char)
    return out


def test_iids_survive_adding_services_and_reordering():
    reg = IdRegistry()
    before = _iids(_build(reg, 2, with_speed=False, with_light=False))

    # Restart with a new optional char and an extra service added *before* the fan.
    after = _iids(_build(IdRegistry(reg.as_dict()), 2, with_speed=True, with_light=True))

    for key, iid in before.items():
        assert after[key] == iid, key
    assert len(set(after.values())) == len(after), "IIDs must be unique within an accessory"


def test_same_type_services_get_distinct_iids():
    reg = IdRegistry()
    loader = get_loader()
    acc = Accessory(SimpleNamespace(loader=loader), "Dual", aid=2, iid_manager=StableIIDManager(reg, 2))
    for uid in (None, "1"):
        svc = loader.get_service("Switch")
        svc.unique_id = uid
        acc.add_service(svc)
    iids = list(acc.iid_manager.iids.values())
    assert len(iids) == len(set(iids))


def test_place_fills_bridges_and_keeps_accessories_where_they_were():
    reg = IdRegistry()
    placed = reg.place(["a", "b", "c", "d", "e"], capacity=2)
    assert placed == {"a": 0, "b": 0, "c": 1, "d": 1, "e": 2}
    assert reg.dirty

    # "a" was removed: "e" stays on bridge 2 and the new "f" takes the free slot.
    reloaded = IdRegistry(reg.as_dict())
    placed = reloaded.place(["b", "c", "d", "e", "f"], capacity=2)
    assert placed == {"b": 0, "c": 1, "d": 1, "e": 2, "f": 0}
    assert IdRegistry(reloaded.as_dict()).place(["b", "c", "d", "e", "f"], 2) == placed


def test_place_on_a_registry_from_before_splitting_keeps_order():
    reg = IdRegistry({"aids": {"x": 2, "y": 3}})
    assert reg.place(["x", "y"], capacity=149) == {"x": 0, "y": 0}
