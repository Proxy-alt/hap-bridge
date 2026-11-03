"""Shims: HomeKit profiles for integrations you don't maintain.

A shim is written like an integration's own `hap_bridge.py` (an
`async_describe_device(ctx)`, optionally `HAP_BRIDGE_API`), plus a `HANDLES`
saying which devices it's for:

    HANDLES = {"integration": "zha", "manufacturer": "IKEA*", "model": "STARKVIND*"}

Patterns are case-insensitive shell-style (`*`, `?`). `HANDLES` may also be a
list of such dicts. Without one, a shim handles the integration it's named
after (`acme.py` -> {"integration": "acme"}).

Shims come from two places:
- files in `<config>/hap_bridge/profiles/*.py`, re-read on every bridge
  start and every preview, so edit -> preview -> reload is a short loop;
- profile packs: any integration with a `hap_bridge_shims.py` platform that
  defines `SHIMS = [{"name": ..., "handles": {...}, "describe": fn}, ...]`
  (optionally "api"), e.g. a community collection installed from HACS.

For each device the most specific matching shim runs, before the
integration's own profile: a model match beats a manufacturer match, which
beats an integration match. Whatever it leaves unclaimed goes on to the
usual steps.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .api import API_VERSION

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import HapContext

_LOGGER = logging.getLogger(__name__)

SHIM_DIR = "hap_bridge/profiles"
PACK_PLATFORM = "hap_bridge_shims"
DATA_PACKS = "hap_bridge_shim_packs"

# How specific each kind of match is; a device's best-scoring shim runs.
_WEIGHTS = {"integration": 1, "manufacturer": 2, "model": 4}


@dataclass
class Shim:
    name: str  # "acme" for a file, "<pack domain>/<name>" for a pack
    describe: Callable[[HapContext], Any]
    handles: list[dict[str, str]]
    api: int = API_VERSION
    origin: str = ""  # file path or pack domain, for messages

    @property
    def source(self) -> str:
        return f"shim:{self.name}"

    def score(self, ctx: HapContext) -> int | None:
        """How specifically this shim matches the device (None: it doesn't)."""
        best = None
        for handle in self.handles:
            if _matches(handle, ctx):
                score = sum(_WEIGHTS[key] for key in handle)
                best = score if best is None else max(best, score)
        return best


def _matches(handle: dict[str, str], ctx: HapContext) -> bool:
    def like(value: str | None, pattern: str) -> bool:
        return value is not None and fnmatchcase(value.lower(), pattern.lower())

    for key, pattern in handle.items():
        if key == "integration":
            if not any(like(e.platform, pattern) for e in ctx.entries):
                return False
        elif ctx.device is None or not like(getattr(ctx.device, key, None), pattern):
            return False
    return True


class ShimError(ValueError):
    """A shim that can't be used, with why."""


def make_shim(
    name: str, describe: Any, handles: Any, api: Any, origin: str
) -> Shim:
    if not callable(describe):
        raise ShimError("has no async_describe_device")
    if handles is None:
        handles = [{"integration": name}]
    elif isinstance(handles, dict):
        handles = [handles]
    if not isinstance(handles, list) or not handles:
        raise ShimError("HANDLES must be a dict or a non-empty list of dicts")
    for handle in handles:
        if not isinstance(handle, dict) or not handle:
            raise ShimError("each HANDLES entry must be a non-empty dict")
        if unknown := set(handle) - set(_WEIGHTS):
            raise ShimError(
                f"HANDLES can match {', '.join(_WEIGHTS)}; not {', '.join(sorted(unknown))}"
            )
        if not all(isinstance(v, str) for v in handle.values()):
            raise ShimError("HANDLES patterns must be strings")
    return Shim(name, describe, handles, int(api if api is not None else API_VERSION), origin)


@dataclass
class LoadedShims:
    shims: list[Shim]
    # (shim name, origin, why it couldn't be loaded)
    errors: list[tuple[str, str, str]]


async def async_load_shims(hass: HomeAssistant) -> LoadedShims:
    """Local shim files (read fresh) plus registered profile packs."""
    directory = Path(hass.config.path(SHIM_DIR))
    shims, errors = await hass.async_add_executor_job(_load_files, directory)
    for pack in hass.data.get(DATA_PACKS, {}).values():
        shims.extend(pack.shims)
        errors.extend(pack.errors)
    return LoadedShims(shims, errors)


def _load_files(directory: Path) -> tuple[list[Shim], list[tuple[str, str, str]]]:
    shims: list[Shim] = []
    errors: list[tuple[str, str, str]] = []
    if not directory.is_dir():
        return shims, errors
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            # A fresh module each time, so edits show up without a restart.
            spec = importlib.util.spec_from_file_location(f"hap_bridge_shim_{name}", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            shims.append(make_shim(
                name,
                getattr(module, "async_describe_device", None),
                getattr(module, "HANDLES", None),
                getattr(module, "HAP_BRIDGE_API", None),
                str(path),
            ))
        except Exception as err:  # noqa: BLE001 - one bad shim mustn't stop the rest
            _LOGGER.warning("Shim %s can't be used: %s", path, err)
            errors.append((name, str(path), f"{type(err).__name__}: {err}"))
    return shims, errors


@dataclass
class Pack:
    shims: list[Shim]
    errors: list[tuple[str, str, str]]


def read_pack(domain: str, platform: Any) -> Pack:
    """Shims from an integration's `hap_bridge_shims` platform."""
    shims: list[Shim] = []
    errors: list[tuple[str, str, str]] = []
    entries = getattr(platform, "SHIMS", None)
    if not isinstance(entries, list | tuple):
        errors.append((domain, domain, f"{PACK_PLATFORM}.py has no SHIMS list"))
        return Pack(shims, errors)
    for index, item in enumerate(entries):
        label = f"{domain}/{item.get('name', index) if isinstance(item, dict) else index}"
        try:
            if not isinstance(item, dict):
                raise ShimError("each SHIMS entry must be a dict")
            shims.append(make_shim(
                label, item.get("describe"), item.get("handles"), item.get("api"), domain
            ))
        except ShimError as err:
            errors.append((label, domain, str(err)))
    return Pack(shims, errors)


def best_shims(shims: list[Shim], ctx: HapContext) -> tuple[Shim | None, list[Shim]]:
    """The shim to run for a device, and any it tied with (then none runs)."""
    scored = [(score, shim) for shim in shims if (score := shim.score(ctx)) is not None]
    if not scored:
        return None, []
    top = max(score for score, _ in scored)
    winners = [shim for score, shim in scored if score == top]
    if len(winners) > 1:
        return None, winners
    return winners[0], []
