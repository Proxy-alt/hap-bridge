"""Fixtures for tests that run the bridge inside a real Home Assistant.

Needs `pytest-homeassistant-custom-component` (the `test-ha` extra). The HAP
server itself isn't started: no sockets in tests, and it isn't ours to test.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.hap_bridge.const import DOMAIN


@pytest.fixture(autouse=True)
def _auto(enable_custom_integrations, mock_async_zeroconf):
    with (
        patch("custom_components.hap_bridge.bridge._Driver.async_start"),
        patch("custom_components.hap_bridge.bridge._Driver.async_stop"),
        patch("custom_components.hap_bridge.bridge._Driver.async_update_advertisement"),
        patch("custom_components.hap_bridge.bridge._Driver.persist"),
        patch("pyhap.util.get_local_address", return_value="127.0.0.1"),
        patch("custom_components.hap_bridge.bridge._port_free", return_value=True),
    ):
        yield


@pytest.fixture
async def config_entry(hass: HomeAssistant):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test Bridge", data={CONF_NAME: "Test Bridge", CONF_PORT: 21099}
    )
    entry.add_to_hass(hass)
    yield entry
    # Unload while the driver patches are still active.
    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
def lamp(hass: HomeAssistant) -> str:
    """A light on its own device, registered by a fake integration."""
    source = MockConfigEntry(domain="acme")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("acme", "lamp")}, name="Desk Lamp"
    )
    ent = er.async_get(hass).async_get_or_create(
        "light", "acme", "lamp", device_id=device.id, original_name="Desk Lamp",
        capabilities={"supported_color_modes": ["brightness"]},
    )
    hass.states.async_set(ent.entity_id, "on", {"brightness": 255})
    return ent.entity_id
