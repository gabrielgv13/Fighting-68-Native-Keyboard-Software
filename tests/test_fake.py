"""Hardware-free tests: drive the real `Keyboard` code against the simulator,
so every protocol path can be validated without a board plugged in."""

import json
import unittest

from fighting68.fake import FakeKeyboard, open_fake
from fighting68.protocol import (
    CALIBRATION_REPORT,
    MAGIC,
    PID,
    REGIONS,
    REPLY_MAGIC,
    REPORT_LEN,
    VID,
    Keyboard,
    ProtocolError,
    chunk_plan,
    decode_calibration,
    encode_rt,
)


class FakeAsHidTest(unittest.TestCase):
    """The fake behaves like hidapi from Keyboard's point of view."""

    def test_open_fake_reports_a_plausible_identity(self):
        with open_fake() as kb:
            self.assertIsInstance(kb, Keyboard)
            self.assertIn("simulated", kb.product)
            device = kb.read_region("device")
            self.assertEqual(device[4:6], VID.to_bytes(2, "little"))
            self.assertEqual(device[6:8], PID.to_bytes(2, "little"))

    def test_every_host_packet_carries_the_magic_byte(self):
        raw = []

        class Spy(FakeKeyboard):
            def write(self, data):
                raw.append(bytes(data))
                return super().write(data)

        with Keyboard(Spy(), product="spy") as kb:
            kb.read_region("rt")
            kb.write_region("led", bytes(16))
        self.assertTrue(raw, "the driver sent no packets at all")
        for pkt in raw:
            self.assertEqual(len(pkt), REPORT_LEN + 1)  # report id + 64 bytes
            self.assertEqual(pkt[1], MAGIC, "host packets must start with 0xAA after the report id")

    def test_write_is_report_id_prefixed_and_sixty_five_bytes(self):
        seen = []

        class Spy:
            def write(self, data):
                seen.append(data)
                return len(data)

            def read(self, n, timeout_ms=0):
                return bytes(REPORT_LEN)

            def close(self):
                pass

        with Keyboard(Spy()) as kb:
            kb.command(0x0F, 0x05)
        self.assertEqual(len(seen[0]), REPORT_LEN + 1)
        self.assertEqual(seen[0][1], MAGIC)


class RegionRoundTripTest(unittest.TestCase):
    def test_read_region_streams_the_whole_block_in_chunks(self):
        seed = {name: bytes((slot * 7 + j) % 256 for j in range(size)) for slot, (name, (_, size)) in enumerate(REGIONS.items())}
        with open_fake(regions=seed) as kb:
            for name in REGIONS:
                self.assertEqual(kb.read_region(name), seed[name], f"{name} region did not round-trip")

    def test_write_region_persists_and_survives_a_reopen(self):
        data = b"".join(encode_rt(trigger=1.2, down=0.5, up=0.08) if slot == 49 else bytes(8) for slot in range(128))
        fake = FakeKeyboard()
        with Keyboard(fake, product="sim") as kb:
            kb.write_region("rt", data)
        fake.open_path()  # the "board" is still plugged in; only the handle was closed
        with Keyboard(fake, product="sim") as kb:  # same board, new connection
            self.assertEqual(kb.read_region("rt"), data)

    def test_chunked_write_sends_exactly_the_driver_plan(self):
        with open_fake() as kb:
            kb.write_region("keys", bytes(range(256)) * 2)
            writes = [(cmd, offset, payload) for cmd, offset, payload in kb.dev.log if cmd == 0x22]
            plan = chunk_plan(512)
            self.assertEqual([offset for _, offset, _ in writes], [offset for offset, _ in plan])
            self.assertEqual([len(payload) for _, _, payload in writes], [length for _, length in plan])
            self.assertEqual(b"".join(payload for _, _, payload in writes)[:512], bytes(range(256)) * 2)

    def test_write_bytes_patches_one_record(self):
        with open_fake() as kb:
            kb.write_bytes("rt", 4 * 8, encode_rt(trigger=0.9))
            got = kb.read_region("rt")
            self.assertEqual(got[4 * 8 + 2] | got[4 * 8 + 3] << 8, 90)  # 0.90 mm
            self.assertEqual(got[: 4 * 8], bytes(4 * 8))  # the rest untouched

    def test_write_of_the_wrong_size_is_rejected_before_touching_the_bus(self):
        with open_fake() as kb:
            with self.assertRaises(ValueError):
                kb.write_region("led", bytes(15))
            self.assertEqual(kb.dev.log, [])

    def test_out_of_bounds_write_bytes_is_rejected(self):
        with open_fake() as kb:
            with self.assertRaises(ValueError):
                kb.write_bytes("settings", 30, bytes(4))

    def test_dump_shape_matches_the_cli_backup(self):
        with open_fake() as kb:
            saved = kb.dev.dump()
            json.dumps(saved)  # dump must stay JSON-serialisable like `dump -o`
            self.assertEqual(set(saved), set(REGIONS))
            for name, hexdata in saved.items():
                self.assertEqual(bytes.fromhex(hexdata), kb.read_region(name))


class FailurePathTest(unittest.TestCase):
    def test_timeout_raises_the_protocol_error_the_cli_shows(self):
        class Deaf(Keyboard):
            pass

        with open_fake() as kb:
            kb.dev._replies.clear()
            kb.dev.read = lambda n, timeout_ms=0: b""  # board stops answering
            with self.assertRaises(ProtocolError):
                kb.transact(0x11, 0, length=32)

    def test_reading_after_close_is_refused(self):
        with open_fake() as kb:
            fake = kb.dev
        with self.assertRaises(ProtocolError):
            fake.write(b"\x00" + bytes(REPORT_LEN))

    def test_unrecognised_command_is_acked_but_flagged(self):
        with open_fake() as kb:
            kb.command(0xEE, 1, 2, 3, 4)
            self.assertIn(0xEE, kb.dev.unknown)


class CalibrationTest(unittest.TestCase):
    def test_calibration_streams_every_key_and_stops(self):
        with open_fake(travel_max={49: 385}) as kb:
            seen = kb.calibrate_for(0.3)
            self.assertFalse(kb.dev.calibrating)
            self.assertIn(49, seen)
            self.assertEqual(seen[49]["travel_max"], 385)
            self.assertEqual(len(seen), 128, "every slot should report at least once")

    def test_calibration_report_decodes_through_the_real_decoder(self):
        with open_fake(travel_max={0: 400}) as kb:
            kb.calibrate(True)
            report = kb.read_report(100)
            self.assertIsNotNone(report)
            self.assertEqual(report[1], CALIBRATION_REPORT)
            self.assertEqual(report[0], REPLY_MAGIC)
            live = decode_calibration(report)
            self.assertEqual(live, {"key": 0, "status": 1, "travel": 400, "travel_max": 400})
            kb.calibrate(False)

    def test_progress_callback_is_invoked(self):
        calls = []

        def progress(running):
            calls.append(len(running))

        with open_fake() as kb:
            kb.calibrate_for(0.5, on_progress=progress)
        self.assertTrue(calls, "on_progress never fired")
        self.assertGreaterEqual(calls[-1], calls[0])

    def test_discard_calibration_leaves_calibration_mode_without_saving(self):
        with open_fake() as kb:
            kb.calibrate(True)
            self.assertTrue(kb.dev.calibrating)
            kb.discard_calibration()
            self.assertFalse(kb.dev.calibrating)
            self.assertEqual(kb.dev.resets, [5])

    def test_collect_calibration_stays_in_calibration_mode_until_committed(self):
        with open_fake(travel_max={49: 385}) as kb:
            seen, interrupted = kb.collect_calibration(0.3)
            self.assertTrue(kb.dev.calibrating)  # caller still has to commit
            self.assertFalse(interrupted)
            self.assertEqual(seen[49]["travel_max"], 385)
            kb.save_calibration()
            self.assertFalse(kb.dev.calibrating)

    def test_interrupted_calibration_discards_instead_of_saving(self):
        with open_fake() as kb:
            kb.collect_calibration = lambda seconds, on_progress=None: ({}, True)
            self.assertEqual(kb.calibrate_for(1.0), {})
            self.assertFalse(kb.dev.calibrating)
            self.assertEqual(kb.dev.resets, [5])

    def test_measure_travel_reads_live_reports_without_touching_the_board(self):
        with open_fake(travel_max={49: 385}) as kb:
            kb.dev.reporting = True  # firmware streams pressed-key travel in normal mode
            seen = kb.measure_travel(0.3)
            self.assertFalse(kb.dev.calibrating)  # never entered calibration mode
            self.assertEqual(kb.dev.resets, [])
            self.assertEqual(kb.dev.log, [])  # no commands sent: read-only
            self.assertIn(49, seen)
            self.assertEqual(seen[49]["travel_max"], 385)
            self.assertEqual(seen[49]["status"], 1)


if __name__ == "__main__":
    unittest.main()
