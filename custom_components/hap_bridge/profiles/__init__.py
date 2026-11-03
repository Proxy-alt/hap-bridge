"""Decide how each device is exposed.

For every device, in order:
  1. the owning integration's own `hap_bridge.py` platform, if it ships one;
  2. bundled profiles (generic device shapes, then per-integration ones);
  3. the per-entity fallback for whatever is still unclaimed;
  4. finishers (e.g. battery) that add to the accessories built so far.
Each step only sees entities the previous steps didn't claim.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
import logging
from types import ModuleType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from ..api import AccessorySpec, HapContext, Settings
from ..const import (
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_INTEGRATIONS,
    DOMAIN,
    PLATFORM,
)
from . import fallback, generic

_LOGGER = logging.getLogger(__name__)


async def async_collect_specs(
    hass: HomeAssistant, entry: ConfigEntry, settings: Settings | None = None
) -> list[AccessorySpec]:
    dev_reg = dr.async_get(hass)

    by_device: dict[str | None, list[er.RegistryEntry]] = defaultdict(list)
    for ent in er.async_get(hass).entities.values():
        if ent.disabled_by is None and is_included(entry.options, ent):
            by_device[ent.device_id].append(ent)

    platforms: dict[str, ModuleType | None] = {}
    specs: list[AccessorySpec] = []

    for device_id, entries in by_device.items():
        if device_id is None:
            # Device-less entities (helpers, templates) each get their own context
            # so they never get grouped by accident.
            contexts = [HapContext(hass, None, [e], settings) for e in entries]
        else:
            contexts = [HapContext(hass, dev_reg.async_get(device_id), entries, settings)]

        for ctx in contexts:
            for domain in sorted({e.platform for e in ctx.entries}):
                module = await _async_platform(hass, platforms, domain)
                if module is not None:
                    try:
                        await module.async_describe_device(ctx)
                    except Exception:  # noqa: BLE001 - a broken profile falls back
                        _LOGGER.exception("%s.%s failed; using fallback", domain, PLATFORM)

            for describe in generic.PROFILES:
                describe(ctx)
            for domain in {e.platform for e in ctx.entries}:
                for describe in generic.BY_INTEGRATION.get(domain, ()):
                    describe(ctx)

            for ent in ctx.unclaimed():
                fallback.describe_entity(ctx, ent)
            for finish in generic.FINISHERS:
                finish(ctx)

            specs.extend(ctx.accessories)

    return specs


def is_included(options: Mapping[str, Any], ent: er.RegistryEntry) -> bool:
    """Apply the options-flow filters. Empty include lists mean "everything"."""
    if ent.platform == DOMAIN:
        return False  # the bridge's own entities
    if (integrations := options.get(CONF_INCLUDE_INTEGRATIONS)) and ent.platform not in integrations:
        return False
    if (domains := options.get(CONF_INCLUDE_DOMAINS)) and ent.domain not in domains:
        return False
    if ent.device_id is not None and ent.device_id in options.get(CONF_EXCLUDE_DEVICES, ()):
        return False
    return ent.entity_id not in options.get(CONF_EXCLUDE_ENTITIES, ())


async def _async_platform(
    hass: HomeAssistant, cache: dict[str, ModuleType | None], domain: str
) -> ModuleType | None:
    if domain not in cache:
        try:
            integration = await async_get_integration(hass, domain)
            cache[domain] = await integration.async_get_platform(PLATFORM)
        except ImportError:
            cache[domain] = None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not load %s.%s", domain, PLATFORM)
            cache[domain] = None
    return cache[domain]
