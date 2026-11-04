"""How an accessory will look in the Home app, for the preview.

The raw preview lists services and characteristics; this says what a person
sees: which tiles, what each is called and shows, and what's folded into a
tile's details (a purifier's filter, a sprinkler's zones, a battery).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .api import AccessorySpec, ServiceSpec

# Service -> what the Home app calls the tile. Services not listed here
# (AccessoryInformation, ServiceLabel, InputSource, ...) never show as tiles.
KINDS = {
    "Lightbulb": "Light", "Switch": "Switch", "Outlet": "Outlet", "Fanv2": "Fan", "Fan": "Fan",
    "AirPurifier": "Air Purifier", "Thermostat": "Thermostat", "HeaterCooler": "Heater/Cooler",
    "HumidifierDehumidifier": "Humidifier", "LockMechanism": "Lock",
    "WindowCovering": "Blinds", "Window": "Window", "Door": "Door",
    "GarageDoorOpener": "Garage Door", "SecuritySystem": "Security System",
    "Valve": "Valve", "IrrigationSystem": "Sprinkler", "Faucet": "Faucet",
    "Television": "TV", "CameraRTPStreamManagement": "Camera", "Doorbell": "Doorbell",
    "StatelessProgrammableSwitch": "Button", "AirQualitySensor": "Air Quality",
    "CarbonDioxideSensor": "Carbon Dioxide", "CarbonMonoxideSensor": "Carbon Monoxide",
    "ContactSensor": "Contact Sensor", "HumiditySensor": "Humidity", "LeakSensor": "Leak Sensor",
    "LightSensor": "Light Sensor", "MotionSensor": "Motion Sensor",
    "OccupancySensor": "Occupancy Sensor", "SmokeSensor": "Smoke Sensor",
    "TemperatureSensor": "Temperature", "FilterMaintenance": "Filter",
    "BatteryService": "Battery", "Slats": "Slats",
}

# Characteristic -> how its value reads on a tile.
_STATES: list[tuple[str, Callable[[Any], str]]] = [
    ("CurrentTemperature", lambda v: f"{v:.1f}°C"),
    ("CurrentRelativeHumidity", lambda v: f"{v:.0f}%"),
    ("CurrentAmbientLightLevel", lambda v: f"{v:g} lx"),
    ("OccupancyDetected", lambda v: "Detected" if v else "Not detected"),
    ("MotionDetected", lambda v: "Motion" if v else "No motion"),
    ("ContactSensorState", lambda v: "Open" if v else "Closed"),
    ("LeakDetected", lambda v: "Leak" if v else "Dry"),
    ("SmokeDetected", lambda v: "Smoke" if v else "Clear"),
    ("LockCurrentState", lambda v: {0: "Unlocked", 1: "Locked", 2: "Jammed"}.get(v, "Unknown")),
    ("CurrentDoorState", lambda v: {0: "Open", 1: "Closed", 2: "Opening", 3: "Closing",
                                    4: "Stopped"}.get(v, "?")),
    ("SecuritySystemCurrentState", lambda v: {0: "Home", 1: "Away", 2: "Night", 3: "Off",
                                              4: "Triggered"}.get(v, "?")),
    ("CurrentPosition", lambda v: f"{v}% open"),
    ("InUse", lambda v: "Running" if v else "Idle"),
    ("On", lambda v: "On" if v else "Off"),
    ("Active", lambda v: "On" if v else "Off"),
]


def tile_state(svc: ServiceSpec) -> str | None:
    for name, fmt in _STATES:
        if (char := svc.chars.get(name)) is not None and char.get is not None:
            try:
                value = char.get()
                return None if value is None else fmt(value)
            except Exception:  # noqa: BLE001 - a broken getter shows as unknown
                return "?"
    return None


def describe_home(spec: AccessorySpec, name_of: Callable[[str], str]) -> dict[str, Any]:
    """The tiles `spec` becomes in the Home app."""
    linked = {id(other) for svc in spec.services for other in svc.linked}
    tiles = []
    for svc in spec.services:
        if id(svc) in linked or svc.type_name not in KINDS:
            continue
        if svc.type_name in ("FilterMaintenance", "BatteryService"):
            continue  # always details, even when not linked
        tiles.append(svc)
    if spec.camera:
        tiles = []  # one Camera tile (a doorbell's too); its sensors show inside it

    def tile_name(svc: ServiceSpec) -> str:
        if (char := svc.chars.get("Name")) is not None and char.get is not None:
            try:
                return str(char.get())
            except Exception:  # noqa: BLE001
                pass
        if len(tiles) < 2 or not (watched := svc.watched()):
            return spec.name
        name = name_of(watched[0])
        if name.lower().startswith(spec.name.lower()) and len(name) > len(spec.name):
            name = name[len(spec.name):].strip(" -:")
        return name or spec.name

    result_tiles = []
    for svc in tiles:
        details = [
            {"kind": KINDS.get(s.type_name, s.type_name), "name": tile_name(s),
             **({"state": st} if (st := tile_state(s)) else {})}
            for s in svc.linked if s.type_name in KINDS
        ]
        result_tiles.append({
            "name": tile_name(svc),
            "kind": KINDS.get(svc.type_name, svc.type_name),
            **({"state": st} if (st := tile_state(svc)) else {}),
            **({"details": details} if details else {}),
        })
    if spec.camera:
        result_tiles.insert(0, {"name": spec.name, "kind": "Camera"})

    notes = []
    if len(result_tiles) > 1:
        notes.append(
            "The Home app shows these as one tile; turn on Show as Separate Tiles "
            "in its settings to split them."
        )
    if spec.standalone:
        notes.append("Added on its own with its own pairing code, not through the bridge.")
    return {"tiles": result_tiles, **({"notes": notes} if notes else {})}
