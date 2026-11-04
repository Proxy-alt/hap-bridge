"""Integration profiles end to end: sources, repair issues, preview, runtime data."""

from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from custom_components.hap_bridge.const import DOMAIN

ACME = "acme_purifiers"


@pytest.fixture
async def purifier(hass):
    source = MockConfigEntry(domain=ACME)
    source.add_to_hass(hass)
    source.runtime_data = SimpleNamespace(client="acme-client")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={(ACME, "p1")}, name="Bedroom Purifier")
    reg = er.async_get(hass)
    fan = reg.async_get_or_create("fan", ACME, "p1", device_id=device.id,
                                  config_entry=source, supported_features=1)
    life = reg.async_get_or_create("sensor", ACME, "p1_filter", device_id=device.id,
                                   config_entry=source, translation_key="filter_life")
    rssi = reg.async_get_or_create("sensor", ACME, "p1_rssi", device_id=device.id,
                                   config_entry=source, entity_category=EntityCategory.DIAGNOSTIC)
    hass.states.async_set(fan.entity_id, "on", {"percentage": 40})
    hass.states.async_set(life.entity_id, "55")
    hass.states.async_set(rssi.entity_id, "-60")
    return SimpleNamespace(device=device, fan=fan.entity_id, life=life.entity_id,
                           rssi=rssi.entity_id, source=source)


def _profile(describe, **attrs) -> ModuleType:
    module = ModuleType(f"{ACME}.hap_bridge")
    module.async_describe_device = describe
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def _ship(module: ModuleType | None):
    """Pretend ACME ships `module` as its hap_bridge.py."""
    from homeassistant import loader

    real = loader.async_get_integration

    async def fake(hass, domain):
        if domain != ACME:
            return await real(hass, domain)
        return SimpleNamespace(
            platforms_exists=lambda names: list(names) if module else [],
            async_get_platform=_async_return(module),
        )

    return patch("custom_components.hap_bridge.profiles.async_get_integration", fake)


def _async_return(value):
    async def get(_name):
        return value

    return get


async def good(ctx):
    fan, life = ctx.entity("fan"), ctx.entity("sensor", translation_key="filter_life")
    acc = ctx.accessory(category="AIR_PURIFIER")
    svc = ctx.blocks.fan(acc.service("AirPurifier", primary=True), fan)
    svc.char("CurrentAirPurifierState", watch=[fan], get=lambda: 2)
    svc.char("TargetAirPurifierState", watch=[fan], get=lambda: 0)
    ctx.blocks.filter(acc.service("FilterMaintenance", linked_to=svc), life=life)
    ctx.seen_client = ctx.runtime_data(ACME).client


async def broken(ctx):
    ctx.accessory(category="AIR_PURIFIER").service("AirPurifier")
    raise RuntimeError("firmware lookup failed")


async def invalid(ctx):
    acc = ctx.accessory()
    acc.service("AirPurifier", primary=True).char("Brightness", watch=[ctx.entity("fan")],
                                                   get=lambda: 1)


async def _start(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"profile_{entry.entry_id}_{ACME}")


async def test_integration_profile_is_used(hass, config_entry, purifier):
    seen = {}

    async def spy(ctx):
        await good(ctx)
        seen["client"] = ctx.seen_client

    with _ship(_profile(spy)):
        bridge = await _start(hass, config_entry)
    diag = bridge.diagnostics()
    (acc,) = [a for a in diag["accessories"] if purifier.fan in a["watches"]]
    assert acc["source"] == f"integration:{ACME}"
    assert [s["type"] for s in acc["services"]][1:] == ["AirPurifier", "FilterMaintenance"]
    assert seen["client"] == "acme-client"
    assert diag["excluded"][purifier.rssi] == "diagnostic entity"
    assert _issue(hass, config_entry) is None


async def test_broken_profile_falls_back_and_raises_issue(hass, config_entry, purifier):
    with _ship(_profile(broken)):
        bridge = await _start(hass, config_entry)
    # The half-built accessory was undone; the bundled purifier profile took over.
    (acc,) = [a for a in bridge.diagnostics()["accessories"] if purifier.fan in a["watches"]]
    assert acc["source"] == "profile:air_purifier"
    issue = _issue(hass, config_entry)
    assert issue is not None and issue.translation_key == "profile_error"
    assert "RuntimeError: firmware lookup failed" in issue.translation_placeholders["problems"]

    # Fixed in the next release: the issue goes away on the next start.
    with _ship(_profile(good)):
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
    assert _issue(hass, config_entry) is None


async def test_invalid_accessory_is_reported(hass, config_entry, purifier):
    with _ship(_profile(invalid)):
        await _start(hass, config_entry)
    problems = _issue(hass, config_entry).translation_placeholders["problems"]
    assert "AirPurifier has no characteristic 'Brightness'" in problems


async def test_too_new_api_is_skipped_cleanly(hass, config_entry, purifier):
    with _ship(_profile(good, HAP_BRIDGE_API=99)):
        bridge = await _start(hass, config_entry)
    assert "needs HAP Bridge API version 99" in _issue(hass, config_entry).translation_placeholders[
        "problems"]
    assert any(a["source"] == "profile:air_purifier" for a in bridge.diagnostics()["accessories"])


async def test_preview(hass, config_entry, purifier):
    await _start(hass, config_entry)
    with _ship(_profile(good)):
        result = await hass.services.async_call(
            DOMAIN, "preview", {"device_id": purifier.device.id}, blocking=True, return_response=True)
    (acc,) = result["accessories"]
    assert acc["source"] == f"integration:{ACME}"
    assert acc["errors"] == []
    purifier_svc = acc["services"][0]
    assert purifier_svc["type"] == "AirPurifier" and purifier_svc["primary"]
    assert purifier_svc["characteristics"]["Active"] == {
        "value": 1, "writable": True, "watches": [purifier.fan]}
    assert result["excluded"] == {purifier.rssi: "diagnostic entity"}
    assert result["problems"] == []
    (home,) = result["home"]
    assert home["tiles"][0]["kind"] == "Air Purifier"

    with _ship(_profile(broken)):
        result = await hass.services.async_call(
            DOMAIN, "preview", {"entity_id": purifier.fan}, blocking=True, return_response=True)
    assert result["problems"][0]["problem"] == "RuntimeError: firmware lookup failed"


async def test_preview_needs_a_bridge(hass, purifier):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "preview", {"device_id": purifier.device.id}, blocking=True,
            return_response=True)


async def test_integration_light_gets_adaptive_lighting(hass, config_entry):
    source = MockConfigEntry(domain=ACME)
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={(ACME, "l1")}, name="Desk Lamp")
    light = er.async_get(hass).async_get_or_create(
        "light", ACME, "l1", device_id=device.id, config_entry=source,
        capabilities={"supported_color_modes": ["color_temp"],
                      "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500})
    hass.states.async_set(light.entity_id, "on", {"brightness": 128, "color_temp_kelvin": 4000})

    async def lamp(ctx):
        eid = ctx.entity("light")
        svc = ctx.accessory(category="LIGHTBULB").service("Lightbulb", primary=True)
        svc.char("On", watch=[eid], get=lambda: ctx.is_on(eid))
        svc.char("Brightness", watch=[eid], get=lambda: 50)
        svc.char("ColorTemperature", watch=[eid], get=lambda: 250)
        svc.on_write(lambda values: None)

    with _ship(_profile(lamp)):
        bridge = await _start(hass, config_entry)
    assert bridge.diagnostics()["adaptive_lighting"] == {light.entity_id: False}
    (acc,) = [a for a in bridge.diagnostics()["accessories"] if light.entity_id in a["watches"]]
    assert acc["source"] == f"integration:{ACME}"
