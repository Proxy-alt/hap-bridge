"""scripts/assign_rooms.py: matching Home app accessories by serial number."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("assign_rooms", ROOT / "scripts" / "assign_rooms.py")
assign_rooms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assign_rooms)


def _detail(serial, by="type"):
    char = ({"type": assign_rooms.SERIAL_TYPE.lower(), "name": "x"} if by == "type"
            else {"type": "?", "name": "Serial Number"})
    return {"services": [
        {"type": "0000003E", "characteristics": [{"type": "00000020", "value": "Acme"},
                                                 {**char, "value": serial}]},
        {"type": "00000043", "characteristics": [{"type": "00000025", "value": True}]},
    ]}


def test_serial_of():
    assert assign_rooms.serial_of(_detail("HB-1")) == "HB-1"
    assert assign_rooms.serial_of(_detail("HB-2", by="name")) == "HB-2"
    assert assign_rooms.serial_of({"services": []}) is None


def test_plan_matches_by_serial_whatever_the_name():
    wanted = [
        {"serial": "HB-A", "name": "Desk Lamp", "room": "Office"},
        {"serial": "HB-B", "name": "Fan", "room": "Bedroom"},
        {"serial": "HB-C", "name": "Gone", "room": "Attic"},
    ]
    home = [
        {"id": "U1", "name": "My favourite lamp"},  # renamed in the Home app
        {"id": "U2", "name": "Fan"},
        {"id": "U3", "name": "Fan"},  # same name, someone else's fan
        {"id": "BR", "name": "HAP Bridge", "is_bridge": True},
    ]
    details = {"U1": _detail("HB-A"), "U2": _detail("XYZ-9"), "U3": _detail("HB-B")}
    read = []

    def detail(uuid):
        read.append(uuid)
        return details.get(uuid)

    assignments, missing = assign_rooms.plan(wanted, home, detail)
    assert assignments == [{"uuid": "U1", "room": "Office"}, {"uuid": "U3", "room": "Bedroom"}]
    assert [m["name"] for m in missing] == ["Gone"]
    assert "BR" not in read
