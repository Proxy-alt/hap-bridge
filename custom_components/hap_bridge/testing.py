"""Test a HomeKit profile without Home Assistant or a Home app.

For integration authors (and HAP Bridge's own tests). Runs your
`async_describe_device` against fake states, checks the result against
HomeKit's catalogue the way the bridge does at startup, and lets you read
values and simulate Home app writes:

    from custom_components.hap_bridge import testing
    from custom_components.acme import hap_bridge

    async def test_purifier():
        hass = testing.FakeHass()
        hass.states.set("fan.p", "on", percentage=50)
        hass.states.set("sensor.p_filter", "8")
        result = await testing.async_describe(hap_bridge.async_describe_device, [
            testing.entry("fan.p", platform="acme"),
            testing.entry("sensor.p_filter", platform="acme", translation_key="filter_life"),
        ], hass=hass)

        result.assert_valid()
        acc = result.accessory()
        assert result.values(acc)["FilterMaintenance.FilterChangeIndication"] == 1
        await result.async_write(acc, "AirPurifier", "Active", 0)
        assert hass.services.calls[-1][:2] == ("fan", "turn_off")

Nothing here needs Home Assistant to be importable.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from .api import AccessorySpec, HapContext, Settings
from .validate import validate_spec


class FakeStates:
    def __init__(self) -> None:
        self.data: dict[str, SimpleNamespace] = {}

    def set(self, entity_id: str, state: str, **attributes: Any) -> None:
        self.data[entity_id] = SimpleNamespace(
            entity_id=entity_id, state=state, attributes=attributes
        )

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self.data.get(entity_id)


class FakeServices:
    """Records service calls as (domain, service, data)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def async_call(self, domain: str, service: str, data: dict[str, Any], blocking: bool = False):
        self.calls.append((domain, service, data))
        return None


class FakeBus:
    """Records fired events as (event_type, data)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        self.events.append((event_type, data or {}))


class FakeHass(SimpleNamespace):
    """Just enough of `hass` for profiles: states, services, bus, unit system."""

    def __init__(self, *, fahrenheit: bool = False) -> None:
        super().__init__(
            states=FakeStates(),
            services=FakeServices(),
            bus=FakeBus(),
            config=SimpleNamespace(
                units=SimpleNamespace(temperature_unit="°F" if fahrenheit else "°C")
            ),
            config_entries=SimpleNamespace(async_get_entry=lambda _entry_id: None),
        )


def entry(entity_id: str, **kw: Any) -> SimpleNamespace:
    """A fake entity registry entry. Override any field: platform, device_class,
    translation_key, supported_features, capabilities, entity_category..."""
    fields = dict(
        id=f"reg-{entity_id}",
        entity_id=entity_id,
        domain=entity_id.split(".")[0],
        platform="test",
        config_entry_id=None,
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
    fields.update(kw)
    return SimpleNamespace(**fields)


def device(device_id: str = "device1", **kw: Any) -> SimpleNamespace:
    """A fake device registry entry."""
    fields = dict(
        id=device_id,
        name="Test device",
        name_by_user=None,
        manufacturer="Acme",
        model="Model 1",
        sw_version="1.0",
        serial_number=None,
        config_entries=set(),
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


_fake_device = device  # `async_describe` has a parameter of the same name

_LOADER: Any = None


def loader() -> Any:
    """HAP-python's service/characteristic catalogue (what validation checks against)."""
    global _LOADER
    if _LOADER is None:
        from pyhap.loader import get_loader

        _LOADER = get_loader()
    return _LOADER


@dataclass
class Result:
    ctx: HapContext
    hass: Any
    accessories: list[AccessorySpec] = field(default_factory=list)

    def accessory(self, index: int = 0) -> AccessorySpec:
        return self.accessories[index]

    def errors(self) -> dict[str, list[str]]:
        """Validation errors per accessory name (empty if all is well)."""
        out = {}
        for acc in self.accessories:
            errors, _warnings = validate_spec(acc, loader())
            if errors:
                out[acc.name] = errors
        return out

    def warnings(self) -> dict[str, list[str]]:
        out = {}
        for acc in self.accessories:
            _errors, warnings = validate_spec(acc, loader())
            if warnings:
                out[acc.name] = warnings
        return out

    def assert_valid(self) -> None:
        """Fail like the bridge would skip the accessory at startup."""
        if errors := self.errors():
            raise AssertionError(f"invalid HomeKit accessories: {errors}")

    @staticmethod
    def values(acc: AccessorySpec) -> dict[str, Any]:
        """Current values, as {"Service.Characteristic": value}."""
        return {
            f"{svc.type_name}.{name}": char.get()
            for svc in acc.services
            for name, char in svc.chars.items()
            if char.get is not None
        }

    @staticmethod
    def service(acc: AccessorySpec, type_name: str, index: int = 0) -> Any:
        return [s for s in acc.services if s.type_name == type_name][index]

    async def async_write(
        self, acc: AccessorySpec, service: str, characteristic: str, value: Any, *, index: int = 0
    ) -> None:
        """Write a value the way the Home app would (one characteristic)."""
        await self.async_write_many(acc, service, {characteristic: value}, index=index)

    async def async_write_many(
        self, acc: AccessorySpec, service: str, values: dict[str, Any], *, index: int = 0
    ) -> None:
        """Write several values to one service in one go, as the Home app often does."""
        svc = self.service(acc, service, index)
        for name, value in values.items():
            setter = svc.chars[name].set
            if setter is not None:
                await _maybe_await(setter(value))
        if svc.write is not None:
            await _maybe_await(svc.write(values))


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


async def async_describe(
    profile: Callable[[HapContext], Any],
    entities: list[Any],
    *,
    device: Any = None,
    hass: Any = None,
    settings: Settings | None = None,
) -> Result:
    """Run a profile (`async_describe_device` or a sync one) for a device's entities.

    `device` defaults to a fake device; pass `device=False` for entities
    without a device.
    """
    hass = hass or FakeHass()
    if device is None:
        device = _fake_device()
    ctx = HapContext(hass, device or None, list(entities), settings)
    ctx.source = "test"
    await _maybe_await(profile(ctx))
    # What the bridge adds to any profile's lights, so tests see it too.
    from .profiles.generic import adaptive_lighting

    adaptive_lighting(ctx)
    return Result(ctx, hass, list(ctx.accessories))
