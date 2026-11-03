"""Repair issues for broken HomeKit profiles: integrations' own, and shims.

One issue per integration (or shim) per bridge, listing what went wrong; it
clears itself the next time the bridge starts without those problems. The
devices still show up in HomeKit, through the next step's mapping.
"""

from __future__ import annotations

import re

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .profiles import Problem

MAX_LISTED = 5


def _prefix(entry: ConfigEntry) -> str:
    return f"profile_{entry.entry_id}_"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")


@callback
def async_update_profile_issues(
    hass: HomeAssistant, entry: ConfigEntry, problems: list[Problem]
) -> None:
    # issue suffix -> (problem kind, name, origin, messages)
    grouped: dict[str, tuple[bool, str, str | None, list[str]]] = {}
    for problem in problems:
        suffix = f"shim_{_slug(problem.integration)}" if problem.shim else problem.integration
        _shim, _name, _origin, messages = grouped.setdefault(
            suffix, (problem.shim, problem.integration, problem.origin, [])
        )
        if problem.message not in messages:
            messages.append(problem.message)

    prefix = _prefix(entry)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id[len(prefix):] not in grouped:
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    for suffix, (shim, name, origin, messages) in grouped.items():
        listed = [f"- {m}" for m in messages[:MAX_LISTED]]
        if len(messages) > MAX_LISTED:
            listed.append(f"- …and {len(messages) - MAX_LISTED} more (see diagnostics)")
        placeholders = {"bridge": entry.title, "problems": "\n".join(listed)}
        if shim:
            placeholders |= {"shim": name, "origin": origin or name}
        else:
            placeholders["integration"] = name
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{prefix}{suffix}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="shim_error" if shim else "profile_error",
            translation_placeholders=placeholders,
        )


@callback
def async_clear_profile_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    async_update_profile_issues(hass, entry, [])
