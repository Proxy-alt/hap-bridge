"""Shims: profiles for integrations you don't maintain, from files and packs."""

from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

from custom_components.hap_bridge.const import DOMAIN
from custom_components.hap_bridge.shims import DATA_PACKS, read_pack

PURIFIER_SHIM = '''
HANDLES = {"integration": "zha", "manufacturer": "IKEA*", "model": "STARKVIND*"}

async def async_describe_device(ctx):
    fan = ctx.entity("fan")
    acc = ctx.accessory(category="AIR_PURIFIER")
    svc = ctx.blocks.fan(acc.service("AirPurifier", primary=True), fan)
    svc.char("CurrentAirPurifierState", watch=[fan], get=lambda: 2)
    svc.char("TargetAirPurifierState", watch=[fan], get=lambda: {target})
'''


@pytest.fixture
def shim_dir(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    directory = tmp_path / "hap_bridge" / "profiles"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
async def starkvind(hass):
    source = MockConfigEntry(domain="zha")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("zha", "s1")}, name="Starkvind",
        manufacturer="IKEA of Sweden", model="STARKVIND Air purifier")
    fan = er.async_get(hass).async_get_or_create(
        "fan", "zha", "s1", device_id=device.id, supported_features=1)
    hass.states.async_set(fan.entity_id, "on", {"percentage": 20})
    return SimpleNamespace(device=device, fan=fan.entity_id)


async def _start(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _preview(hass, device_id):
    return await hass.services.async_call(
        DOMAIN, "preview", {"device_id": device_id}, blocking=True, return_response=True)


def _sources(bridge):
    return {a["name"]: a["source"] for a in bridge.diagnostics()["accessories"]}


def _issue(hass, entry, suffix):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"profile_{entry.entry_id}_{suffix}")


async def test_file_shim_matches_by_model(hass, config_entry, starkvind, shim_dir):
    (shim_dir / "starkvind.py").write_text(PURIFIER_SHIM.replace("{target}", "0"))
    bridge = await _start(hass, config_entry)
    assert _sources(bridge)["Starkvind"] == "shim:starkvind"


async def test_preview_picks_up_edits_without_reload(hass, config_entry, starkvind, shim_dir):
    path = shim_dir / "starkvind.py"
    path.write_text(PURIFIER_SHIM.replace("{target}", "0"))
    await _start(hass, config_entry)

    def target(result):
        (acc,) = result["accessories"]
        return acc["services"][0]["characteristics"]["TargetAirPurifierState"]["value"]

    assert target(await _preview(hass, starkvind.device.id)) == 0
    path.write_text(PURIFIER_SHIM.replace("{target}", "1"))
    assert target(await _preview(hass, starkvind.device.id)) == 1


async def test_more_specific_shim_wins_and_ties_are_reported(hass, config_entry, starkvind, shim_dir):
    # Named after the integration: handles every zha device, less specifically.
    (shim_dir / "zha.py").write_text(
        "async def async_describe_device(ctx):\n    raise AssertionError('not me')\n")
    (shim_dir / "starkvind.py").write_text(PURIFIER_SHIM.replace("{target}", "0"))
    result = await _preview_after_start(hass, config_entry, starkvind)
    assert result["accessories"][0]["source"] == "shim:starkvind"
    assert result["problems"] == []

    # Two equally specific shims: neither runs, both are reported.
    (shim_dir / "starkvind_copy.py").write_text(PURIFIER_SHIM.replace("{target}", "1"))
    result = await _preview(hass, starkvind.device.id)
    assert result["accessories"][0]["source"] == "fallback"
    assert {p["shim"] for p in result["problems"]} == {"starkvind", "starkvind_copy"}


async def _preview_after_start(hass, entry, starkvind):
    await _start(hass, entry)
    return await _preview(hass, starkvind.device.id)


async def test_broken_shims_fall_back_with_repair_issues(hass, config_entry, starkvind, shim_dir):
    (shim_dir / "starkvind.py").write_text(
        'HANDLES = {"model": "STARKVIND*"}\n'
        "async def async_describe_device(ctx):\n"
        "    ctx.accessory().service('AirPurifier')\n"
        "    raise RuntimeError('no such attribute')\n")
    (shim_dir / "typo.py").write_text('HANDLES = {"modle": "X"}\nasync def async_describe_device(ctx): pass\n')
    (shim_dir / "syntax.py").write_text("def broken(:\n")
    bridge = await _start(hass, config_entry)

    (fan_acc,) = [a for a in bridge.diagnostics()["accessories"] if starkvind.fan in a["watches"]]
    assert fan_acc["source"] == "fallback"  # what the shim built was rolled back
    issue = _issue(hass, config_entry, "shim_starkvind")
    assert issue.translation_key == "shim_error"
    assert "RuntimeError: no such attribute" in issue.translation_placeholders["problems"]
    assert issue.translation_placeholders["origin"].endswith("starkvind.py")
    assert "not modle" in _issue(hass, config_entry, "shim_typo").translation_placeholders["problems"]
    assert "SyntaxError" in _issue(hass, config_entry, "shim_syntax").translation_placeholders[
        "problems"]

    for name in ("starkvind.py", "typo.py", "syntax.py"):
        (shim_dir / name).unlink()
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN and "shim_" in i]


async def test_shim_runs_before_the_integrations_own_profile(hass, config_entry, starkvind, shim_dir):
    # A shim claims only the fan; the rest of the device still gets the usual steps.
    lamp = er.async_get(hass).async_get_or_create(
        "light", "zha", "s1_light", device_id=starkvind.device.id)
    hass.states.async_set(lamp.entity_id, "on")
    (shim_dir / "starkvind.py").write_text(PURIFIER_SHIM.replace("{target}", "0"))
    result = await _preview_after_start(hass, config_entry, starkvind)
    # The fan is the shim's; the light, left over, goes through the fallback.
    assert sorted(a["source"] for a in result["accessories"]) == ["fallback", "shim:starkvind"]


async def test_profile_packs(hass, config_entry, starkvind, shim_dir):
    async def describe(ctx):
        fan = ctx.entity("fan")
        acc = ctx.accessory(category="FAN")
        ctx.blocks.fan(acc.service("Fanv2", primary=True), fan)

    platform = SimpleNamespace(SHIMS=[
        {"name": "starkvind", "handles": {"model": "STARKVIND*"}, "describe": describe},
        {"name": "bad", "handles": {"colour": "red"}, "describe": describe},
    ])
    hass.data[DATA_PACKS] = {"community_shims": read_pack("community_shims", platform)}
    bridge = await _start(hass, config_entry)
    assert _sources(bridge)["Starkvind"] == "shim:community_shims/starkvind"
    issue = _issue(hass, config_entry, "shim_community_shims_bad")
    assert "not colour" in issue.translation_placeholders["problems"]
    assert read_pack("x", SimpleNamespace()).errors[0][2] == "hap_bridge_shims.py has no SHIMS list"
