"""Back up and restore a bridge's HomeKit identity.

What makes the Home app recognise a bridge: each unit's pairing state (its
keys and paired controllers), plus the AIDs/IIDs and ports the bridge handed
out. Restoring them into a re-added config entry brings every accessory back
with its rooms, scenes and automations, without pairing again.

A backup holds private keys: anyone with the file can impersonate the bridge
to your Home. It's written inside the config directory, which Home Assistant
backups already include.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR, Store

from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

BACKUP_VERSION = 1
BACKUP_DIR = "hap_bridge_backups"
# Stores keyed by entry ID: f"{DOMAIN}.{entry_id}.{name}"
_STORES = ("ids", "settings", "adaptive")
# Stands in for the entry ID in unit IDs, so a backup restores into a new entry.
_ENTRY = "{entry}"


def _state_path(hass: HomeAssistant, unit_id: str) -> Path:
    return Path(hass.config.path(STORAGE_DIR, f"{DOMAIN}.{unit_id}.state"))


def _unit_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    """The entry's unit IDs that have pairing state on disk."""
    pattern = re.compile(rf"^{re.escape(DOMAIN)}\.({re.escape(entry_id)}(?:_[0-9a-z]+)?)\.state$")
    storage = Path(hass.config.path(STORAGE_DIR))
    if not storage.is_dir():
        return []
    return sorted(m.group(1) for p in storage.iterdir() if (m := pattern.match(p.name)))


async def async_export(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Write a backup of `entry` to <config>/hap_bridge_backups/ and say where."""
    stores = {
        name: await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.{name}").async_load()
        for name in _STORES
    }

    def _read_units() -> dict[str, Any]:
        return {
            unit_id.replace(entry.entry_id, _ENTRY, 1): json.loads(
                _state_path(hass, unit_id).read_text()
            )
            for unit_id in _unit_ids(hass, entry.entry_id)
        }

    units = await hass.async_add_executor_job(_read_units)
    backup = {
        "version": BACKUP_VERSION,
        "title": entry.title,
        "created": datetime.now().isoformat(timespec="seconds"),
        "options": dict(entry.options),
        "stores": stores,
        "units": units,
    }
    slug = re.sub(r"[^a-z0-9]+", "_", entry.title.lower()).strip("_") or "bridge"
    name = f"{slug}_{datetime.now():%Y%m%d_%H%M%S}.json"
    path = Path(hass.config.path(BACKUP_DIR, name))

    def _write() -> None:
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(backup, indent=2))
        path.chmod(0o600)

    await hass.async_add_executor_job(_write)
    _LOGGER.info("Backed up %s (%s units) to %s", entry.title, len(units), path)
    return {"path": str(path.relative_to(hass.config.path())), "units": len(units)}


def resolve(hass: HomeAssistant, file: str) -> Path:
    """A backup file named relative to the config directory (or its backup folder)."""
    config = Path(hass.config.path()).resolve()
    for candidate in (config / file, config / BACKUP_DIR / file):
        candidate = candidate.resolve()
        if candidate.is_relative_to(config) and candidate.is_file():
            return candidate
    raise FileNotFoundError(file)


async def async_restore(hass: HomeAssistant, entry: ConfigEntry, path: Path) -> int:
    """Replace `entry`'s HomeKit identity with a backup's, then start it again."""
    backup = json.loads(await hass.async_add_executor_job(path.read_text))
    if backup.get("version") != BACKUP_VERSION or not isinstance(backup.get("units"), dict):
        raise ValueError("not a HAP Bridge backup")

    was_loaded = entry.state is ConfigEntryState.LOADED
    if was_loaded:
        await hass.config_entries.async_unload(entry.entry_id)

    for name in _STORES:
        store: Store[Any] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.{name}")
        if (data := backup["stores"].get(name)) is not None:
            await store.async_save(data)
        else:
            await store.async_remove()

    def _write_units() -> None:
        for unit_id in _unit_ids(hass, entry.entry_id):
            _state_path(hass, unit_id).unlink()
        for unit_id, state in backup["units"].items():
            if not unit_id.startswith(_ENTRY):
                raise ValueError(f"unexpected unit {unit_id!r}")
            target = _state_path(hass, unit_id.replace(_ENTRY, entry.entry_id, 1))
            target.write_text(json.dumps(state))
            target.chmod(0o600)

    await hass.async_add_executor_job(_write_units)
    hass.config_entries.async_update_entry(entry, options=backup.get("options") or {})
    if was_loaded:
        await hass.config_entries.async_setup(entry.entry_id)
    _LOGGER.info("Restored %s from %s", entry.title, path)
    return len(backup["units"])
