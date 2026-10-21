import unittest

from fighting68.macro import (
    TRIGGER_REPEAT,
    TRIGGER_ROUTINE,
    TRIGGER_TOGGLE,
    MacroAction,
    MacroEvent,
    compile_timeline,
    decode_macro,
    decode_macros,
    decode_trigger,
    encode_macro,
    encode_macros,
    encode_trigger,
    events_to_actions,
    macro_keys,
)


class MacroEncodeTest(unittest.TestCase):
    def test_round_trip_single_macro(self):
        events = [
            MacroEvent(0, 4, True),  # press A
            MacroEvent(10, 22, True),  # press S 10ms later (chord A+S)
            MacroEvent(50, 4, False),  # release A
            MacroEvent(0, 22, False),  # release S
        ]
        region = encode_macros([events])
        self.assertEqual(len(region), 4008)
        decoded = decode_macros(region)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0], events)

    def test_record_layout_and_flags(self):
        blob = encode_macro([MacroEvent(300, 0x29, True)])
        self.assertEqual(blob[0], 2)  # record count * 2
        self.assertEqual(blob[1], 0)
        self.assertEqual(blob[4] | blob[5] << 8, 300)  # delay
        self.assertEqual(blob[6], 0x29)  # HID usage
        self.assertEqual(blob[7], 0xB0)  # 0x10|0x20|0x80 = keydown keyboard

    def test_keyup_and_mouse_flags(self):
        blob = encode_macro([MacroEvent(5, 4, False), MacroEvent(5, 1, True, mouse=True)])
        self.assertEqual(blob[7], 0x30)  # keyup keyboard: 0x10 | 0x20
        self.assertEqual(blob[11], 0x90)  # keydown mouse: 0x10 | 0x80

    def test_multiple_macro_headers(self):
        region = encode_macros(
            [
                [MacroEvent(0, 4, True), MacroEvent(10, 4, False)],
                [MacroEvent(0, 22, True), MacroEvent(20, 22, False)],
            ]
        )
        self.assertEqual(int.from_bytes(region[0:4], "little"), 400)  # first macro data offset
        self.assertEqual(len(decode_macros(region)), 2)

    def test_empty_macro_rejected(self):
        with self.assertRaises(ValueError):
            encode_macros([[]])


class MacroTimelineTest(unittest.TestCase):
    def test_simultaneous_chord(self):
        actions = [MacroAction(4, 0, 100), MacroAction(22, 0, 100)]
        self.assertEqual(
            [(e.delay_ms, e.usage, e.down) for e in compile_timeline(actions)],
            [(0, 4, True), (0, 22, True), (100, 4, False), (0, 22, False)],
        )

    def test_tap_uses_minimum_press(self):
        events = compile_timeline([MacroAction(4, 0, 0)])
        self.assertEqual([(e.usage, e.down, e.delay_ms) for e in events], [(4, True, 0), (4, False, 10)])

    def test_more_than_ten_keys_rejected(self):
        with self.assertRaises(ValueError):
            compile_timeline([MacroAction(i, 0, 0) for i in range(11)])

    def test_ten_keys_allowed(self):
        self.assertEqual(len(compile_timeline([MacroAction(i, 0, 0) for i in range(10)])), 20)

    def test_events_to_actions_round_trip(self):
        actions = [MacroAction(4, 0, 100), MacroAction(22, 10, 50)]
        restored = events_to_actions(compile_timeline(actions))
        self.assertEqual(
            sorted((a.usage, a.start_ms, a.duration_ms) for a in restored),
            sorted((a.usage, a.start_ms, a.duration_ms) for a in actions),
        )


class MacroKeysTest(unittest.TestCase):
    def test_modifier_usages_are_real_hid_codes(self):
        keys = macro_keys()
        self.assertEqual(keys[224], "L-Ctrl")
        self.assertEqual(keys[225], "L-Shift")
        self.assertEqual(keys[230], "R-Alt")
        self.assertEqual(keys[64], "F7")  # not the bogus "R-Alt"

    def test_letters_keep_their_usage(self):
        keys = macro_keys()
        self.assertEqual(keys[4], "A")
        self.assertEqual(keys[29], "Z")


class MacroTriggerTest(unittest.TestCase):
    def test_routine_trigger_record(self):
        self.assertEqual(encode_trigger(3), bytes([6, 3, 0, 0]))
        self.assertEqual(decode_trigger(encode_trigger(3)), {"slot": 3, "mode": 0, "repeat": 0})

    def test_repeat_and_toggle_trigger_records(self):
        self.assertEqual(encode_trigger(5, TRIGGER_REPEAT, 12), bytes([6, 5, 1, 12]))
        self.assertEqual(encode_trigger(5, TRIGGER_TOGGLE), bytes([6, 5, 2, 0]))

    def test_decode_trigger_rejects_non_macro_records(self):
        self.assertIsNone(decode_trigger(bytes([2, 0, 4, 0])))
        self.assertIsNone(decode_trigger(bytes([6, 3])))


if __name__ == "__main__":
    unittest.main()
