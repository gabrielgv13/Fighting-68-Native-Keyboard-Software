"""HID wire protocol of the VTER "Fighting 68" keyboard.

Recovered from the WebHID driver at https://vter.driveall.cn/#/ (bundle
`static/js/app.94fa93896fc63cff684f.js`, minified transport object `Ra`).
Region packets are the same in both directions:

    0xAA <cmd> <len> <offset lo> <offset hi> 0 0 0 <payload at byte 8>

`len` is the number of payload bytes in this packet and `offset` is the byte
offset inside the region named by `cmd`. A host packet (magic 0xAA) is answered
by one device packet (magic 0x55) with the same cmd; transfers larger than 56
payload bytes are split into chunks and each chunk is sent only after the
previous one is acknowledged.
"""

from __future__ import annotations

import time

import hid

MAGIC = 0xAA
REPLY_MAGIC = 0x55  # the device answers 0xAA requests with 0x55
REPORT_LEN = 64  # collections on usage page 0xFF68 use 64-byte reports
CHUNK = REPORT_LEN - 8
TIMEOUT_MS = 2000

VID = 0x0C45  # Sonix
PID = 0x8030  # Fighting68 (0x8051 is the Hall-effect/axial variant)
USAGE_PAGE = 0xFF68

# region -> (read command, size). Write command is read command + 16.
REGIONS = {
    "device": (0x10, 24),
    "settings": (0x11, 32),
    "keys": (0x12, 512),
    "led": (0x13, 16),
    "colors": (0x14, 512),
    "macros": (0x15, 4008),
    "fn": (0x16, 512),
    "rt": (0x17, 1024),
    "dks": (0x18, 1024),
}
WRITE_COMMAND_DELTA = 16


class ProtocolError(RuntimeError):
    pass


def frame(cmd: int, offset: int, payload: bytes = b"", length: int | None = None) -> bytes:
    """Build one region packet. `length` may exceed the payload; the rest is zero fill."""
    if length is None:
        length = len(payload)
    if not 0 <= length <= CHUNK:
        raise ValueError(f"length {length} out of range 0..{CHUNK}")
    if len(payload) > length:
        raise ValueError(f"payload of {len(payload)} bytes exceeds declared length {length}")
    if not 0 <= offset <= 0xFFFF:
        raise ValueError(f"offset {offset} does not fit in 16 bits")
    pkt = bytearray(REPORT_LEN)
    pkt[0] = MAGIC
    pkt[1] = cmd
    pkt[2] = length
    pkt[3] = offset & 0xFF
    pkt[4] = (offset >> 8) & 0xFF
    pkt[8 : 8 + len(payload)] = payload
    return bytes(pkt)


def chunk_plan(size: int) -> list[tuple[int, int]]:
    """Split a region into (offset, length) packets as the driver does: 56 bytes
    per packet, and the tail packet keeps the full length unless the transfer has
    more than one packet."""
    count, rest = divmod(size, CHUNK)
    if rest:
        count += 1
    plan = []
    for i, offset in enumerate(range(0, size, CHUNK)):
        length = rest if (i == count - 1 and i != 0 and rest) else CHUNK
        plan.append((offset, length))
    return plan


def encode_key(usage: int = 0, *, special: int = 0, media: int = 0, mouse: tuple[int, int] | None = None) -> bytes:
    """One 4-byte key-map record (driver: `getUpdateKeyData`)."""
    rec = [2, 0, 0, 0]
    if media:
        rec[0], rec[1], rec[2] = 3, media & 0xFF, (media >> 8) & 0xFF
    elif mouse:
        rec[0], rec[1], rec[2] = 1, mouse[0], mouse[1]
    elif special:
        rec[1] = special
    else:
        rec[2] = usage
    return bytes(rec)


EMPTY_KEY = b"\x00\x00\x00\x00"  # the record the driver writes to unbind a slot


def decode_key(rec: bytes) -> dict:
    """Inverse of `encode_key` plus the advanced key types (driver: `getInityKeyValue`)."""
    kind, b1, b2, b3 = rec[0], rec[1], rec[2], rec[3]
    if kind == 0:
        return {"type": "empty"}
    if kind == 1:
        return {"type": "mouse", "kind": b1, "code": b2}
    if kind == 2:
        return {"type": "key", "special": b1, "usage": b1 or b2}
    if kind == 3:
        return {"type": "media", "code": b1 | b2 << 8}
    if kind == 6:
        return {"type": "macro", "slot": b1}
    if kind == 8:
        return {"type": "dks", "slot": b1}
    if kind == 9:
        return {"type": "mt", "tap": b1, "hold": b2, "time_ms": b3 * 10}
    if kind == 10:
        return {"type": "tgl", "value": b1}
    if kind == 11:
        return {"type": "socd", "mode": b1, "key1": b2, "key2": b3}
    if kind == 12:
        return {"type": "rs", "key1": b2, "key2": b3}
    return {"type": f"unknown({kind})", "raw": rec.hex()}


# Rapid trigger, one 8-byte record per key slot (driver: `getSenRt`).
# All three travel values are stored in 0.01 mm; flags come from the driver's
# `whole` / `bottom` / `isRtMode` toggles.
RT_WHOLE = 1  # rapid trigger stays active across the whole travel
RT_BOTTOM = 2  # bottoming out counts as an actuation
RT_RAMPAGE = 4  # "Rampage Mode": extreme precision

# Switch (axis) types a Hall-effect key can be calibrated against.
SWITCH_TYPES = {0: "stock", 1: "aurora", 2: "jade", 3: "king"}

# SOCD behaviour stored in a key record of type 11 (driver: `actionSocd`).
SOCD_MODES = {1: "key1", 2: "key2", 3: "last", 4: "cancel"}


def encode_rt(*, switch: int = 0, flags: int = 0, trigger: float = 0.0, down: float = 0.0, up: float = 0.0) -> bytes:
    """One rapid-trigger record. `trigger`, `down` and `up` are travel in mm."""
    rec = bytearray(8)
    rec[0] = switch
    rec[1] = flags
    for slot, value in ((2, trigger), (4, down), (6, up)):
        mm = round(value * 100)
        if not 0 <= mm <= 0xFFFF:
            raise ValueError(f"{value} mm out of range 0..655.35")
        rec[slot : slot + 2] = mm.to_bytes(2, "little")
    return bytes(rec)


def decode_rt(rec: bytes) -> dict:
    return {
        "switch": rec[0],
        "flags": rec[1],
        "trigger": (rec[2] | rec[3] << 8) / 100,
        "down": (rec[4] | rec[5] << 8) / 100,
        "up": (rec[6] | rec[7] << 8) / 100,
    }


def encode_socd(mode: int, key1: int, key2: int) -> bytes:
    """A SOCD rule lives in a key-map record of type 11 (driver: `getSendSocd`)."""
    if mode not in SOCD_MODES:
        raise ValueError(f"SOCD mode must be one of {sorted(SOCD_MODES)}")
    return bytes([11, mode, key1, key2])


# Board-wide settings block: field -> byte index (driver: `handleClickReport`).
SETTINGS_FIELDS = {
    "rest_time": 3,
    "report_rate": 5,
    "zone_top": 8,
    "zone_bottom": 9,
    "stability": 11,
    "auto_calibration": 14,
    "waken": 15,
}


def decode_settings(rec: bytes) -> dict:
    return {name: rec[index] for name, index in SETTINGS_FIELDS.items()}


def patch_settings(block: bytes, **fields: int) -> bytes:
    """The settings block with the given fields replaced; the rest is untouched."""
    out = bytearray(block)
    for name, value in fields.items():
        if name not in SETTINGS_FIELDS:
            raise ValueError(f"unknown setting {name!r}")
        if not 0 <= value <= 255:
            raise ValueError(f"{name} {value} out of range 0..255")
        out[SETTINGS_FIELDS[name]] = value
    return bytes(out)


CALIBRATION_REPORT = 0xFB


def decode_calibration(rec: bytes) -> dict:
    """While calibrating the board reports the key slot and its state; otherwise the
    live travel of the pressed key (driver: `window.getCalibration`). Travel fields
    are hundredths of a millimeter, matching the web driver's travel ratio math."""
    if len(rec) < 14 or rec[0] != REPLY_MAGIC or rec[1] != CALIBRATION_REPORT:
        raise ProtocolError("invalid calibration report")
    if rec[2] >= 128:
        raise ProtocolError(f"invalid calibration key slot {rec[2]}")
    return {
        "key": rec[2],
        "status": rec[3],
        "travel": rec[10] | rec[11] << 8,
        "travel_max": rec[12] | rec[13] << 8,
    }


class Keyboard:
    """One open connection to the vendor HID collection."""

    def __init__(self, dev: hid.device, product: str = "", serial: str = ""):
        self.dev = dev
        self.product = product
        self.serial = serial

    @classmethod
    def open(cls, vid: int = VID, pid: int = PID, usage_page: int = USAGE_PAGE) -> "Keyboard":
        matches = [d for d in hid.enumerate(vid, pid) if d["usage_page"] == usage_page]
        if not matches:
            raise ProtocolError(
                f"no HID collection on usage page {usage_page:#06x} for {vid:04x}:{pid:04x} - is the keyboard plugged in?"
            )
        if len(matches) > 1:
            raise ProtocolError(f"{len(matches)} matching collections; unplug the extras")
        dev = hid.device()
        dev.open_path(matches[0]["path"])
        return cls(dev, matches[0]["product_string"] or "", matches[0]["serial_number"] or "")

    def close(self) -> None:
        self.dev.close()

    def __enter__(self) -> "Keyboard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def transact(self, cmd: int, offset: int, payload: bytes = b"", length: int | None = None) -> bytes:
        """Send one region packet and return the reply that echoes `cmd`."""
        self.dev.write(b"\x00" + frame(cmd, offset, payload, length))
        for _ in range(8):
            reply = bytes(self.dev.read(REPORT_LEN, TIMEOUT_MS))
            if not reply:
                raise ProtocolError(f"timeout waiting for reply to cmd {cmd:#04x}")
            if reply[0] == REPLY_MAGIC and reply[1] == cmd:
                return reply
        raise ProtocolError(f"no reply carrying cmd {cmd:#04x}")

    def command(self, cmd: int, *args: int) -> None:
        """Short command packet: its arguments sit at byte 2, not in the payload slot."""
        pkt = bytearray(REPORT_LEN)
        pkt[0] = MAGIC
        pkt[1] = cmd
        pkt[2 : 2 + len(args)] = args
        self.dev.write(b"\x00" + bytes(pkt))
        self.dev.read(REPORT_LEN, 500)  # the device acknowledges; the driver ignores the body

    def read_region(self, name: str) -> bytes:
        cmd, size = REGIONS[name]
        out = bytearray(size)
        for offset, length in chunk_plan(size):
            reply = self.transact(cmd, offset, length=length)
            got = min(reply[2], size - offset)
            out[offset : offset + got] = reply[8 : 8 + got]
        return bytes(out)

    def write_region(self, name: str, data: bytes) -> None:
        cmd, size = REGIONS[name]
        if len(data) != size:
            raise ValueError(f"{name} region is {size} bytes, got {len(data)}")
        for offset, length in chunk_plan(size):
            self.transact(cmd + WRITE_COMMAND_DELTA, offset, data[offset : offset + length], length)

    def write_bytes(self, name: str, offset: int, data: bytes) -> None:
        """Write part of a region (the driver's `buildPkt_Key` style single record)."""
        cmd, size = REGIONS[name]
        if not (0 <= offset and offset + len(data) <= size):
            raise ValueError(f"write of {len(data)} bytes at offset {offset} falls outside the {name} region")
        self.transact(cmd + WRITE_COMMAND_DELTA, offset, data)

    def reset(self, what: int = 0xFF) -> None:
        """Factory reset. 0xFF resets everything, 0x05 the key map (driver: `sendReportCmd`)."""
        self.command(0x0F, what, 0, 0, 0)

    def calibrate(self, on: bool) -> None:
        """Enter (0x64) or save-and-leave (0x65) calibration mode — the web
        driver's `getSendReportCalibration` / "Save Calibration". While on, the
        board streams CALIBRATION_REPORT."""
        self.command(0x64 if on else 0x65, 0, 0, 0, 0)

    def save_calibration(self) -> None:
        """Leave calibration mode and commit what the firmware measured."""
        self.calibrate(False)

    def discard_calibration(self) -> None:
        """Send the web driver's Clear Calibration command (0x0F/5).

        This clears calibration and must not be used for a Cancel action that
        is meant to preserve the saved values.
        """
        self.command(0x0F, 5, 0, 0, 0)

    def read_report(self, timeout_ms: int = TIMEOUT_MS) -> bytes | None:
        """Next unsolicited report, or None if none arrives in time."""
        return bytes(self.dev.read(REPORT_LEN, timeout_ms)) or None

    def measure_travel(self, seconds: float, on_progress=None) -> dict[int, dict]:
        """Read each pressed key's travel WITHOUT entering the board's
        calibration mode. In normal mode the firmware still streams 0xFB reports
        for pressed keys carrying the slot, the live travel and the switch's full
        travel, so nothing is written to the board. Returns per key {status,
        travel, travel_max}. The latest valid raw sample is retained; values are
        not smoothed or capped."""
        seen: dict[int, dict] = {}
        deadline = time.monotonic() + seconds
        last = 0.0
        try:
            while time.monotonic() < deadline:
                report = self.read_report(500)
                if not report or len(report) < 2 or report[1] != CALIBRATION_REPORT:
                    continue
                try:
                    live = decode_calibration(report)
                except ProtocolError:
                    continue
                if live["travel"] <= 0:
                    continue  # resting key: not actually pressed
                if live["travel_max"] > 0 and live["travel"] > live["travel_max"]:
                    continue  # impossible sample; preserve the last valid reading
                row = seen.setdefault(live["key"], {"status": 1, "travel": 0, "travel_max": 0})
                row["travel"] = live["travel"]
                row["travel_max"] = live["travel_max"]
                if on_progress and time.monotonic() - last > 0.2:
                    last = time.monotonic()
                    on_progress(dict(seen))
        except KeyboardInterrupt:
            pass
        return seen

    def calibration_session(self, should_stop, on_progress=None) -> tuple[dict[int, dict], str]:
        """Stream until ``should_stop()`` returns ``save`` or ``cancel``.

        Cancel deliberately sends no command: the only observed web command for
        clearing calibration is 0x0F/5, which can erase saved values. The caller
        releases the HID connection after cancel.
        """
        seen: dict[int, dict] = {}
        last = 0.0
        self.calibrate(True)
        while True:
            action = should_stop()
            if action:
                return seen, action
            report = self.read_report(100)
            if not report or len(report) < 2 or report[1] != CALIBRATION_REPORT:
                continue
            try:
                live = decode_calibration(report)
            except ProtocolError:
                continue
            if not live["status"] or live["travel"] <= 0 or live["travel_max"] <= 0:
                continue  # don't expose untouched keys as zero-travel calibrations
            if live["travel"] > live["travel_max"]:
                continue  # impossible sample; preserve the last valid reading
            row = seen.setdefault(live["key"], {"status": 1, "travel": 0, "travel_max": 0})
            row["travel"] = live["travel"]
            row["travel_max"] = live["travel_max"]
            if on_progress and time.monotonic() - last > 0.2:
                last = time.monotonic()
                on_progress(dict(seen))

    def collect_calibration(self, seconds: float, on_progress=None) -> tuple[dict[int, dict], bool]:
        """Collect reports for a fixed interval, leaving the session open.

        The caller can then save. An interrupted session sends no clear command;
        close the HID connection to release the device without erasing saved data.
        """
        seen: dict[int, dict] = {}
        deadline = time.monotonic() + seconds
        last = 0.0
        interrupted = False
        self.calibrate(True)
        try:
            while time.monotonic() < deadline:
                report = self.read_report(100)
                if not report or len(report) < 2 or report[1] != CALIBRATION_REPORT:
                    continue
                try:
                    live = decode_calibration(report)
                except ProtocolError:
                    continue
                if not live["status"] or live["travel"] <= 0 or live["travel_max"] <= 0:
                    continue
                if live["travel"] > live["travel_max"]:
                    continue
                row = seen.setdefault(live["key"], {"status": 1, "travel": 0, "travel_max": 0})
                row["travel"] = live["travel"]
                row["travel_max"] = live["travel_max"]
                if on_progress and time.monotonic() - last > 0.2:
                    last = time.monotonic()
                    on_progress(dict(seen))
        except KeyboardInterrupt:
            interrupted = True
        return seen, interrupted

    def calibrate_for(self, seconds: float, on_progress=None) -> dict[int, dict]:
        """Compatibility helper for a timed capture; commits only successful keys."""
        seen, interrupted = self.collect_calibration(seconds, on_progress)
        if not interrupted and any(
            row["status"] and row["travel"] > 0 and row["travel_max"] > 0
            for row in seen.values()
        ):
            self.save_calibration()
        return seen

    def commit(self) -> None:
        """Trailing cmd-0 packet the driver sends after a bulk read or an RT write."""
        self.transact(0x00, 0, b"\x00\x00\x00\x00", CHUNK)
