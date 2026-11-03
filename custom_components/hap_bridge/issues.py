"""Repair issues for integrations whose own HomeKit profile is broken.

One issue per integration per bridge, listing what went wrong; it clears
itself the next time the bridge starts without that integration's problems.
The device still shows up in HomeKit, through the bridge's own mapping.
"""

from __future__ import annotations

from collections import defaultdict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .profiles import Problem

MAX_LISTED = 5


def _prefix(entry: ConfigEntry) -> str:
    return f"profile_{entry.entry_id}_"


@callback
def async_update_profile_issues(
    hass: HomeAssistant, entry: ConfigEntry, problems: list[Problem]
) -> None:
    by_integration: dict[str, list[str]] = defaultdict(list)
    for problem in problems:
        if problem.message not in by_integration[problem.integration]:
            by_integration[problem.integration].append(problem.message)

    prefix = _prefix(entry)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if (
            domain == DOMAIN
            and issue_id.startswith(prefix)
            and issue_id[len(prefix):] not in by_integration
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    for integration, messages in by_integration.items():
        listed = [f"- {m}" for m in messages[:MAX_LISTED]]
        if len(messages) > MAX_LISTED:
            listed.append(f"- …and {len(messages) - MAX_LISTED} more (see diagnostics)")
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{prefix}{integration}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="profile_error",
            translation_placeholders={
                "integration": integration,
                "bridge": entry.title,
                "problems": "\n".join(listed),
            },
        )


@callback
def async_clear_profile_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    async_update_profile_issues(hass, entry, [])
