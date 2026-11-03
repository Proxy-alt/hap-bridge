"""Decide how each device is exposed.

For every device, in order:
  1. the owning integration's own `hap_bridge.py` platform, if it ships one;
  2. bundled profiles (generic device shapes, then per-integration ones);
  3. the per-entity fallback for whatever is still unclaimed.
Each step only sees entities the previous steps didn't claim.
"""

from __future__ import annotations

from collections import defaultdict
import logging
from types import ModuleType

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from ..api import AccessorySpec, HapContext
from ..const import CONF_INCLUDE_INTEGRATIONS, PLATFORM
from . import fallback, generic

_LOGGER = logging.getLogger(__name__)


async def async_collect_specs(hass: HomeAssistant, entry: ConfigEntry) -> list[AccessorySpec]:
    include = set(entry.options.get(CONF_INCLUDE_INTEGRATIONS, []))
    dev_reg = dr.async_get(hass)

    by_device: dict[str | None, list[er.RegistryEntry]] = defaultdict(list)
    for ent in er.async_get(hass).entities.values():
        if ent.disabled_by is not None:
            continue
        if include and ent.platform not in include:
            continue
        by_device[ent.device_id].append(ent)

    platforms: dict[str, ModuleType | None] = {}
    specs: list[AccessorySpec] = []

    for device_id, entries in by_device.items():
        if device_id is None:
            # Device-less entities (helpers, templates) each get their own context
            # so they never get grouped by accident.
            contexts = [HapContext(hass, None, [e]) for e in entries]
        else:
            contexts = [HapContext(hass, dev_reg.async_get(device_id), entries)]

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

            specs.extend(ctx.accessories)

    return specs


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
