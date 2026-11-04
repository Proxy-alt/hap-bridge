#!/usr/bin/env python3
"""Put HAP Bridge accessories in the Home app rooms matching their HA areas.

HomeKit has no way for an accessory to say which room it's in; rooms live
in the Home app. This uses HomeClaw (https://github.com/omarshahine/HomeClaw),
a Mac app with HomeKit access, to move them:

1. ask Home Assistant where each accessory belongs (the
   `hap_bridge.room_assignments` action: serial number -> area);
2. list the home with `homeclaw-cli` and read each accessory's serial
   number, which HAP Bridge makes unique per accessory, so matches survive
   renames in the Home app;
3. `homeclaw-cli assign-rooms` by the Home app's own IDs. Missing rooms are
   created; accessories already in the right room are left alone.

It previews by default; pass --apply to move things.

    export HA_TOKEN=...   # a long-lived access token (your HA profile page)
    python3 scripts/assign_rooms.py --url http://homeassistant.local:8123
    python3 scripts/assign_rooms.py --url http://homeassistant.local:8123 --apply

HomeClaw must be running on this Mac and signed in to your home.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
from typing import Any
import urllib.request

SERIAL_TYPE = "00000030-0000-1000-8000-0026BB765291"
APP_CLI = "/Applications/HomeClaw.app/Contents/MacOS/homeclaw-cli"


# -- pure logic (tested) -------------------------------------------------------------


def serial_of(detail: dict[str, Any]) -> str | None:
    """The Serial Number characteristic from `homeclaw-cli get --json` output."""
    for service in detail.get("services") or []:
        for char in service.get("characteristics") or []:
            kind = str(char.get("type") or "").upper()
            name = str(char.get("name") or "").lower().replace(" ", "_")
            if kind == SERIAL_TYPE or name in ("serial_number", "serialnumber"):
                value = char.get("value")
                # HomeClaw shows "--" for a value it has never read.
                return str(value) if value not in (None, "", "--") else None
    return None


def plan(
    wanted: Iterable[dict[str, Any]],
    home: Iterable[dict[str, Any]],
    detail: Callable[[str], dict[str, Any] | None],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(assignments by Home app ID, wanted accessories not found in the home,
    wanted accessories matched by name).

    Every accessory's serial number is read: the Home app's listing doesn't
    include it, and an accessory's name or bridge name may have been changed.
    When HomeClaw has never read a bridged accessory's serial number (it was
    unreachable), it is matched by name instead, but only if exactly one such
    accessory and one wanted accessory have that name."""
    by_serial = {item["serial"]: item for item in wanted}
    found: dict[str, str] = {}
    unknown: list[dict[str, Any]] = []  # bridged accessories with no serial number
    for accessory in home:
        if accessory.get("is_bridge"):
            continue
        uuid = accessory.get("id")
        if not uuid:
            continue
        serial = serial_of(detail(uuid) or {})
        if serial in by_serial and serial not in found:
            found[serial] = uuid
        elif serial is None and accessory.get("bridge"):
            unknown.append(accessory)

    def counts(names: Iterable[str]) -> dict[str, int]:
        result: dict[str, int] = {}
        for name in names:
            result[name] = result.get(name, 0) + 1
        return result

    left = [item for serial, item in by_serial.items() if serial not in found]
    left_names = counts(item["name"] for item in left)
    home_names = counts(a.get("name") or "" for a in unknown)
    by_name = {a.get("name"): a["id"] for a in unknown}
    named = [item for item in left
             if left_names[item["name"]] == 1 and home_names.get(item["name"]) == 1]
    for item in named:
        found[item["serial"]] = by_name[item["name"]]

    assignments = [
        {"uuid": found[serial], "room": item["room"]}
        for serial, item in by_serial.items() if serial in found
    ]
    missing = [item for serial, item in by_serial.items() if serial not in found]
    return assignments, missing, named


# -- talking to Home Assistant and HomeClaw ---------------------------------------------


def normalize_url(url: str) -> str:
    """Accept bare hosts like 192.168.1.5:8123; port 443 implies https."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        scheme = "https" if url.endswith(":443") else "http"
        url = f"{scheme}://{url}"
    return url


def fetch_assignments(
    url: str, token: str, entry_id: str | None, verify_tls: bool = True,
) -> dict[str, Any]:
    body = json.dumps({"config_entry_id": entry_id} if entry_id else {}).encode()
    request = urllib.request.Request(
        f"{normalize_url(url)}/api/services/hap_bridge/room_assignments?return_response",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    context = None if verify_tls else ssl._create_unverified_context()  # noqa: S323 - opt-in flag
    with urllib.request.urlopen(request, timeout=30, context=context) as response:  # noqa: S310 - user's HA URL
        return json.load(response)["service_response"]


def find_cli(explicit: str | None) -> str:
    for candidate in (explicit, shutil.which("homeclaw-cli"), APP_CLI):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit("homeclaw-cli not found: install HomeClaw, or pass --homeclaw PATH")


class HomeClaw:
    def __init__(self, cli: str, home: str | None) -> None:
        self.cli = cli
        self.home = ["--home", home] if home else []

    def run(self, *args: str, stdin: str | None = None, check: bool = True) -> Any:
        result = subprocess.run(  # noqa: S603 - fixed binary, our arguments
            [self.cli, *args, *self.home, "--json"],
            input=stdin, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 and not check:
            return None
        if result.returncode != 0:
            sys.exit(f"homeclaw-cli {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
        return json.loads(result.stdout or "null")

    def accessories(self) -> list[dict[str, Any]]:
        data = self.run("list")
        return data if isinstance(data, list) else (data or {}).get("accessories", [])

    def detail(self, uuid: str) -> dict[str, Any] | None:
        # --no-refresh first: serial numbers are static, no need to poll devices.
        # HomeClaw may never have read one, though; then ask the device (this
        # fails for unreachable ones, which plan() matches by name).
        data = self.run("get", uuid, "--no-refresh")
        if isinstance(data, dict) and serial_of(data) is None:
            fresh = self.run("get", uuid, check=False)
            if isinstance(fresh, dict):
                data = fresh
        return data if isinstance(data, dict) else None

    def assign(self, assignments: list[dict[str, str]], apply: bool) -> dict[str, Any]:
        args = ["assign-rooms", "-"] + ([] if apply else ["--dry-run"])
        return self.run(*args, stdin=json.dumps(assignments))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=os.environ.get("HA_URL", "http://homeassistant.local:8123"))
    parser.add_argument("--token", default=os.environ.get("HA_TOKEN"),
                        help="long-lived access token (default: $HA_TOKEN)")
    parser.add_argument("--bridge", help="HAP Bridge config entry ID (default: the first)")
    parser.add_argument("--home", help="Home app home name or ID (default: the primary home)")
    parser.add_argument("--homeclaw", help="path to homeclaw-cli")
    parser.add_argument("--no-verify-tls", action="store_true",
                        help="skip HTTPS certificate checks (self-signed or IP-addressed HA)")
    parser.add_argument("--apply", action="store_true", help="move accessories (default: preview)")
    args = parser.parse_args()
    if not args.token:
        sys.exit("pass --token or set HA_TOKEN")

    wanted = fetch_assignments(args.url, args.token, args.bridge,
                               verify_tls=not args.no_verify_tls)
    claw = HomeClaw(find_cli(args.homeclaw), args.home)
    home = claw.accessories()
    print(f"Home Assistant: {len(wanted['accessories'])} accessories with an area; "
          f"Home app: {len(home)} accessories. Reading serial numbers…", file=sys.stderr)
    assignments, missing, named = plan(wanted["accessories"], home, claw.detail)

    for item in named:
        print(f"  matched by name (serial number unreadable): {item['name']}", file=sys.stderr)

    for item in missing:
        print(f"  not in the Home app: {item['name']} ({item['serial']})", file=sys.stderr)
    for name in wanted.get("without_area", []):
        print(f"  no area in Home Assistant: {name}", file=sys.stderr)
    if not assignments:
        print("Nothing to move.")
        return
    result = claw.assign(assignments, args.apply)
    for line in result.get("details", []):
        print(f"  {line.get('status')}: {line.get('accessory')} -> {line.get('room')}")
    verb = "Moved" if args.apply else "Would move"
    print(f"{verb} {result.get('assigned', 0)}; already in place or skipped "
          f"{result.get('skipped', 0)}.")
    if not args.apply:
        print("Preview only: run again with --apply to move them.")


if __name__ == "__main__":
    main()
