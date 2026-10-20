"""Native driver for the VTER Fighting 68: Hall-effect sensitivity, SOCD and
calibration. Reverse engineered from the web configurator at https://vter.driveall.cn/#/."""

from __future__ import annotations

import argparse
import json
import sys

from . import layout
from .protocol import (
    CALIBRATION_REPORT,
    REGIONS,
    SOCD_MODES,
    SWITCH_TYPES,
    Keyboard,
    ProtocolError,
    decode_calibration,
    decode_key,
    decode_rt,
    decode_settings,
    encode_rt,
    encode_socd,
    patch_settings,
)

_BY_NAME = {name.lower(): code for name, code in layout.USAGE.items()}
_BY_SWITCH = {name: code for code, name in SWITCH_TYPES.items()}
_BY_MODE = {name: code for code, name in SOCD_MODES.items()}


def byte_arg(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value <= 255:
        raise argparse.ArgumentTypeError(f"{text} out of range 0..255")
    return value


def slot_arg(text: str) -> int:
    value = int(text, 0)
    if not 0 <= value < 128:
        raise argparse.ArgumentTypeError(f"{text} out of range 0..127")
    return value


def cap_arg(text: str) -> int:
    """A key by its cap label ("A", "PgUp") or by its key-map slot number."""
    for slot, name in layout.LAYOUT.items():
        if name.lower() == text.lower():
            return slot
    return slot_arg(text)


def usage_arg(text: str) -> int:
    """A key by name as the driver spells it, or by numeric HID usage id."""
    if text.lower() in _BY_NAME:
        return _BY_NAME[text.lower()]
    try:
        return int(text, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is neither a key name nor a usage id")


def mm_arg(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 655.35:
        raise argparse.ArgumentTypeError(f"{text} mm out of range 0..655.35")
    return value


def named(mapping: dict, what: str):
    def parse(text: str):
        if text.lower() in mapping:
            return mapping[text.lower()]
        raise argparse.ArgumentTypeError(f"{text!r} is not one of {sorted(mapping)}")

    parse.__name__ = what
    return parse


def open_keyboard(args: argparse.Namespace) -> Keyboard:
    return Keyboard.open(vid=args.vid, pid=args.pid)


def cmd_list(args: argparse.Namespace) -> None:
    import hid

    rows = [d for d in hid.enumerate() if d["vendor_id"] == args.vid]
    if not rows:
        print(f"no {args.vid:04x}:* devices found")
        return
    for d in rows:
        mark = " <- driver interface" if d["usage_page"] == 0xFF68 else ""
        print(f"{d['vendor_id']:04x}:{d['product_id']:04x} usage_page={d['usage_page']:04x} {d['product_string']}{mark}")


SETTINGS_HELP = {
    "report_rate": "report rate: 1 = 1 kHz, 2 = 4 kHz, 3 = 8 kHz",
    "zone_top": "top dead zone",
    "zone_bottom": "bottom dead zone",
    "stability": "stability filter",
    "rest_time": "rest time before the board sleeps",
    "auto_calibration": "adaptive dynamic calibration (0/1)",
    "waken": "wake behaviour",
}


def cmd_info(args: argparse.Namespace) -> None:
    wanted = {name: getattr(args, name) for name in SETTINGS_HELP if getattr(args, name) is not None}
    with open_keyboard(args) as kb:
        device = kb.read_region("device")
        block = patch_settings(kb.read_region("settings"), **wanted)
        if wanted:
            kb.write_region("settings", block)
            kb.commit()
        settings = decode_settings(block)
    print(f"product     {kb.product}")
    print(f"battery     {device[17]}%")
    for name, value in settings.items():
        print(f"{name:<18} {value}   ({SETTINGS_HELP[name]})")


def cmd_dump(args: argparse.Namespace) -> None:
    out = {}
    with open_keyboard(args) as kb:
        for name in REGIONS:
            out[name] = kb.read_region(name).hex()
        kb.commit()
    text = json.dumps(out, indent=1)
    if args.output:
        args.output.write(text + "\n")
        print(f"wrote {args.output.name}")
    else:
        print(text)


def cmd_restore(args: argparse.Namespace) -> None:
    saved = json.load(args.file)
    unknown = set(saved) - set(REGIONS)
    if unknown:
        raise SystemExit(f"unknown region(s) in {args.file.name}: {', '.join(sorted(unknown))}")
    with open_keyboard(args) as kb:
        for name, hexdata in saved.items():
            if name == "device":
                continue  # identity block; the driver never writes it
            kb.write_region(name, bytes.fromhex(hexdata))
            print(f"restored {name}")
        kb.commit()


def cmd_keys(args: argparse.Namespace) -> None:
    with open_keyboard(args) as kb:
        data = kb.read_region("keys")
    for slot in sorted(layout.LAYOUT):
        info = decode_key(data[4 * slot : 4 * slot + 4])
        extra = " ".join(f"{k}={v}" for k, v in info.items() if k != "type")
        print(f"{slot:>4}  {layout.LAYOUT[slot]:<10} {info['type']:<8} {extra}")


def cmd_rt(args: argparse.Namespace) -> None:
    changing = args.clear or any(getattr(args, n) is not None for n in ("switch", "flags", "trigger", "down", "up"))
    if args.cap is None:
        if changing:
            raise SystemExit("pick a key to change")
        with open_keyboard(args) as kb:
            data = kb.read_region("rt")
        for slot in range(128):
            rec = data[8 * slot : 8 * slot + 8]
            if any(rec):
                print(f"{slot:>4}  {layout.LAYOUT.get(slot, '?'):<10} {describe_rt(rec)}")
        return
    with open_keyboard(args) as kb:
        data = kb.read_region("rt")
        current = decode_rt(bytes(8)) if args.clear else decode_rt(data[8 * args.cap : 8 * args.cap + 8])
        for name in ("switch", "flags", "trigger", "down", "up"):
            if getattr(args, name) is not None:
                current[name] = getattr(args, name)
        if changing:
            kb.write_bytes("rt", 8 * args.cap, encode_rt(**current))
            kb.commit()
        print(f"{args.cap} {layout.LAYOUT.get(args.cap, '?')}: {describe_rt(encode_rt(**current))}")


def describe_rt(rec: bytes) -> str:
    rt = decode_rt(rec)
    return (
        f"trigger={rt['trigger']:.2f}mm down={rt['down']:.2f}mm up={rt['up']:.2f}mm "
        f"flags={rt['flags']} switch={SWITCH_TYPES.get(rt['switch'], rt['switch'])}"
    )


def cmd_socd(args: argparse.Namespace) -> None:
    listing = args.key1 is None and args.key2 is None
    with open_keyboard(args) as kb:
        data = kb.read_region("keys")
        if not listing:
            if (args.key1 is None) != (args.key2 is None):
                raise SystemExit("--key1 and --key2 go together")
            if args.clear and args.mode is not None:
                raise SystemExit("--mode and --clear are mutually exclusive")
            if not args.clear and args.mode is None:
                raise SystemExit("give --mode or --clear")
            slots = [layout.SLOT_BY_USAGE.get(u) for u in (args.key1, args.key2)]
            if None in slots:
                raise SystemExit(f"{[u for u, s in zip((args.key1, args.key2), slots) if s is None]} not on this keyboard")
            rec = bytes(4) if args.clear else encode_socd(args.mode, args.key1, args.key2)
            for slot in slots:
                kb.write_bytes("keys", 4 * slot, rec)
            print(f"slots {slots}: {describe_key(rec)}")
            return
    seen = set()
    for slot in range(128):
        rec = data[4 * slot : 4 * slot + 4]
        if not any(rec) or rec in seen:
            continue
        seen.add(rec)
        print(f"{slot:>4}  {layout.LAYOUT.get(slot, '?'):<10} {describe_key(rec)}")


def describe_key(rec: bytes) -> str:
    info = decode_key(rec)
    if info["type"] in ("socd", "rs"):
        keys = " ".join(f"{n}={usage_label(v)}" for n, v in info.items() if n.startswith("key"))
        return f"{info['type']} mode={SOCD_MODES.get(info.get('mode'), info.get('mode'))} {keys}"
    return json.dumps(info)


def usage_label(usage: int) -> str:
    return layout.USAGE_NAMES.get(usage, f"0x{usage:02x}")


def cmd_calibrate(args: argparse.Namespace) -> None:
    print("measuring - press each key fully, Ctrl-C to stop (read-only, nothing is written)")
    with open_keyboard(args) as kb:
        seen = kb.measure_travel(args.seconds or float("inf"))
    for slot, row in sorted(seen.items()):
        print(
            f"{slot:>4}  {layout.LAYOUT.get(slot, '?'):<10} "
            f"travel={row['travel'] / 10:.1f}mm of {row['travel_max'] / 10:.1f}mm"
        )
    print(f"{len(seen)} keys measured")


def cmd_gui(args: argparse.Namespace) -> None:
    from .gui import run

    run(args.vid, args.pid)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fighting68", description=__doc__)
    p.add_argument("--vid", type=lambda t: int(t, 0), default=0x0C45, help="USB vendor id")
    p.add_argument("--pid", type=lambda t: int(t, 0), default=0x8030, help="USB product id")
    sub = p.add_subparsers(dest="command")
    p.set_defaults(func=cmd_gui)

    sub.add_parser("list", help="show the keyboard's HID collections").set_defaults(func=cmd_list)
    sub.add_parser("keys", help="show the key map slots against the physical cap labels").set_defaults(func=cmd_keys)
    sub.add_parser("gui", help="open the Qt interface").set_defaults(func=cmd_gui)

    info = sub.add_parser("info", help="show or change the board-wide Hall-effect settings")
    for name, help_text in SETTINGS_HELP.items():
        info.add_argument(f"--{name.replace('_', '-')}", dest=name, type=byte_arg, help=help_text)
    info.set_defaults(func=cmd_info)

    dump = sub.add_parser("dump", help="read every region as hex JSON (a restorable backup)")
    dump.add_argument("-o", "--output", type=argparse.FileType("w", encoding="utf-8"))
    dump.set_defaults(func=cmd_dump)

    restore = sub.add_parser("restore", help="write a dump back to the keyboard")
    restore.add_argument("file", type=argparse.FileType("r", encoding="utf-8"))
    restore.set_defaults(func=cmd_restore)

    rt = sub.add_parser("rt", help="rapid trigger and per-key sensitivity")
    rt.add_argument("cap", nargs="?", type=cap_arg, metavar="KEY", help="cap label or key-map slot (see `keys`)")
    rt.add_argument("--trigger", type=mm_arg, metavar="MM", help="actuation point in mm")
    rt.add_argument("--down", type=mm_arg, metavar="MM", help="press sensitivity in mm")
    rt.add_argument("--up", type=mm_arg, metavar="MM", help="release sensitivity in mm")
    rt.add_argument("--flags", type=byte_arg, help="1 whole-travel, 2 bottom, 4 rampage")
    rt.add_argument("--switch", type=named(_BY_SWITCH, "switch"), help=f"one of {sorted(_BY_SWITCH)}")
    rt.add_argument("--clear", action="store_true", help="reset this key to the board defaults")
    rt.set_defaults(func=cmd_rt)

    socd = sub.add_parser("socd", help="simultaneous opposite cardinal direction resolution")
    socd.add_argument("--mode", type=named(_BY_MODE, "mode"), help=f"one of {sorted(_BY_MODE)}")
    socd.add_argument("--key1", type=usage_arg, help="first conflicting key")
    socd.add_argument("--key2", type=usage_arg, help="second conflicting key")
    socd.add_argument("--clear", action="store_true", help="remove the rule")
    socd.set_defaults(func=cmd_socd)

    cal = sub.add_parser("calibrate", help="measure each pressed key's travel without changing the board")
    cal.add_argument("--seconds", type=float, help="stop after this many seconds")
    cal.set_defaults(func=cmd_calibrate)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (ProtocolError, OSError, ValueError) as exc:
        raise SystemExit(f"error: {exc}")


if __name__ == "__main__":
    main()
