"""Decide how each device is exposed.

For every device, in order:
  1. the owning integration's own `hap_bridge.py` platform, if it ships one;
  2. bundled profiles (generic device shapes, then per-integration ones);
  3. the per-entity fallback for whatever is still unclaimed;
  4. finishers (e.g. battery) that add to the accessories built so far.
Each step only sees entities the previous steps didn't claim.

Alongside the accessories, collection records why each left-out entity was
left out, and what went wrong in integrations' own profiles, for previews,
diagnostics and repair issues.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from types import ModuleType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from ..api import API_VERSION, AccessorySpec, HapContext, Settings
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


@dataclass
class Problem:
    """Something wrong with an integration's own profile."""

    integration: str
    message: str
    device: str | None = None


@dataclass
class Collection:
    specs: list[AccessorySpec] = field(default_factory=list)
    # entity_id -> why it isn't exposed
    excluded: dict[str, str] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)


async def async_collect_specs(
    hass: HomeAssistant,
    entry: ConfigEntry,
    settings: Settings | None = None,
    *,
    device_id: str | None = None,
    entity_id: str | None = None,
) -> Collection:
    """Describe every included device; or only one device or entity (previews)."""
    dev_reg = dr.async_get(hass)
    result = Collection()

    by_device: dict[str | None, list[er.RegistryEntry]] = defaultdict(list)
    for ent in er.async_get(hass).entities.values():
        if ent.disabled_by is not None or ent.platform == DOMAIN:
            continue
        if device_id is not None and ent.device_id != device_id:
            continue
        if entity_id is not None and ent.entity_id != entity_id:
            continue
        if (reason := exclusion_reason(entry.options, ent)) is not None:
            result.excluded[ent.entity_id] = reason
            continue
        by_device[ent.device_id].append(ent)

    platforms: dict[str, ModuleType | None] = {}
    for dev_id, entries in by_device.items():
        if dev_id is None:
            # Device-less entities (helpers, templates) each get their own context
            # so they never get grouped by accident.
            contexts = [HapContext(hass, None, [e], settings) for e in entries]
        else:
            contexts = [HapContext(hass, dev_reg.async_get(dev_id), entries, settings)]

        for ctx in contexts:
            for domain in sorted({e.platform for e in ctx.entries}):
                module = await _async_platform(hass, platforms, domain, result)
                if module is not None:
                    await _async_run_integration(ctx, domain, module, result)

            ctx.source = "profile"
            for describe in generic.PROFILES:
                ctx.source = f"profile:{describe.__name__}"
                describe(ctx)
            for domain in {e.platform for e in ctx.entries}:
                for describe in generic.BY_INTEGRATION.get(domain, ()):
                    ctx.source = f"profile:{domain}.{describe.__name__}"
                    describe(ctx)

            ctx.source = "fallback"
            for ent in ctx.unclaimed():
                fallback.describe_entity(ctx, ent)
            for finish in generic.FINISHERS:
                finish(ctx)

            for ent in ctx.unclaimed():
                result.excluded[ent.entity_id] = fallback.why_not(ent)
            result.specs.extend(ctx.accessories)

    return result


async def _async_run_integration(
    ctx: HapContext, domain: str, module: ModuleType, result: Collection
) -> None:
    """Run an integration's own profile; on failure undo what it did and record why."""
    device = ctx.device.id if ctx.device else None
    describe = getattr(module, "async_describe_device", None)
    if describe is None:
        result.problems.append(Problem(domain, f"{PLATFORM}.py has no async_describe_device", device))
        return
    needed = getattr(module, "HAP_BRIDGE_API", API_VERSION)
    if needed > API_VERSION:
        result.problems.append(Problem(
            domain,
            f"needs HAP Bridge API version {needed}; this HAP Bridge has {API_VERSION}. "
            "Update HAP Bridge to use it.",
            device,
        ))
        return
    ctx.source = f"integration:{domain}"
    before_accessories, before_claimed = len(ctx.accessories), set(ctx._claimed)
    try:
        await describe(ctx)
    except Exception as err:  # noqa: BLE001 - a broken profile falls back
        _LOGGER.exception("%s.%s failed; using the bridge's own mapping", domain, PLATFORM)
        # Undo whatever it built, so the bundled profiles and fallback see everything.
        del ctx.accessories[before_accessories:]
        ctx._claimed = before_claimed
        result.problems.append(Problem(domain, f"{type(err).__name__}: {err}", device))


def exclusion_reason(options: Mapping[str, Any], ent: er.RegistryEntry) -> str | None:
    """Why the options-flow filters leave an entity out, or None. Empty include
    lists mean "everything"."""
    if (integrations := options.get(CONF_INCLUDE_INTEGRATIONS)) and ent.platform not in integrations:
        return f"options: {ent.platform} isn't an included integration"
    if (domains := options.get(CONF_INCLUDE_DOMAINS)) and ent.domain not in domains:
        return f"options: {ent.domain} isn't an included domain"
    if ent.device_id is not None and ent.device_id in options.get(CONF_EXCLUDE_DEVICES, ()):
        return "options: its device is excluded"
    if ent.entity_id in options.get(CONF_EXCLUDE_ENTITIES, ()):
        return "options: excluded"
    return None


def is_included(options: Mapping[str, Any], ent: er.RegistryEntry) -> bool:
    return ent.platform != DOMAIN and exclusion_reason(options, ent) is None


async def _async_platform(
    hass: HomeAssistant, cache: dict[str, ModuleType | None], domain: str, result: Collection
) -> ModuleType | None:
    if domain not in cache:
        cache[domain] = None
        try:
            integration = await async_get_integration(hass, domain)
        except Exception:  # noqa: BLE001 - e.g. a removed custom integration
            return None
        if not integration.platforms_exists([PLATFORM]):
            return None
        try:
            cache[domain] = await integration.async_get_platform(PLATFORM)
        except Exception as err:  # noqa: BLE001 - the integration ships one but it's broken
            _LOGGER.exception("Could not load %s.%s", domain, PLATFORM)
            result.problems.append(Problem(domain, f"{PLATFORM}.py failed to import: {err}"))
    return cache[domain]
