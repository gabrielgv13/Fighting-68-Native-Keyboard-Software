"""Smoke test for the Qt interface: build the window offscreen and check the
forms round-trip the wire records."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fighting68 import gui, layout
from fighting68.protocol import RT_BOTTOM, RT_WHOLE, encode_rt, encode_socd, patch_settings

SLOT_A, SLOT_D = 49, 51


class GuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        gui.DeviceJobs.submit = lambda self, name, job: None  # no board in tests

    def setUp(self):
        keys = bytearray(512)
        rule = encode_socd(3, 4, 7)  # A + D, last input wins
        keys[4 * SLOT_A : 4 * SLOT_A + 4] = rule
        keys[4 * SLOT_D : 4 * SLOT_D + 4] = rule

        self.window = gui.Window(0x0C45, 0x8030)
        self.window.regions = {
            "settings": patch_settings(bytes(32), report_rate=2, zone_top=10),
            "keys": bytes(keys),
            "rt": b"".join(
                encode_rt(trigger=1.2, down=0.5, up=0.08, flags=RT_WHOLE | RT_BOTTOM) if slot == SLOT_A else bytes(8)
                for slot in range(128)
            ),
        }
        self.window._fill_all()
        self.window.key_group.button(SLOT_A).click()

    def test_the_panel_is_laid_out_as_the_keyboard(self):
        self.assertEqual(len(self.window.key_group.buttons()), 68)
        for row in layout.ROWS:
            self.assertEqual(sum(width for _, width in row), 16, "rows must line up")

    def test_clicking_a_key_selects_it(self):
        self.window.key_group.button(SLOT_D).click()
        self.assertEqual(self.window._current_slot(), SLOT_D)

    def test_sensitivity_form_round_trips_the_record(self):
        self.assertEqual(self.window._current_slot(), SLOT_A)
        self.assertEqual(self.window.rt_trigger.value(), 1.2)
        self.assertEqual(self.window.rt_up.value(), 0.08)
        self.assertTrue(self.window.rt_whole.isChecked())
        self.assertFalse(self.window.rt_rampage.isChecked())
        self.assertEqual(self.window._rt_from_form(), self.window._rt_record(SLOT_A))

    def test_settings_form_round_trips_the_block(self):
        self.assertEqual(self.window.report_rate.currentData(), 2)
        self.assertEqual(self.window._settings_from_form(), self.window.regions["settings"])

    def test_settings_form_keeps_a_report_rate_it_does_not_know(self):
        self.window.regions["settings"] = bytes(32)  # every field zero, including the unset rate
        self.window._fill_settings()
        self.assertEqual(self.window.report_rate.currentData(), 0)
        self.assertEqual(self.window._settings_from_form(), bytes(32))

    def test_socd_rules_are_listed_once_per_rule(self):
        self.assertEqual(self.window.socd_table.rowCount(), 1)
        self.assertEqual(self.window.socd_table.item(0, 0).data(gui.ROLE)[:3], (4, 7, 3))

    def test_calibration_results_fill_the_table(self):
        self.window._fill_calibration({SLOT_A: {"status": 1, "travel": 34, "travel_max": 34}})
        self.assertEqual(self.window.calibration_table.rowCount(), 1)
        self.assertEqual(self.window.calibration_table.item(0, 3).text(), "3.4 mm")


if __name__ == "__main__":
    unittest.main()
