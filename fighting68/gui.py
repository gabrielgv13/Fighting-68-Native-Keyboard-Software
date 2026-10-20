"""Qt interface for the Fighting 68 driver: per-key sensitivity, SOCD, the
board-wide Hall-effect settings and calibration."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import layout
from .protocol import (
    EMPTY_KEY,
    REGIONS,
    RT_BOTTOM,
    RT_RAMPAGE,
    RT_WHOLE,
    SOCD_MODES,
    SWITCH_TYPES,
    Keyboard,
    decode_key,
    decode_rt,
    decode_settings,
    encode_rt,
    encode_socd,
    patch_settings,
)

ROLE = Qt.ItemDataRole.UserRole

REPORT_RATES = {0: "0 (unset)", 1: "1 kHz", 2: "4 kHz", 3: "8 kHz"}
CALIBRATION_SECONDS = 20.0

# Calibration board colours.
COLOR_RED = "#d9534f"  # not calibrated
COLOR_GREEN = "#5cb85c"  # calibrated
COLOR_BLUE = "#5bc0de"  # calibration improved

SETTING_LABELS = {
    "rest_time": "Rest time",
    "report_rate": "Report rate",
    "zone_top": "Top dead zone",
    "zone_bottom": "Bottom dead zone",
    "stability": "Stability filter",
    "auto_calibration": "Adaptive dynamic calibration (0/1)",
    "waken": "Wake behaviour",
}

# Hover text: shown while the mouse stays on a label, option or key.
SETTING_HELP = {
    "rest_time": "How long the board sits idle before it re-measures the resting point of every Hall sensor.",
    "report_rate": "How many times a second the board reports key state. 4 kHz halves latency over 1 kHz but costs host CPU.",
    "zone_top": "Travel ignored at the very top of the stroke, so a key resting slightly down is not seen as pressed.",
    "zone_bottom": "Travel ignored at the very bottom, so bottoming out does not count as an extra actuation.",
    "stability": "Smoothing on the Hall sensor reading. Higher filters jitter and noise, but adds a little latency.",
    "auto_calibration": "The board keeps trimming the rest point to follow temperature drift. 1 is on, 0 is off.",
    "waken": "How the board wakes the host from sleep.",
}

SOCD_HELP = {
    "key1": "The first key of the pair always fires; the second is ignored while both are held.",
    "key2": "The second key of the pair always fires; the first is ignored while both are held.",
    "last": "Last input wins: whichever key you pressed most recently fires, so opposing taps alternate cleanly.",
    "cancel": "Neither key fires while both are held. Releasing one lets the other through.",
}

RT_HELP = {
    "trigger": "Actuation point: how far you must press before the key fires.",
    "down": "Press sensitivity: how far you must press again before an already-fired key re-triggers.",
    "up": "Release sensitivity: how far you must lift before the key lets go.",
    "switch": "Which switch the key's travel profile is calibrated against.",
    "whole": "Rapid trigger stays active across the whole travel instead of only at the actuation point.",
    "bottom": "Bottoming out counts as an actuation.",
    "rampage": "Rampage Mode: the tightest rapid trigger the firmware offers, for competitive play. Very fast, but worn springs can chatter.",
}


class DeviceJobs(QObject):
    """Runs blocking HID work off the GUI thread. One worker serialises access to
    the board; signals marshal results back onto the GUI thread."""

    done = Signal(str, object)
    progress = Signal(object)
    failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.pool = ThreadPoolExecutor(max_workers=1)

    def submit(self, name: str, job) -> None:
        def run() -> None:
            try:
                self.done.emit(name, job())
            except Exception as exc:  # the user has to hear what went wrong
                self.failed.emit(name, str(exc))

        self.pool.submit(run)


class Window(QWidget):
    def __init__(self, vid: int, pid: int) -> None:
        super().__init__()
        self.vid, self.pid = vid, pid
        self.regions: dict[str, bytes] = {}
        self.jobs = DeviceJobs()
        self.jobs.done.connect(self._on_done)
        self.jobs.progress.connect(self._fill_calibration)
        self.jobs.failed.connect(self._on_failed)
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        self.setWindowTitle("Fighting 68")
        self.resize(1000, 640)

        header = QHBoxLayout()
        self.status = QLabel("connecting…")
        header.addWidget(self.status, 1)
        for text, slot in (("Refresh", self.refresh), ("Backup…", self.backup), ("Restore…", self.restore)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            header.addWidget(button)

        tabs = QTabWidget()
        tabs.addTab(self._sensitivity_tab(), "Sensitivity")
        tabs.addTab(self._socd_tab(), "SOCD")
        tabs.addTab(self._settings_tab(), "Behaviour")
        tabs.addTab(self._calibration_tab(), "Calibration")

        root = QVBoxLayout(self)
        root.addLayout(header)
        root.addWidget(tabs)

    def _sensitivity_tab(self) -> QWidget:
        board, self.key_buttons = self._board(on_click=self._toggle_key)
        board.setStyleSheet("QPushButton:checked { background-color: #4a90d9; color: white; }")
        self.selected: set[int] = set()
        self.current_slot = sorted(layout.LAYOUT)[0]
        self.key_buttons[self.current_slot].setChecked(True)
        self.selected.add(self.current_slot)

        self.rt_trigger = self._mm_box()
        self.rt_down = self._mm_box()
        self.rt_up = self._mm_box()
        self.rt_switch = QComboBox()
        for code, name in sorted(SWITCH_TYPES.items()):
            self.rt_switch.addItem(name, code)
        self.rt_whole = QCheckBox("Rapid trigger over the whole travel")
        self.rt_bottom = QCheckBox("Bottom optimization")
        self.rt_rampage = QCheckBox("Rampage Mode")
        for widget, help_text in (
            (self.rt_trigger, RT_HELP["trigger"]),
            (self.rt_down, RT_HELP["down"]),
            (self.rt_up, RT_HELP["up"]),
            (self.rt_switch, RT_HELP["switch"]),
            (self.rt_whole, RT_HELP["whole"]),
            (self.rt_bottom, RT_HELP["bottom"]),
            (self.rt_rampage, RT_HELP["rampage"]),
        ):
            widget.setToolTip(help_text)

        form = QFormLayout()
        form.addRow("Actuation point", self.rt_trigger)
        form.addRow("Press sensitivity", self.rt_down)
        form.addRow("Release sensitivity", self.rt_up)
        form.addRow("Switch", self.rt_switch)
        form.addRow(self.rt_whole)
        form.addRow(self.rt_bottom)
        form.addRow(self.rt_rampage)

        buttons = QHBoxLayout()
        self.selection_label = QLabel("")
        self._update_selection_label()
        for text, slot in (
            ("Select all", self._select_all),
            ("Apply to selection", self._apply_rt),
            ("Reset to board default", self._clear_rt),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addWidget(self.selection_label)

        panel = QWidget()
        inner = QVBoxLayout(panel)
        inner.addLayout(form)
        inner.addLayout(buttons)
        inner.addStretch(1)

        splitter = QSplitter()
        splitter.addWidget(board)
        splitter.addWidget(panel)
        splitter.setSizes([620, 380])
        return splitter

    def _socd_tab(self) -> QWidget:
        self._socd_turn = 1
        board, self.socd_buttons = self._board(on_click=self._socd_pick, checkable=False)
        self.socd_hint = QLabel("Click a key on the board: it fills Key 1, the next click fills Key 2.")

        self.socd_table = QTableWidget(0, 3)
        self.socd_table.setHorizontalHeaderLabels(["key 1", "key 2", "mode"])
        self.socd_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.socd_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.socd_table.verticalHeader().hide()

        self.socd_key1 = self._usage_combo()
        self.socd_key2 = self._usage_combo()
        self.socd_key1.setToolTip(SOCD_HELP["key1"].split(";")[0] + " Pick it on the board or from the list.")
        self.socd_key2.setToolTip(SOCD_HELP["key2"].split(";")[0] + " Pick it on the board or from the list.")
        self.socd_mode = QComboBox()
        for code, name in sorted(SOCD_MODES.items()):
            self.socd_mode.addItem(name, code)
            index = self.socd_mode.count() - 1
            self.socd_mode.setItemData(index, SOCD_HELP[name], Qt.ItemDataRole.ToolTipRole)
        self.socd_mode.setToolTip("How the board resolves both keys pressed at once. Hover a mode for details.")

        form = QFormLayout()
        form.addRow("Key 1", self.socd_key1)
        form.addRow("Key 2", self.socd_key2)
        form.addRow("Mode", self.socd_mode)

        buttons = QHBoxLayout()
        for text, slot in (("Add rule", self._apply_socd), ("Remove selected", self._remove_socd)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)

        pickers = QWidget()
        left = QVBoxLayout(pickers)
        left.addWidget(self.socd_hint)
        left.addLayout(form)
        left.addLayout(buttons)
        left.addWidget(self.socd_table, 1)

        panel = QWidget()
        body = QHBoxLayout(panel)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(board, 1)
        body.addWidget(pickers, 1)
        return panel

    def _socd_pick(self, button: QPushButton) -> None:
        """First click sets Key 1, the second Key 2, then it starts over."""
        usage = layout.USAGE_BY_SLOT.get(self._slot_of(button))
        if usage is None:
            self.socd_hint.setText(f"{button.text()} sends no HID usage; pick it from the list instead.")
            return
        target = self.socd_key1 if self._socd_turn == 1 else self.socd_key2
        target.setCurrentIndex(max(0, target.findData(usage)))
        self._socd_turn = 2 if self._socd_turn == 1 else 1
        self.socd_hint.setText(
            f"Key {3 - self._socd_turn} set to {button.text()}; click another key for Key {self._socd_turn}."
        )

    def _settings_tab(self) -> QWidget:
        self.report_rate = QComboBox()
        for code, name in sorted(REPORT_RATES.items()):
            self.report_rate.addItem(name, code)
        self.settings: dict[str, QWidget] = {
            name: (self.report_rate if name == "report_rate" else self._byte_box()) for name in SETTING_LABELS
        }

        form = QFormLayout()
        for name, widget in self.settings.items():
            label = QLabel(SETTING_LABELS[name])
            label.setToolTip(SETTING_HELP[name])  # hover the name for the explanation
            widget.setToolTip(SETTING_HELP[name])
            form.addRow(label, widget)

        button = QPushButton("Apply")
        button.clicked.connect(self._apply_settings)

        group = QGroupBox("Board-wide Hall-effect settings")
        inner = QVBoxLayout(group)
        inner.addLayout(form)
        inner.addWidget(button)
        inner.addStretch(1)
        return group

    def _calibration_tab(self) -> QWidget:
        self.calibration_button = QPushButton("Start measuring")
        self.calibration_button.clicked.connect(self._calibrate)
        hint = QLabel(
            f"Press each key fully to measure its travel ({CALIBRATION_SECONDS:.0f} s). "
            "This only reads the board; it never changes calibration."
        )

        board, self.cal_buttons = self._board(checkable=False)
        board.setEnabled(False)  # display only
        self.cal_colors: dict[int, str] = {}  # absent = measured (green)
        self._cal_seen: dict[int, dict] = {}
        self._paint_calibration()

        legend = QHBoxLayout()
        for colour, text in ((COLOR_GREEN, "Measured"), (COLOR_RED, "Not measured"), (COLOR_BLUE, "Pressed deeper")):
            swatch = QLabel()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(f"background-color: {colour}; border: 1px solid #666;")
            legend.addWidget(swatch)
            legend.addWidget(QLabel(text))
        legend.addStretch(1)

        self.calibration_table = QTableWidget(0, 4)
        self.calibration_table.setHorizontalHeaderLabels(["key", "status", "measured travel", "full travel"])
        self.calibration_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.calibration_table.verticalHeader().hide()

        left = QWidget()
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(self.calibration_button)
        column.addWidget(hint)
        column.addLayout(legend)
        column.addWidget(board, 1)

        panel = QWidget()
        body = QHBoxLayout(panel)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(left, 1)
        body.addWidget(self.calibration_table, 1)
        return panel

    def _paint_calibration(self) -> None:
        for slot, button in self.cal_buttons.items():
            colour = self.cal_colors.get(slot, COLOR_GREEN)
            button.setStyleSheet(f"background-color: {colour}; color: white;")
            button.setToolTip(
                f"{layout.LAYOUT[slot]}: "
                + ("not calibrated" if colour == COLOR_RED else "improved" if colour == COLOR_BLUE else "calibrated")
            )

    @staticmethod
    def _mm_box() -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 655.35)
        box.setDecimals(2)
        box.setSingleStep(0.01)
        box.setSuffix(" mm")
        return box

    @staticmethod
    def _byte_box() -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 255)
        return box

    @staticmethod
    def _usage_combo() -> QComboBox:
        combo = QComboBox()
        for name, code in sorted(layout.USAGE.items()):
            combo.addItem(name, code)
        return combo

    def _board(self, on_click=None, checkable: bool = True) -> tuple[QWidget, dict[int, QPushButton]]:
        """The 68 keys at their real widths, shared by the Sensitivity, SOCD and
        Calibration tabs. Returns the widget and {slot: button}."""
        board = QWidget()
        grid = QGridLayout(board)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(3)
        grid.setVerticalSpacing(3)
        buttons: dict[int, QPushButton] = {}
        for row, keys in enumerate(layout.ROWS):
            column = 0
            for slot, width in keys:
                button = QPushButton(layout.LAYOUT[slot])
                button.setCheckable(checkable)
                button.setFixedHeight(38)
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                span = round(width * 4)  # 4 grid columns to the key unit
                grid.addWidget(button, row, column, 1, span)
                if on_click:
                    button.clicked.connect(lambda _=False, b=button: on_click(b))
                buttons[slot] = button
                column += span
        for column in range(sum(w for _, w in layout.ROWS[0]) * 4):
            grid.setColumnStretch(column, 1)
        grid.setRowStretch(len(layout.ROWS), 1)  # empty row so the keys stay put
        return board, buttons

    def _slot_of(self, button: QPushButton) -> int:
        for name in ("key_buttons", "socd_buttons", "cal_buttons"):
            for slot, candidate in getattr(self, name, {}).items():
                if candidate is button:
                    return slot
        raise ValueError("button is not on the board")

    # ------------------------------------------------------------------- state

    def _current_slot(self) -> int:
        return self.current_slot

    def _selected_slots(self) -> list[int]:
        return sorted(self.selected)

    def _toggle_key(self, button: QPushButton) -> None:
        """Click toggles a key in the selection; Ctrl+click behaves the same way,
        so a selection grows by clicking and shrinks by clicking again."""
        slot = self._slot_of(button)
        if button.isChecked():
            self.selected.add(slot)
        else:
            self.selected.discard(slot)
        self.current_slot = slot
        self._update_selection_label()
        self._load_rt_form()

    def _select_all(self) -> None:
        self.selected = set(self.key_buttons)
        for button in self.key_buttons.values():
            button.setChecked(True)
        self.current_slot = sorted(self.selected)[0]
        self._update_selection_label()
        self._load_rt_form()

    def _update_selection_label(self) -> None:
        count = len(self.selected)
        self.selection_label.setText(f"{count} key{'s' if count != 1 else ''} selected")

    def _rt_record(self, slot: int) -> bytes:
        return self.regions["rt"][8 * slot : 8 * slot + 8]

    def _key_record(self, slot: int) -> bytes:
        return self.regions["keys"][4 * slot : 4 * slot + 4]

    def _fill_all(self) -> None:
        self._fill_settings()
        self._fill_socd()
        self._load_rt_form()
        self.status.setText("ready")

    def _fill_settings(self) -> None:
        for name, value in decode_settings(self.regions["settings"]).items():
            widget = self.settings[name]
            if isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(0, widget.findData(value)))
            else:
                widget.setValue(value)

    def _load_rt_form(self, *_) -> None:
        rt = decode_rt(self._rt_record(self._current_slot()))
        self.rt_trigger.setValue(rt["trigger"])
        self.rt_down.setValue(rt["down"])
        self.rt_up.setValue(rt["up"])
        self.rt_switch.setCurrentIndex(max(0, self.rt_switch.findData(rt["switch"])))
        self.rt_whole.setChecked(bool(rt["flags"] & RT_WHOLE))
        self.rt_bottom.setChecked(bool(rt["flags"] & RT_BOTTOM))
        self.rt_rampage.setChecked(bool(rt["flags"] & RT_RAMPAGE))

    def _fill_socd(self) -> None:
        rules, seen = [], set()
        for slot in range(128):
            rec = self._key_record(slot)
            info = decode_key(rec)
            if info["type"] != "socd" or rec in seen:
                continue
            seen.add(rec)
            rules.append((info["key1"], info["key2"], info["mode"], slot))
        self.socd_table.setRowCount(len(rules))
        for row, (key1, key2, mode, slot) in enumerate(rules):
            cells = [
                QTableWidgetItem(f"{layout.USAGE_NAMES.get(key1, hex(key1))}  ({hex(key1)})"),
                QTableWidgetItem(f"{layout.USAGE_NAMES.get(key2, hex(key2))}  ({hex(key2)})"),
                QTableWidgetItem(SOCD_MODES.get(mode, str(mode))),
            ]
            for column, cell in enumerate(cells):
                cell.setData(ROLE, (key1, key2, mode, slot))
                self.socd_table.setItem(row, column, cell)

    def _fill_calibration(self, seen: dict) -> None:
        for slot, values in seen.items():
            if not values["status"]:
                continue
            previous = self._cal_seen.get(slot)
            if previous is None:
                self.cal_colors[slot] = COLOR_GREEN  # first time this key measured
            elif values["travel"] > previous["travel"]:
                self.cal_colors[slot] = COLOR_BLUE  # pressed deeper than before
            elif slot in self.cal_colors and self.cal_colors[slot] != COLOR_BLUE:
                self.cal_colors[slot] = COLOR_GREEN  # measured again without going deeper
            self._cal_seen[slot] = values
        self._paint_calibration()

        self.calibration_table.setRowCount(len(seen))
        for row, slot in enumerate(sorted(seen)):
            values = seen[slot]
            cells = [
                f"{layout.LAYOUT.get(slot, slot)}  (slot {slot})",
                "ok" if values["status"] else "not calibrated",
                f"{values['travel'] / 10:.1f} mm",
                f"{values['travel_max'] / 10:.1f} mm",
            ]
            for column, text in enumerate(cells):
                self.calibration_table.setItem(row, column, QTableWidgetItem(text))

    # ----------------------------------------------------------------- actions

    def refresh(self) -> None:
        self.status.setText("reading the board…")

        def job() -> dict:
            with Keyboard.open(self.vid, self.pid) as kb:
                return {name: kb.read_region(name) for name in ("settings", "keys", "rt")}

        self.jobs.submit("load", job)

    def backup(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Backup", "fighting68-backup.json", "JSON (*.json)")
        if not path:
            return

        def job() -> str:
            with Keyboard.open(self.vid, self.pid) as kb:
                data = {name: kb.read_region(name).hex() for name in REGIONS}
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1)
            return f"wrote {path}"

        self.jobs.submit("backup", job)

    def restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Restore", "", "JSON (*.json)")
        if not path:
            return
        if QMessageBox.question(self, "Restore", f"Overwrite the keyboard with {path}?") != QMessageBox.StandardButton.Yes:
            return

        def job() -> str:
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            unknown = set(saved) - set(REGIONS)
            if unknown:
                raise ValueError(f"unknown region(s): {', '.join(sorted(unknown))}")
            with Keyboard.open(self.vid, self.pid) as kb:
                for name, hexdata in saved.items():
                    if name != "device":  # identity block; the driver never writes it
                        kb.write_region(name, bytes.fromhex(hexdata))
                kb.commit()
            return f"restored {path}"

        self.jobs.submit("restore", job)

    def _apply_rt(self) -> None:
        slots = self._selected_slots()
        if not slots:
            QMessageBox.warning(self, "Sensitivity", "Select at least one key first.")
            return
        record = self._rt_from_form()
        self._write_each("rt", [(8 * slot, record) for slot in slots], f"sensitivity saved to {len(slots)} key(s)")

    def _rt_from_form(self) -> bytes:
        flags = (
            (RT_WHOLE if self.rt_whole.isChecked() else 0)
            | (RT_BOTTOM if self.rt_bottom.isChecked() else 0)
            | (RT_RAMPAGE if self.rt_rampage.isChecked() else 0)
        )
        return encode_rt(
            switch=self.rt_switch.currentData(),
            flags=flags,
            trigger=self.rt_trigger.value(),
            down=self.rt_down.value(),
            up=self.rt_up.value(),
        )

    def _clear_rt(self) -> None:
        slots = self._selected_slots()
        if not slots:
            QMessageBox.warning(self, "Sensitivity", "Select at least one key first.")
            return
        self._write_each(
            "rt", [(8 * slot, bytes(8)) for slot in slots], f"{len(slots)} key(s) reset to the board default"
        )

    def _apply_socd(self) -> None:
        key1, key2 = self.socd_key1.currentData(), self.socd_key2.currentData()
        if key1 == key2:
            QMessageBox.warning(self, "SOCD", "Pick two different keys.")
            return
        record = encode_socd(self.socd_mode.currentData(), key1, key2)
        self._write_socd(key1, key2, record, f"{key1} / {key2} rule saved")

    def _remove_socd(self) -> None:
        row = self.socd_table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "SOCD", "Select a rule first.")
            return
        key1, key2, _, _ = self.socd_table.item(row, 0).data(ROLE)
        self._write_socd(key1, key2, EMPTY_KEY, "rule removed")

    def _write_socd(self, key1: int, key2: int, record: bytes, message: str) -> None:
        slots = [layout.SLOT_BY_USAGE.get(key) for key in (key1, key2)]
        if None in slots:
            QMessageBox.warning(self, "SOCD", "That key is not on this keyboard.")
            return
        self._write_each("keys", [(4 * slot, record) for slot in slots], message)

    def _apply_settings(self) -> None:
        self._write_region("settings", self._settings_from_form(), "settings saved")

    def _settings_from_form(self) -> bytes:
        wanted = {
            name: widget.currentData() if isinstance(widget, QComboBox) else widget.value()
            for name, widget in self.settings.items()
        }
        return patch_settings(self.regions["settings"], **wanted)

    def _write_region(self, region: str, data: bytes, message: str) -> None:
        def job() -> str:
            with Keyboard.open(self.vid, self.pid) as kb:
                kb.write_region(region, data)
                kb.commit()
            return message

        self.jobs.submit("write", job)

    def _write_each(self, region: str, writes: list[tuple[int, bytes]], message: str, name: str = "write") -> None:
        """Write several records in one connection, then commit."""
        def job() -> str:
            with Keyboard.open(self.vid, self.pid) as kb:
                for offset, data in writes:
                    kb.write_bytes(region, offset, data)
                kb.commit()
            return message

        self.jobs.submit(name, job)

    def _calibrate(self) -> None:
        self.calibration_button.setEnabled(False)
        self.status.setText("measuring…")
        self.cal_colors = {slot: COLOR_RED for slot in self.cal_buttons}  # every key starts unmeasured
        self._cal_seen = {}
        self._paint_calibration()

        def job() -> dict:
            with Keyboard.open(self.vid, self.pid) as kb:
                return kb.measure_travel(CALIBRATION_SECONDS, self.jobs.progress.emit)

        self.jobs.submit("calibrate", job)

    # --------------------------------------------------------------- callbacks

    def _on_done(self, name: str, result) -> None:
        if name == "load":
            self.regions = result
            self._fill_all()
        elif name == "calibrate":
            self._fill_calibration(result)
            self.calibration_button.setEnabled(True)
            self.status.setText(f"measured {len(result)} key(s)")
        else:
            self.status.setText(str(result))
            if name in ("write", "socd", "restore"):
                self.refresh()

    def _on_failed(self, name: str, message: str) -> None:
        self.calibration_button.setEnabled(True)
        self.status.setText(f"{name} failed")
        QMessageBox.warning(self, "Fighting 68", message)


def run(vid: int, pid: int) -> None:
    app = QApplication([])
    window = Window(vid, pid)
    window.show()
    app.exec()
