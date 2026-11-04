"""Building blocks for integration profiles: the bridge's own tested mappings.

A profile gets these as `ctx.blocks`. Each service block binds a Home
Assistant entity onto a service you created, the same way the bridge does
it for its own profiles, and returns that service:

    acc = ctx.accessory(category="AIR_PURIFIER")
    purifier = ctx.blocks.fan(acc.service("AirPurifier", primary=True), fan)
    ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=purifier),
                      life=filter_life, reset=reset_button)

Accessory blocks (`tv`, `activity_remote`, `camera`) build a whole
accessory and return it. Entities are entity IDs belonging to the device
being described. Getting a block wrong raises ValueError with what to fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .profiles import appliances, cameras, fallback, tv

if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import RegistryEntry

    from .api import AccessorySpec, HapContext, ServiceSpec

# keyword -> pollutant sensor device class, for `air_quality`
_POLLUTANTS = {
    "pm25": "pm25",
    "pm10": "pm10",
    "nitrogen_dioxide": "nitrogen_dioxide",
    "voc": "volatile_organic_compounds",
    "ozone": "ozone",
    "sulphur_dioxide": "sulphur_dioxide",
}


class Blocks:
    def __init__(self, ctx: HapContext) -> None:
        self._ctx = ctx

    # -- helpers --------------------------------------------------------------------

    def _entry(self, entity_id: str, *domains: str) -> RegistryEntry:
        entry = self._ctx.entry(entity_id)
        if entry is None:
            raise ValueError(f"{entity_id} is not one of this device's entities")
        if domains and entry.domain not in domains:
            raise ValueError(f"{entity_id} is a {entry.domain}; expected {' or '.join(domains)}")
        return entry

    @staticmethod
    def _expect(svc: ServiceSpec, *types: str) -> None:
        if svc.type_name not in types:
            raise ValueError(
                f"this block goes on a {' or '.join(types)} service, not {svc.type_name}"
            )

    # -- lights, switches, fans ---------------------------------------------------------

    def light(self, svc: ServiceSpec, light: str) -> ServiceSpec:
        """On/off, brightness, color and color temperature (with Adaptive Lighting)."""
        self._expect(svc, "Lightbulb")
        fallback.bind_light(self._ctx, svc, self._entry(light, "light"))
        return svc

    def switch(self, svc: ServiceSpec, entity: str) -> ServiceSpec:
        """`On` for anything with turn_on/turn_off (switch, input_boolean, script...)."""
        self._expect(svc, "Switch", "Outlet", "Lightbulb")
        fallback.bind_on_off(self._ctx, svc, self._entry(entity))
        return svc

    def fan(self, svc: ServiceSpec, fan: str) -> ServiceSpec:
        """Power, speed, oscillation; on Fanv2 also direction and an auto preset."""
        self._expect(svc, "Fanv2", "AirPurifier")
        fallback.bind_fan(self._ctx, svc, self._entry(fan, "fan"))
        return svc

    # -- climate ------------------------------------------------------------------------

    def thermostat(self, svc: ServiceSpec, climate: str, *, humidity: str | None = None) -> ServiceSpec:
        self._expect(svc, "Thermostat")
        if humidity is not None:
            self._entry(humidity, "sensor")
        fallback.bind_thermostat(self._ctx, svc, self._entry(climate, "climate"), humidity)
        return svc

    def heater_cooler(self, svc: ServiceSpec, climate: str) -> ServiceSpec:
        """AC-style control: power, heat/cool/auto, setpoints, fan speed, swing."""
        self._expect(svc, "HeaterCooler")
        appliances.bind_heater_cooler(self._ctx, svc, self._entry(climate, "climate"))
        return svc

    def climate_service_type(self, climate: str) -> str:
        """"HeaterCooler" for AC-style units, else "Thermostat" (core HomeKit's rule)."""
        entry = self._entry(climate, "climate")
        return "HeaterCooler" if appliances.uses_heater_cooler(entry) else "Thermostat"

    def humidifier(self, svc: ServiceSpec, humidifier: str, *, humidity: str | None = None) -> ServiceSpec:
        self._expect(svc, "HumidifierDehumidifier")
        appliances.bind_humidifier(self._ctx, svc, self._entry(humidifier, "humidifier"), humidity)
        return svc

    def water_heater(self, svc: ServiceSpec, water_heater: str) -> ServiceSpec:
        self._expect(svc, "Thermostat")
        appliances.bind_water_heater(self._ctx, svc, self._entry(water_heater, "water_heater"))
        return svc

    # -- openings, security -------------------------------------------------------------

    def lock(self, svc: ServiceSpec, lock: str) -> ServiceSpec:
        self._expect(svc, "LockMechanism")
        fallback.bind_lock(self._ctx, svc, self._entry(lock, "lock"))
        return svc

    def window_covering(self, svc: ServiceSpec, cover: str) -> ServiceSpec:
        """Position (and tilt) for a cover that can be set to a position."""
        self._expect(svc, "WindowCovering", "Window", "Door")
        fallback.bind_window_covering(self._ctx, svc, self._entry(cover, "cover"))
        return svc

    def garage_door(self, svc: ServiceSpec, cover: str, *, obstruction: str | None = None) -> ServiceSpec:
        self._expect(svc, "GarageDoorOpener")
        fallback.bind_garage_door(self._ctx, svc, self._entry(cover, "cover"), obstruction)
        return svc

    def security_system(self, svc: ServiceSpec, alarm: str) -> ServiceSpec:
        self._expect(svc, "SecuritySystem")
        appliances.bind_security_system(self._ctx, svc, self._entry(alarm, "alarm_control_panel"))
        return svc

    def valve(self, svc: ServiceSpec, valve: str) -> ServiceSpec:
        self._expect(svc, "Valve")
        appliances.bind_valve(self._ctx, svc, self._entry(valve, "valve"))
        return svc

    # -- things that are switches ------------------------------------------------------------

    def vacuum(self, svc: ServiceSpec, vacuum: str) -> ServiceSpec:
        self._expect(svc, "Switch")
        appliances.bind_vacuum(self._ctx, svc, self._entry(vacuum, "vacuum"))
        return svc

    def lawn_mower(self, svc: ServiceSpec, mower: str) -> ServiceSpec:
        self._expect(svc, "Switch")
        appliances.bind_lawn_mower(self._ctx, svc, self._entry(mower, "lawn_mower"))
        return svc

    def momentary(self, svc: ServiceSpec, entity: str) -> ServiceSpec:
        """A switch that runs a scene or presses a button, then turns itself off."""
        self._expect(svc, "Switch")
        appliances.bind_momentary(
            self._ctx, svc, self._entry(entity, *appliances.MOMENTARY)
        )
        return svc

    # -- events --------------------------------------------------------------------------

    def doorbell(self, svc: ServiceSpec, event: str) -> ServiceSpec:
        """Rings once per new event of a doorbell `event` entity."""
        self._expect(svc, "Doorbell", "StatelessProgrammableSwitch")
        fallback.bind_doorbell(self._ctx, svc, self._entry(event, "event"))
        return svc

    def button(self, svc: ServiceSpec, event: str) -> ServiceSpec:
        """Single/double/long presses of a button `event` entity, for Home automations."""
        self._expect(svc, "StatelessProgrammableSwitch")
        entry = self._entry(event, "event")
        if not fallback.button_presses(entry):
            raise ValueError(f"{event} has no event types HomeKit can show as presses")
        fallback.bind_button(self._ctx, svc, entry)
        return svc

    # -- sensors --------------------------------------------------------------------------

    def sensor_service_type(self, sensor: str) -> str | None:
        """The HomeKit service for a sensor/binary sensor, or None if HomeKit has none."""
        return fallback.sensor_service(self._entry(sensor, "sensor", "binary_sensor"))

    def sensor(self, svc: ServiceSpec, sensor: str) -> ServiceSpec:
        """A sensor or binary sensor; `svc` must be of `sensor_service_type(sensor)`."""
        entry = self._entry(sensor, "sensor", "binary_sensor")
        expected = fallback.sensor_service(entry)
        if expected is None:
            raise ValueError(f"HomeKit has no service for {sensor}")
        self._expect(svc, expected)
        fallback.bind_sensor(self._ctx, svc, entry)
        return svc

    def air_quality(self, svc: ServiceSpec, **pollutants: str) -> ServiceSpec:
        """AirQuality from the worst pollutant, plus densities.

        Keywords: pm25, pm10, nitrogen_dioxide, voc, ozone, sulphur_dioxide.
        """
        self._expect(svc, "AirQualitySensor")
        unknown = set(pollutants) - set(_POLLUTANTS)
        if unknown or not pollutants:
            raise ValueError(f"air_quality takes some of {', '.join(_POLLUTANTS)}")
        fallback.bind_air_quality(
            self._ctx, svc, {_POLLUTANTS[k]: self._entry(v, "sensor").entity_id
                             for k, v in pollutants.items()},
        )
        return svc

    def filter(
        self,
        svc: ServiceSpec,
        *,
        life: str | None = None,
        change: str | None = None,
        reset: str | None = None,
    ) -> ServiceSpec:
        """Filter life (%), a "replace filter" binary sensor, a "reset filter" button."""
        self._expect(svc, "FilterMaintenance")
        if life is None and change is None:
            raise ValueError("filter needs `life` or `change`")
        fallback.bind_filter(self._ctx, svc, life=life, change=change, reset=reset)
        return svc

    def battery(
        self, svc: ServiceSpec, level: str, *, low: str | None = None, charging: str | None = None
    ) -> ServiceSpec:
        """Battery level (%), with optional low-battery and charging binary sensors."""
        self._expect(svc, "BatteryService")
        fallback.bind_battery(self._ctx, svc, self._entry(level, "sensor").entity_id,
                              low=low, charging=charging)
        return svc

    # -- whole accessories ----------------------------------------------------------------

    def tv(self, media_player: str, *, remote: str | None = None) -> AccessorySpec:
        """A standalone Television: inputs, Remote widget keys, volume, settings."""
        entry = self._entry(media_player, "media_player")
        if remote is not None:
            self._entry(remote, "remote")
            self._ctx.claim(remote)
        return tv.describe_tv(self._ctx, entry, remote)

    def activity_remote(self, remote: str) -> AccessorySpec:
        """A standalone Television whose inputs are the remote's activities."""
        acc = tv.describe_activity_remote(self._ctx, self._entry(remote, "remote"))
        self._ctx.claim(remote)
        return acc

    def camera(
        self,
        camera: str,
        *,
        doorbell: str | None = None,
        motion: str | None = None,
        audio: bool = False,
    ) -> AccessorySpec:
        """A standalone camera (live video, snapshots); a video doorbell with `doorbell`.

        `audio=True` sends the stream's sound too; only for streams with audio.
        """
        self._entry(camera, "camera")
        if doorbell is not None:
            self._entry(doorbell, "event")
        return cameras.describe_camera(
            self._ctx, camera, doorbell=doorbell, motion=motion, audio=audio
        )
