"""Smoke test for the Qt interface: build the window offscreen and check the
forms round-trip the wire records."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from fighting68 import gui, layout
from fighting68.protocol import RT_BOTTOM, RT_WHOLE, encode_rt, encode_socd, patch_settings

SLOT_A, SLOT_W, SLOT_D = 49, 34, 51


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
        # Start each test with exactly A selected: drop the Esc key the build preselects.
        self.window.key_buttons[0].setChecked(False)
        self.window.selected = {SLOT_A}
        self.window.current_slot = SLOT_A
        self.window.key_buttons[SLOT_A].setChecked(True)
        self.window._update_selection_label()
        self.window._load_rt_form()

    def test_the_panel_is_laid_out_as_the_keyboard(self):
        self.assertEqual(len(self.window.key_buttons), 68)
        for row in layout.ROWS:
            self.assertEqual(sum(width for _, width in row), 16, "rows must line up")

    def test_clicking_a_key_selects_it(self):
        self.window.key_buttons[SLOT_D].click()
        self.assertEqual(self.window._current_slot(), SLOT_D)
        self.assertIn(SLOT_D, self.window.selected)

    def test_ctrl_clicking_adds_to_the_selection(self):
        self.window.key_buttons[SLOT_D].click()
        self.window.key_buttons[SLOT_W].click()  # plain or Ctrl+click: the selection grows
        self.assertEqual(self.window._selected_slots(), sorted({SLOT_A, SLOT_W, SLOT_D}))
        self.assertEqual(self.window.selection_label.text(), "3 keys selected")

    def test_clicking_a_selected_key_deselects_it(self):
        self.window.key_buttons[SLOT_A].click()  # A starts selected, so one click clears it
        self.assertNotIn(SLOT_A, self.window.selected)
        self.assertEqual(self.window.selection_label.text(), "0 keys selected")
        self.window.key_buttons[SLOT_A].click()  # and one more brings it back
        self.assertEqual(self.window._selected_slots(), [SLOT_A])

    def test_select_all_selects_every_key(self):
        self.window._select_all()
        self.assertEqual(len(self.window.selected), 68)
        self.assertEqual(self.window.selection_label.text(), "68 keys selected")
        self.assertTrue(all(button.isChecked() for button in self.window.key_buttons.values()))

    def test_apply_writes_the_form_record_to_every_selected_key(self):
        self.window._select_all()
        written = []
        self.window._write_each = lambda region, writes, message, name="write": written.append((region, writes))
        self.window.rt_trigger.setValue(2.5)
        self.window._apply_rt()
        region, writes = written[0]
        self.assertEqual({offset for offset, _ in writes}, {8 * slot for slot in self.window.selected})
        self.assertTrue(all(record == self.window._rt_from_form() for _, record in writes))

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

    def test_the_board_is_drawn_on_every_tab(self):
        self.assertEqual(len(self.window.socd_buttons), 68)
        self.assertEqual(len(self.window.cal_buttons), 68)

    # ------------------------------------------------------------ hover help

    def test_hover_help_is_attached_to_every_setting(self):
        for name, label in self.window.settings.items():
            self.assertTrue(label.toolTip(), f"{name} has no description")
            self.assertIn(" ", label.toolTip())  # a sentence, not a stub

    def test_socd_modes_describe_themselves_on_hover(self):
        mode = self.window.socd_mode
        for index in range(mode.count()):
            self.assertTrue(mode.itemData(index, Qt.ItemDataRole.ToolTipRole), mode.itemText(index))
        self.assertIn("Rampage", self.window.rt_rampage.toolTip())
        self.assertTrue(self.window.rt_rampage.toolTip() != "")

    # ------------------------------------------------------------- SOCD board

    def test_clicking_the_socd_board_fills_key1_then_key2(self):
        self.window.socd_key1.setCurrentIndex(-1)
        self.window.socd_key2.setCurrentIndex(-1)
        self.window._socd_turn = 1
        self.window.socd_buttons[SLOT_A].click()
        self.assertEqual(self.window.socd_key1.currentData(), layout.USAGE_BY_SLOT[SLOT_A])
        self.window.socd_buttons[SLOT_D].click()
        self.assertEqual(self.window.socd_key2.currentData(), layout.USAGE_BY_SLOT[SLOT_D])
        self.assertEqual(self.window._socd_turn, 1)  # wrapped back to Key 1

    # --------------------------------------------------------- calibration art

    def test_board_starts_all_green_and_turns_red_when_calibration_starts(self):
        self.assertEqual(self.window.cal_colors, {})  # nothing recorded = green everywhere
        colour_a = self.window.cal_buttons[SLOT_A].styleSheet()
        self.assertIn(gui.COLOR_GREEN, colour_a)

        self.window._calibrate()
        self.assertEqual(self.window.cal_colors[SLOT_A], gui.COLOR_RED)
        self.assertIn(gui.COLOR_RED, self.window.cal_buttons[SLOT_A].styleSheet())

    def test_first_reading_is_green_and_a_deeper_one_is_blue(self):
        self.window._calibrate()
        self.window._fill_calibration({SLOT_A: {"status": 1, "travel": 30, "travel_max": 30}})
        self.assertEqual(self.window.cal_colors[SLOT_A], gui.COLOR_GREEN)

        self.window._fill_calibration({SLOT_A: {"status": 1, "travel": 36, "travel_max": 36}})
        self.assertEqual(self.window.cal_colors[SLOT_A], gui.COLOR_BLUE)

        # shallower than the best seen keeps the blue mark
        self.window._fill_calibration({SLOT_A: {"status": 1, "travel": 30, "travel_max": 30}})
        self.assertEqual(self.window.cal_colors[SLOT_A], gui.COLOR_BLUE)

    def test_a_key_the_board_never_reports_stays_red(self):
        self.window._calibrate()
        self.window._fill_calibration({SLOT_D: {"status": 1, "travel": 34, "travel_max": 34}})
        self.assertEqual(self.window.cal_colors[SLOT_D], gui.COLOR_GREEN)
        self.assertEqual(self.window.cal_colors[SLOT_A], gui.COLOR_RED)


if __name__ == "__main__":
    unittest.main()
