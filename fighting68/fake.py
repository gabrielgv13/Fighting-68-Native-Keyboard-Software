"""A simulated Fighting 68 for developing and testing with no board attached.

`FakeKeyboard` speaks the same wire protocol as the real thing: 64-byte reports,
`0xAA` host packets answered by `0x55` echoes, 56-byte chunked region transfers,
short commands at byte 2 and the `0xFB` calibration stream. It keeps every
region in memory, so a feature can be built and unit-tested offline and then
validated against the real keyboard later, unchanged:

    with open_fake() as kb:            # no hardware needed
        kb.write_bytes("rt", 0, encode_rt(trigger=1.2))

`open_fake()` returns a normal `Keyboard` wired to the simulator, so every code
path in `protocol.py`, `__main__.py` and `gui.py` runs as-is.
"""

from __future__ import annotations

from .protocol import (
    CALIBRATION_REPORT,
    MAGIC,
    PID,
    REGIONS,
    REPLY_MAGIC,
    REPORT_LEN,
    TIMEOUT_MS,
    VID,
    WRITE_COMMAND_DELTA,
    Keyboard,
    ProtocolError,
)

# read cmd -> region name, for cmds 0x10..0x18 (writes are cmd + 16).
_READ_CMDS = {cmd: name for name, (cmd, _) in REGIONS.items()}
_WRITE_CMDS = {cmd + WRITE_COMMAND_DELTA: name for name, (cmd, _) in REGIONS.items()}


class FakeKeyboard:
    """An in-memory stand-in for the vendor HID collection.

    Assign it to `Keyboard(fake)` or use `open_fake()`. What it models:

    * every region, byte for byte, seeded with the identity block (VID/PID at
      bytes 4-7) and a plausible settings default;
    * chunked reads and writes, so `read_region` / `write_region` /
      `write_bytes` round-trip exactly like on hardware;
    * short commands (`reset`, `calibrate`) which are acked and recorded;
    * the `0xFB` calibration stream: while calibration is on every `read`
      returns the next key's measurement, cycling through all 128 slots;
    * timeouts: a read with nothing to return gives back `b""`, exactly what
      hidapi does after `TIMEOUT_MS`, so `ProtocolError` paths are testable.
    """

    def __init__(self, regions: dict[str, bytes] | None = None, travel_max: dict[int, int] | None = None):
        self.regions: dict[str, bytearray] = {name: bytearray(size) for name, (_, size) in REGIONS.items()}
        self.regions["device"][4:6] = VID.to_bytes(2, "little")
        self.regions["device"][6:8] = PID.to_bytes(2, "little")
        self.regions["settings"][5] = 2  # report rate: 2 = 4 kHz
        for name, data in (regions or {}).items():
            if name not in self.regions:
                raise ValueError(f"unknown region {name!r}")
            if len(data) != len(self.regions[name]):
                raise ValueError(f"{name} region is {len(self.regions[name])} bytes, got {len(data)}")
            self.regions[name][:] = data

        # Calibration physics: deepest travel (in the wire's 0.01 mm units) per
        # key slot. Anything not listed measures a plausible 3.00-3.85 mm.
        self.travel_max: dict[int, int] = dict(travel_max or {})
        self.calibrating = False
        self.resets: list[int] = []
        self.log: list[tuple[int, int, bytes]] = []  # (cmd, offset, payload) of every request
        self.unknown: list[int] = []
        self.closed = False
        self._replies: list[bytes] = []
        self._cal_slot = 0

    # -- hid.device surface -------------------------------------------------

    def write(self, data: bytes) -> int:
        """One host report; the report id (first byte) is dropped like hidapi does."""
        if self.closed:
            raise ProtocolError("the simulated keyboard is closed")
        pkt = bytes(data[1:] if len(data) == REPORT_LEN + 1 else data)
        if len(pkt) != REPORT_LEN or pkt[0] != MAGIC:
            self.unknown.append(pkt[0] if pkt else -1)
            return len(data)
        cmd, length, offset = pkt[1], pkt[2], pkt[3] | pkt[4] << 8
        payload = pkt[8 : 8 + length]
        self.log.append((cmd, offset, payload))

        if cmd in _READ_CMDS:  # region read: stream the requested window
            body = bytes(self.regions[_READ_CMDS[cmd]][offset : offset + length])
            self._replies.append(self._reply(cmd, body))
        elif cmd in _WRITE_CMDS:  # region write: store the window, ack the chunk
            region = self.regions[_WRITE_CMDS[cmd]]
            region[offset : offset + len(payload)] = payload
            self._replies.append(self._reply(cmd, b""))
        elif cmd == 0x0F:  # reset(what)
            self.resets.append(pkt[2])
            self._replies.append(self._reply(cmd, b""))
        elif cmd == 0x64:  # calibration on
            self.calibrating, self._cal_slot = True, 0
            self._replies.append(self._reply(cmd, b""))
        elif cmd == 0x65:  # calibration off
            self.calibrating = False
            self._replies.append(self._reply(cmd, b""))
        else:  # commit (0x00) and anything else: plain ack, tracked as unknown
            self.unknown.append(cmd)
            self._replies.append(self._reply(cmd, b""))
        return len(data)

    def read(self, count: int, timeout_ms: int = TIMEOUT_MS) -> bytes:
        """Next queued reply, else the next calibration report, else a timeout."""
        if self._replies:
            return self._replies.pop(0)
        if self.calibrating:
            return self._calibration_report()
        return b""  # hidapi's timeout: an empty report, which Keyboard treats as no reply

    def close(self) -> None:
        self.closed = True

    def open_path(self, path: object = None) -> None:
        """hidapi's reopen; the simulated board keeps everything it had."""
        self.closed = False

    # -- calibration stream -------------------------------------------------

    def _travel(self, slot: int) -> int:
        return self.travel_max.get(slot, 300 + (slot % 8) * 15)

    def _calibration_report(self) -> bytes:
        """The board measures one key per report while calibration is on."""
        slot = self._cal_slot % 128
        self._cal_slot += 1
        max_travel = self._travel(slot)
        rep = bytearray(REPORT_LEN)
        rep[0] = REPLY_MAGIC
        rep[1] = CALIBRATION_REPORT
        rep[2] = slot  # key slot
        rep[3] = 1  # status: pressed
        rep[10:12] = max_travel.to_bytes(2, "little")  # live travel
        rep[12:14] = max_travel.to_bytes(2, "little")  # deepest seen
        return bytes(rep)

    # -- helpers ------------------------------------------------------------

    def _reply(self, cmd: int, body: bytes) -> bytes:
        rep = bytearray(REPORT_LEN)
        rep[0] = REPLY_MAGIC
        rep[1] = cmd
        rep[2] = min(len(body), REPORT_LEN - 8)
        rep[8 : 8 + rep[2]] = body[: rep[2]]
        return bytes(rep)

    def dump(self) -> dict[str, str]:
        """The same hex-per-region shape `dump` writes, for compare-after-write checks."""
        return {name: bytes(block).hex() for name, block in self.regions.items()}


def open_fake(regions: dict[str, bytes] | None = None, travel_max: dict[int, int] | None = None) -> Keyboard:
    """A `Keyboard` wired to a fresh simulator — the drop-in for `Keyboard.open()`."""
    fake = FakeKeyboard(regions, travel_max)
    return Keyboard(fake, product="Fighting 68 (simulated)", serial="FAKE")
