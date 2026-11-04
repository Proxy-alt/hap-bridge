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
                return str(value) if value is not None else None
    return None


def plan(
    wanted: Iterable[dict[str, Any]],
    home: Iterable[dict[str, Any]],
    detail: Callable[[str], dict[str, Any] | None],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """(assignments by Home app ID, wanted accessories not found in the home).

    Every accessory's serial number is read (from HomeClaw's cache, without
    polling the device): the Home app's listing doesn't include it, and an
    accessory's name or bridge name may have been changed."""
    by_serial = {item["serial"]: item for item in wanted}
    found: dict[str, str] = {}
    for accessory in home:
        if accessory.get("is_bridge"):
            continue
        uuid = accessory.get("id")
        if not uuid:
            continue
        info = detail(uuid)
        serial = serial_of(info or {})
        if serial in by_serial and serial not in found:
            found[serial] = uuid
    assignments = [
        {"uuid": found[serial], "room": item["room"]}
        for serial, item in by_serial.items() if serial in found
    ]
    missing = [item for serial, item in by_serial.items() if serial not in found]
    return assignments, missing


# -- talking to Home Assistant and HomeClaw ---------------------------------------------


def normalize_url(url: str) -> str:
    """Accept bare hosts like 192.168.1.5:8123; port 443 implies https."""
    url = url.strip().rstrip("/")
    if "://" not in url:
        scheme = "https" if url.endswith(":443") else "http"
        url = f"{scheme}://{url}"
    return url


def fetch_assignments(url: str, token: str, entry_id: str | None) -> dict[str, Any]:
    body = json.dumps({"config_entry_id": entry_id} if entry_id else {}).encode()
    request = urllib.request.Request(
        f"{normalize_url(url)}/api/services/hap_bridge/room_assignments?return_response",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - user's HA URL
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

    def run(self, *args: str, stdin: str | None = None) -> Any:
        result = subprocess.run(  # noqa: S603 - fixed binary, our arguments
            [self.cli, *args, *self.home, "--json"],
            input=stdin, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            sys.exit(f"homeclaw-cli {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
        return json.loads(result.stdout or "null")

    def accessories(self) -> list[dict[str, Any]]:
        data = self.run("list")
        return data if isinstance(data, list) else (data or {}).get("accessories", [])

    def detail(self, uuid: str) -> dict[str, Any] | None:
        # --no-refresh: serial numbers are static, no need to poll devices.
        data = self.run("get", uuid, "--no-refresh")
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
    parser.add_argument("--apply", action="store_true", help="move accessories (default: preview)")
    args = parser.parse_args()
    if not args.token:
        sys.exit("pass --token or set HA_TOKEN")

    wanted = fetch_assignments(args.url, args.token, args.bridge)
    claw = HomeClaw(find_cli(args.homeclaw), args.home)
    home = claw.accessories()
    print(f"Home Assistant: {len(wanted['accessories'])} accessories with an area; "
          f"Home app: {len(home)} accessories. Reading serial numbers…", file=sys.stderr)
    assignments, missing = plan(wanted["accessories"], home, claw.detail)

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
