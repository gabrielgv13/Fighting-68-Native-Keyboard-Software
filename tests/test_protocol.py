import unittest

from fighting68.protocol import (
    CHUNK,
    EMPTY_KEY,
    RT_RAMPAGE,
    RT_WHOLE,
    SETTINGS_FIELDS,
    SOCD_MODES,
    chunk_plan,
    decode_calibration,
    decode_key,
    decode_rt,
    decode_settings,
    encode_key,
    encode_rt,
    encode_socd,
    frame,
    patch_settings,
)


class FrameTest(unittest.TestCase):
    def test_header_layout(self):
        pkt = frame(0x22, 0x0102, b"\x01\x02\x03\x04")
        self.assertEqual(pkt[0], 0xAA)
        self.assertEqual(pkt[1], 0x22)
        self.assertEqual(pkt[2], 4)
        self.assertEqual(pkt[3:5], b"\x02\x01")
        self.assertEqual(pkt[8:12], b"\x01\x02\x03\x04")
        self.assertEqual(len(pkt), 64)

    def test_declared_length_zero_fills(self):
        pkt = frame(0x11, 0, b"\x07", length=CHUNK)
        self.assertEqual(pkt[2], CHUNK)
        self.assertEqual(pkt[8], 7)
        self.assertEqual(pkt[9:], bytes(CHUNK - 1))

    def test_payload_longer_than_declared_length_is_rejected(self):
        with self.assertRaises(ValueError):
            frame(0x11, 0, b"\x00" * 9, length=8)


class ChunkPlanTest(unittest.TestCase):
    def test_matches_the_drivers_read_pattern(self):
        self.assertEqual(chunk_plan(512), [(i * CHUNK, 8 if i == 9 else CHUNK) for i in range(10)])
        self.assertEqual(chunk_plan(1024), [(i * CHUNK, 16 if i == 18 else CHUNK) for i in range(19)])

    def test_single_packet_region_keeps_the_full_length(self):
        # The driver sends a 56-byte length even for its 24-byte device block.
        self.assertEqual(chunk_plan(24), [(0, CHUNK)])

    def test_every_byte_is_covered_once(self):
        for size in (16, 24, 32, 512, 1024, 4008):
            plan = chunk_plan(size)
            self.assertEqual(plan[0][0], 0)
            self.assertGreaterEqual(plan[-1][0] + CHUNK, size)
            for (off_a, _), (off_b, _) in zip(plan, plan[1:]):
                self.assertEqual(off_b - off_a, CHUNK)


class KeyRecordTest(unittest.TestCase):
    def test_plain_key(self):
        self.assertEqual(encode_key(0x29), b"\x02\x00\x29\x00")
        self.assertEqual(decode_key(b"\x02\x00\x29\x00"), {"type": "key", "special": 0, "usage": 0x29})

    def test_special_key_lives_in_byte_one(self):
        self.assertEqual(encode_key(special=0xE0), b"\x02\xe0\x00\x00")
        self.assertEqual(decode_key(b"\x02\xe0\x00\x00")["usage"], 0xE0)

    def test_media_key_is_little_endian(self):
        self.assertEqual(encode_key(media=0x0183), b"\x03\x83\x01\x00")
        self.assertEqual(decode_key(b"\x03\x83\x01\x00"), {"type": "media", "code": 0x0183})

    def test_clear(self):
        self.assertEqual(EMPTY_KEY, b"\x00\x00\x00\x00")
        self.assertEqual(encode_key(0), b"\x02\x00\x00\x00")

    def test_advanced_types_round_trip(self):
        for rec, expected in [
            (b"\x01\x01\x02\x00", {"type": "mouse", "kind": 1, "code": 2}),
            (b"\x06\x05\x00\x00", {"type": "macro", "slot": 5}),
            (b"\x08\x03\x00\x00", {"type": "dks", "slot": 3}),
            (b"\x09\x04\x05\x0a", {"type": "mt", "tap": 4, "hold": 5, "time_ms": 100}),
            (b"\x0a\x02\x00\x00", {"type": "tgl", "value": 2}),
            (b"\x0b\x01\x2c\x2d", {"type": "socd", "mode": 1, "key1": 0x2C, "key2": 0x2D}),
            (b"\x0c\x00\x2c\x2d", {"type": "rs", "key1": 0x2C, "key2": 0x2D}),
        ]:
            self.assertEqual(decode_key(rec), expected, rec.hex())


class RapidTriggerTest(unittest.TestCase):
    def test_travel_is_stored_in_hundredths_of_a_mm(self):
        rec = encode_rt(switch=2, flags=RT_WHOLE | RT_RAMPAGE, trigger=1.2, down=0.5, up=0.08)
        self.assertEqual(rec, b"\x02\x05\x78\x00\x32\x00\x08\x00")
        self.assertEqual(
            decode_rt(rec),
            {"switch": 2, "flags": 5, "trigger": 1.2, "down": 0.5, "up": 0.08},
        )

    def test_round_trip_keeps_every_field(self):
        for rec in (bytes(8), encode_rt(switch=3, flags=7, trigger=3.3, down=2.4, up=2.4)):
            self.assertEqual(encode_rt(**decode_rt(rec)), rec)

    def test_out_of_range_travel_is_rejected(self):
        with self.assertRaises(ValueError):
            encode_rt(trigger=700.0)


class SocdTest(unittest.TestCase):
    def test_rule_is_a_type_11_key_record(self):
        self.assertEqual(SOCD_MODES[3], "last")
        self.assertEqual(encode_socd(3, 88, 90), b"\x0b\x03\x58\x5a")
        self.assertEqual(
            decode_key(b"\x0b\x03\x58\x5a"),
            {"type": "socd", "mode": 3, "key1": 88, "key2": 90},
        )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            encode_socd(9, 1, 2)


class CalibrationTest(unittest.TestCase):
    def test_report_carries_the_key_state_and_live_travel(self):
        rec = b"\x55" + bytes([0xFB, 41, 1]) + bytes(6) + b"\x2c\x01\x58\x02" + bytes(50)
        self.assertEqual(len(rec), 64)
        self.assertEqual(
            decode_calibration(rec),
            {"key": 41, "status": 1, "travel": 300, "travel_max": 600},
        )


class SettingsTest(unittest.TestCase):
    def test_patch_only_touches_the_named_fields(self):
        block = bytes(range(32))
        patched = patch_settings(block, zone_top=10, report_rate=2)
        self.assertEqual(patched[8], 10)
        self.assertEqual(patched[5], 2)
        self.assertEqual(patched[:5], block[:5])
        self.assertEqual(patched[9:], block[9:])

    def test_decode_returns_every_field(self):
        self.assertEqual(sorted(decode_settings(bytes(32))), sorted(SETTINGS_FIELDS))

    def test_unknown_or_out_of_range_values_are_rejected(self):
        with self.assertRaises(ValueError):
            patch_settings(bytes(32), nonsense=1)
        with self.assertRaises(ValueError):
            patch_settings(bytes(32), zone_top=256)


if __name__ == "__main__":
    unittest.main()
