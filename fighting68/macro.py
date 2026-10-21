"""Macro store for the Fighting 68.

The `macros` region (4008 bytes) is a 100-slot store. Bytes 0..399 are one
32-bit little-endian data offset per macro; the first offset is 400. Each macro
blob starts with a 16-bit record count * 2 and then one 4-byte record per event:

    delay_ms: u16 | usage: u8 | flags: u8

flags: 0x10 always, 0x20 keyboard (clear = mouse), 0x80 press (clear = release).
`delay_ms` is the pause before the event, in milliseconds.

A timeline (the editor's model) is a list of key actions — press a key at a
time for a duration — compiled into that flat event stream. Simultaneous keys
are just events whose delays collapse to zero between them.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import layout

MACRO_REGION_SIZE = 4008
HEADER_BYTES = 400  # 100 macros, one 4-byte offset each
MAX_MACROS = HEADER_BYTES // 4
MAX_KEYS_PER_MACRO = 10
MIN_PRESS_MS = 10  # a "tap" still presses the key for 10 ms

EVENT_FLAG = 0x10  # the web driver always sets this bit
KEYBOARD_FLAG = 0x20  # keyboard key (clear = mouse)
KEYDOWN_FLAG = 0x80  # press (clear = release)


@dataclass
class MacroEvent:
    """One entry of the flat firmware stream."""

    delay_ms: int  # pause before this event (0..65535)
    usage: int  # HID usage for keyboard keys
    down: bool  # True = press, False = release
    mouse: bool = False


@dataclass
class MacroAction:
    """One key action on the timeline: press `usage` at `start_ms`, release it
    `duration_ms` later (0 means a tap)."""

    usage: int
    start_ms: int
    duration_ms: int


def macro_keys() -> dict[int, str]:
    """Correct usage -> label map for every key a macro can press.

    `layout.USAGE` is right for letters, digits and navigation but stores the
    modifier keys under fake codes (1/2/4/16/32/64); drop those and add the real
    HID modifier usages, then let the physical layout win for the on-board keys.
    """
    wrong = {"L-Ctrl", "R-Ctrl", "L-Shift", "R-Shift", "L-Alt", "R-Alt"}
    usage: dict[int, str] = {}
    for label, code in layout.USAGE.items():
        if label in wrong:
            continue
        usage.setdefault(code, label)
    usage.update(
        {
            224: "L-Ctrl",
            225: "L-Shift",
            226: "L-Alt",
            227: "L-Win",
            228: "R-Ctrl",
            229: "R-Shift",
            230: "R-Alt",
            231: "R-Win",
        }
    )
    for code, slot in layout.SLOT_BY_USAGE.items():
        if code in (224, 225, 226, 227, 228, 229, 230, 231):
            continue  # keep the L-/R- modifier labels
        usage[code] = layout.LAYOUT.get(slot, usage.get(code, f"0x{code:02x}"))
    return usage


def encode_macro(events: list[MacroEvent]) -> bytes:
    """Serialize one macro's event list to its region blob."""
    if not events:
        raise ValueError("a macro needs at least one event")
    blob = bytearray(4 + 4 * len(events))
    blob[0] = (2 * len(events)) & 0xFF
    blob[1] = (2 * len(events)) >> 8
    for i, event in enumerate(events):
        base = 4 + 4 * i
        delay = max(0, min(0xFFFF, event.delay_ms))
        blob[base] = delay & 0xFF
        blob[base + 1] = (delay >> 8) & 0xFF
        blob[base + 2] = event.usage & 0xFF
        flags = EVENT_FLAG | KEYDOWN_FLAG if event.down else EVENT_FLAG
        if not event.mouse:
            flags |= KEYBOARD_FLAG
        blob[base + 3] = flags
    return bytes(blob)


def decode_macro(region: bytes, offset: int) -> tuple[list[MacroEvent], int]:
    """Decode one macro whose blob starts at `offset`; return (events, next offset)."""
    count = (region[offset] | region[offset + 1] << 8) // 2
    events: list[MacroEvent] = []
    for i in range(count):
        base = offset + 4 + 4 * i
        delay = region[base] | region[base + 1] << 8
        usage = region[base + 2]
        flags = region[base + 3]
        events.append(
            MacroEvent(
                delay_ms=delay,
                usage=usage,
                down=bool(flags & KEYDOWN_FLAG),
                mouse=not (flags & KEYBOARD_FLAG),
            )
        )
    return events, offset + 4 + 4 * count


def encode_macros(macros: list[list[MacroEvent]]) -> bytes:
    """Serialize the whole region from a list of macros (each a list of events)."""
    if len(macros) > MAX_MACROS:
        raise ValueError(f"at most {MAX_MACROS} macros fit in the region")
    region = bytearray(MACRO_REGION_SIZE)
    offset = HEADER_BYTES
    for idx, events in enumerate(macros):
        if not events:
            raise ValueError(f"macro {idx + 1} is empty")
        region[4 * idx : 4 * idx + 4] = offset.to_bytes(4, "little")
        blob = encode_macro(events)
        end = offset + len(blob)
        if end > MACRO_REGION_SIZE:
            raise ValueError("macros exceed the 4008-byte region")
        region[offset:end] = blob
        offset = end
    return bytes(region)


def decode_macros(region: bytes) -> list[list[MacroEvent]]:
    """Decode every macro in the region, in slot order."""
    macros: list[list[MacroEvent]] = []
    for idx in range(MAX_MACROS):
        offset = int.from_bytes(region[4 * idx : 4 * idx + 4], "little")
        if offset == 0 or offset >= MACRO_REGION_SIZE:
            break
        events, _ = decode_macro(region, offset)
        macros.append(events)
    return macros


def events_to_actions(events: list[MacroEvent]) -> list[MacroAction]:
    """Reconstruct timeline actions from a flat event stream.

    Pairs each press with its release by walking absolute time; a dangling
    press/release becomes a tap. Repeat expansion is not recoverable, so a
    loaded macro always edits as one pass (repeat 1).
    """
    actions: list[MacroAction] = []
    held: dict[int, int] = {}  # usage -> press time
    now = 0
    for event in events:
        now += event.delay_ms
        if event.down:
            held.setdefault(event.usage, now)
        else:
            start = held.pop(event.usage, None)
            actions.append(MacroAction(event.usage, now if start is None else start, max(0, now - start) if start is not None else 0))
    for usage, start in held.items():  # still held when the macro ends
        actions.append(MacroAction(usage, start, 0))
    return actions


def compile_timeline(actions: list[MacroAction]) -> list[MacroEvent]:
    """Compile timed key actions into the firmware's flat event stream.

    Each action presses a key for its duration (a tap still presses for
    MIN_PRESS_MS). Events are emitted in time order — keydowns before keyups at
    the same instant — and `delay_ms` becomes the gap since the previous event.
    Repetition is a property of the *trigger* (the key binding), not the content:
    see `encode_trigger`.
    """
    keys = {action.usage for action in actions}
    if len(keys) > MAX_KEYS_PER_MACRO:
        raise ValueError(f"a macro can press at most {MAX_KEYS_PER_MACRO} keys")
    if not actions:
        raise ValueError("a macro needs at least one action")

    moments: list[tuple[int, bool, int]] = []  # (time, down, usage)
    for action in actions:
        moments.append((action.start_ms, True, action.usage))
        moments.append((action.start_ms + max(action.duration_ms, MIN_PRESS_MS), False, action.usage))
    moments.sort(key=lambda m: (m[0], not m[1]))  # downs before ups at the same time

    seq: list[MacroEvent] = []
    previous = 0
    for time, down, usage in moments:
        seq.append(MacroEvent(delay_ms=time - previous, usage=usage, down=down))
        previous = time
    return seq


# Macro trigger modes, stored in the key record that fires the macro.
TRIGGER_ROUTINE = 0  # play once per press
TRIGGER_REPEAT = 1  # play `repeat` times per press
TRIGGER_TOGGLE = 2  # keep playing until the key is pressed again


def encode_trigger(slot: int, mode: int = TRIGGER_ROUTINE, repeat: int = 0) -> bytes:
    """The 4-byte key/fn record that runs macro `slot`: [6, slot, mode, repeat]."""
    return bytes([6, slot & 0xFF, mode & 0xFF, repeat & 0xFF])


def decode_trigger(rec: bytes) -> dict | None:
    """Decode a macro trigger record, or None if `rec` is not a macro binding."""
    if len(rec) < 4 or rec[0] != 6:
        return None
    return {"slot": rec[1], "mode": rec[2], "repeat": rec[3]}
