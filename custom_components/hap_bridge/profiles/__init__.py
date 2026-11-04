"""Decide how each device is exposed.

For every device, in order:
  0. the best-matching shim, if any (a profile someone else wrote for it;
     see shims.py);
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
import hashlib
import inspect
import logging
from types import ModuleType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.loader import async_get_integration

from ..api import API_VERSION, AccessorySpec, HapContext, Settings
from ..const import (
    CONF_ALARM_CODES,
    CONF_CAMERA_AUDIO,
    CONF_EXCLUDE_DEVICES,
    CONF_EXCLUDE_ENTITIES,
    CONF_INCLUDE_DOMAINS,
    CONF_INCLUDE_INTEGRATIONS,
    CONF_NAMES,
    CONF_NO_PREBUFFER,
    DOMAIN,
    PLATFORM,
)
from ..shims import LoadedShims, Shim, best_shims
from . import fallback, generic

_LOGGER = logging.getLogger(__name__)


@dataclass
class Problem:
    """Something wrong with an integration's own profile, or with a shim."""

    integration: str  # the integration, or for shims the shim's name
    message: str
    device: str | None = None
    shim: bool = False
    origin: str | None = None  # a shim's file or pack


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
    shims: LoadedShims | None = None,
) -> Collection:
    """Describe every included device; or only one device or entity (previews)."""
    dev_reg = dr.async_get(hass)
    result = Collection()
    if shims is not None:
        for name, origin, message in shims.errors:
            result.problems.append(Problem(name, message, shim=True, origin=origin))

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
            ctx.alarm_codes = entry.options.get(CONF_ALARM_CODES) or {}

        for ctx in contexts:
            if shims is not None:
                await _async_run_shim(ctx, shims.shims, result)
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

    if device_id is None and entity_id is None:
        _anyone_home(hass, settings, by_device, result)
    apply_options(entry.options, result.specs)
    assign_serials(entry.entry_id, result.specs)
    return result


def _anyone_home(
    hass: HomeAssistant,
    settings: Settings | None,
    by_device: Mapping[str | None, list[er.RegistryEntry]],
    result: Collection,
) -> None:
    """With two or more people, an "Anyone Home" occupancy sensor for automations."""
    people = [e for entries in by_device.values() for e in entries if e.domain == "person"]
    if len(people) < 2:
        return
    ctx = HapContext(hass, None, people, settings)
    ctx.source = "profile:anyone_home"
    acc = ctx.accessory("Anyone Home", category="SENSOR", key="anyone_home")
    fallback.bind_anyone_home(
        ctx, acc.service("OccupancySensor", primary=True, unique_id="anyone_home"),
        sorted(e.entity_id for e in people),
    )
    result.specs.extend(ctx.accessories)


def serial_number(entry_id: str, key: str) -> str:
    """A serial number unique to one accessory of one bridge, and stable.

    It's how tools on the Home app side (e.g. HomeClaw) can find exactly this
    accessory: the Home app's own IDs never reach the accessory, and names
    change when you rename things in the Home app. A device's real serial
    number isn't used because the same device may also be in HomeKit natively.
    """
    digest = hashlib.sha1(f"{entry_id}|{key}".encode()).hexdigest()[:10].upper()
    return f"HB-{digest}"


def assign_serials(entry_id: str, specs: list[AccessorySpec]) -> None:
    for spec in specs:
        spec.info["serial_number"] = serial_number(entry_id, spec.key)


def apply_options(options: Mapping[str, Any], specs: list[AccessorySpec]) -> None:
    """Names and camera audio chosen in the options flow."""
    names = options.get(CONF_NAMES) or {}
    audio = set(options.get(CONF_CAMERA_AUDIO) or ())
    no_prebuffer = set(options.get(CONF_NO_PREBUFFER) or ())
    for spec in specs:
        if name := names.get(spec.key):
            spec.name = name
        if spec.camera is not None and spec.camera in audio:
            spec.camera_audio = True
        if spec.camera is not None and spec.camera in no_prebuffer:
            spec.camera_prebuffer = False


async def _async_run_shim(ctx: HapContext, shims: list[Shim], result: Collection) -> None:
    shim, tied = best_shims(shims, ctx)
    device = ctx.device.id if ctx.device else None
    if tied:
        names = ", ".join(t.name for t in tied)
        for t in tied:
            result.problems.append(Problem(
                t.name,
                f"matches {ctx.device.name if ctx.device else 'an entity'} as specifically "
                f"as {names}; make one HANDLES more specific (neither runs meanwhile)",
                device, shim=True, origin=t.origin,
            ))
        return
    if shim is None:
        return

    def problem(message: str) -> Problem:
        return Problem(shim.name, message, device, shim=True, origin=shim.origin)

    await _async_run(ctx, shim.source, shim.describe, shim.api, problem, result)


async def _async_run_integration(
    ctx: HapContext, domain: str, module: ModuleType, result: Collection
) -> None:
    device = ctx.device.id if ctx.device else None

    def problem(message: str) -> Problem:
        return Problem(domain, message, device)

    describe = getattr(module, "async_describe_device", None)
    if describe is None:
        result.problems.append(problem(f"{PLATFORM}.py has no async_describe_device"))
        return
    await _async_run(
        ctx, f"integration:{domain}", describe, getattr(module, "HAP_BRIDGE_API", API_VERSION),
        problem, result,
    )


async def _async_run(
    ctx: HapContext,
    source: str,
    describe: Any,
    needed_api: int,
    problem: Any,
    result: Collection,
) -> None:
    """Run a profile; on failure undo what it did and record why."""
    if needed_api > API_VERSION:
        result.problems.append(problem(
            f"needs HAP Bridge API version {needed_api}; this HAP Bridge has {API_VERSION}. "
            "Update HAP Bridge to use it."
        ))
        return
    ctx.source = source
    before_accessories, before_claimed = len(ctx.accessories), set(ctx._claimed)
    try:
        if inspect.isawaitable(outcome := describe(ctx)):
            await outcome
    except Exception as err:  # noqa: BLE001 - a broken profile falls back
        _LOGGER.exception("%s failed; using the next step instead", source)
        # Undo whatever it built, so later steps see everything.
        del ctx.accessories[before_accessories:]
        ctx._claimed = before_claimed
        result.problems.append(problem(f"{type(err).__name__}: {err}"))


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
