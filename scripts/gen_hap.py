"""Generate custom_components/hap_bridge/hap.py from HAP-python's catalogue.

    python scripts/gen_hap.py          # rewrite hap.py
    python scripts/gen_hap.py --check  # exit 1 if hap.py is out of date
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

from pyhap.loader import get_loader

TARGET = Path(__file__).resolve().parents[1] / "custom_components" / "hap_bridge" / "hap.py"


def constant(name: str) -> str:
    """'AirPurifier' -> 'AIR_PURIFIER', 'PM2.5Density' -> 'PM2_5_DENSITY'."""
    name = re.sub(r"[^0-9A-Za-z]+", "_", name)
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    name = re.sub(r"_+", "_", name).strip("_").upper()
    return f"_{name}" if name[0].isdigit() else name


def class_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "_", name)


def _add_hksv_definitions(loader) -> None:
    """recording.py's HomeKit Secure Video definitions, which HAP-python lacks.

    Loaded from the file (not imported as a package) so this runs without
    Home Assistant installed."""
    import importlib.util
    import types

    path = TARGET.parent / "recording.py"
    if "_hap_bridge_gen" not in sys.modules:
        package = types.ModuleType("_hap_bridge_gen")
        package.__path__ = [str(TARGET.parent)]
        sys.modules["_hap_bridge_gen"] = package
    spec = importlib.util.spec_from_file_location("_hap_bridge_gen.recording", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.register_definitions(loader)


def render() -> str:
    loader = get_loader()
    _add_hksv_definitions(loader)
    lines = [
        '"""HomeKit service, characteristic and value names, so profiles needn\'t',
        "spell raw HAP strings or remember what 0 and 1 mean.",
        "",
        "Generated from HAP-python's catalogue by scripts/gen_hap.py; don't edit.",
        "Profiles get this module as `ctx.hap` without importing it:",
        "",
        "    svc = acc.service(ctx.hap.Service.AIR_PURIFIER, primary=True)",
        "    svc.char(ctx.hap.Char.ACTIVE, get=lambda: ctx.hap.Active.ACTIVE)",
        '"""',
        "",
        "from enum import IntEnum",
        "from typing import Final",
        "",
        "",
        "class Service:",
        '    """Service names, for `acc.service(...)`."""',
        "",
    ]
    lines += [f'    {constant(n)}: Final = "{n}"' for n in sorted(loader.serv_types)]
    lines += ["", "", "class Char:", '    """Characteristic names, for `svc.char(...)`."""', ""]
    lines += [f'    {constant(n)}: Final = "{n}"' for n in sorted(loader.char_types)]
    for name in sorted(loader.char_types):
        values = loader.char_types[name].get("ValidValues")
        if not values:
            continue
        lines += ["", "", f"class {class_name(name)}(IntEnum):",
                  f'    """Values of {name}."""', ""]
        for label, value in sorted(values.items(), key=lambda kv: (kv[1], kv[0])):
            lines.append(f"    {constant(label)} = {value}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if TARGET.read_text() == text else 1)
    TARGET.write_text(text)
