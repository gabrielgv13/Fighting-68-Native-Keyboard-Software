"""Qt interface for the Fighting 68 driver: per-key sensitivity, SOCD, the
board-wide Hall-effect settings and calibration."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
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

SETTING_LABELS = {
    "rest_time": "Rest time",
    "report_rate": "Report rate",
    "zone_top": "Top dead zone",
    "zone_bottom": "Bottom dead zone",
    "stability": "Stability filter",
    "auto_calibration": "Adaptive dynamic calibration (0/1)",
    "waken": "Wake behaviour",
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
        tabs.addTab(self._settings_tab(), "Settings")
        tabs.addTab(self._calibration_tab(), "Calibration")

        root = QVBoxLayout(self)
        root.addLayout(header)
        root.addWidget(tabs)

    def _sensitivity_tab(self) -> QWidget:
        self.key_group = QButtonGroup(self)
        self.key_group.setExclusive(True)
        self.key_group.idClicked.connect(self._load_rt_form)

        board = QWidget()
        board.setStyleSheet("QPushButton:checked { background-color: #4a90d9; color: white; }")
        grid = QGridLayout(board)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(3)
        grid.setVerticalSpacing(3)
        for row, keys in enumerate(layout.ROWS):
            column = 0
            for slot, width in keys:
                button = QPushButton(layout.LAYOUT[slot])
                button.setCheckable(True)
                button.setFixedHeight(38)
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                span = round(width * 4)  # 4 grid columns to the key unit
                grid.addWidget(button, row, column, 1, span)
                self.key_group.addButton(button, slot)
                column += span
        for column in range(sum(w for _, w in layout.ROWS[0]) * 4):
            grid.setColumnStretch(column, 1)
        grid.setRowStretch(len(layout.ROWS), 1)  # empty row so the keys stay put
        self.key_group.button(sorted(layout.LAYOUT)[0]).setChecked(True)

        self.rt_trigger = self._mm_box()
        self.rt_down = self._mm_box()
        self.rt_up = self._mm_box()
        self.rt_switch = QComboBox()
        for code, name in sorted(SWITCH_TYPES.items()):
            self.rt_switch.addItem(name, code)
        self.rt_whole = QCheckBox("Rapid trigger over the whole travel")
        self.rt_bottom = QCheckBox("Bottom optimization")
        self.rt_rampage = QCheckBox("Rampage Mode")

        form = QFormLayout()
        form.addRow("Actuation point", self.rt_trigger)
        form.addRow("Press sensitivity", self.rt_down)
        form.addRow("Release sensitivity", self.rt_up)
        form.addRow("Switch", self.rt_switch)
        form.addRow(self.rt_whole)
        form.addRow(self.rt_bottom)
        form.addRow(self.rt_rampage)

        buttons = QHBoxLayout()
        for text, slot in (("Apply to this key", self._apply_rt), ("Reset to board default", self._clear_rt)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)

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
        self.socd_table = QTableWidget(0, 3)
        self.socd_table.setHorizontalHeaderLabels(["key 1", "key 2", "mode"])
        self.socd_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.socd_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.socd_table.verticalHeader().hide()

        self.socd_key1 = self._usage_combo()
        self.socd_key2 = self._usage_combo()
        self.socd_mode = QComboBox()
        for code, name in sorted(SOCD_MODES.items()):
            self.socd_mode.addItem(name, code)

        form = QFormLayout()
        form.addRow("Key 1", self.socd_key1)
        form.addRow("Key 2", self.socd_key2)
        form.addRow("Mode", self.socd_mode)

        buttons = QHBoxLayout()
        for text, slot in (("Add rule", self._apply_socd), ("Remove selected", self._remove_socd)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)

        panel = QWidget()
        inner = QVBoxLayout(panel)
        inner.addLayout(form)
        inner.addLayout(buttons)
        inner.addWidget(self.socd_table, 1)
        return panel

    def _settings_tab(self) -> QWidget:
        self.report_rate = QComboBox()
        for code, name in sorted(REPORT_RATES.items()):
            self.report_rate.addItem(name, code)
        self.settings: dict[str, QWidget] = {
            name: (self.report_rate if name == "report_rate" else self._byte_box()) for name in SETTING_LABELS
        }

        form = QFormLayout()
        for name, widget in self.settings.items():
            form.addRow(SETTING_LABELS[name], widget)

        button = QPushButton("Apply")
        button.clicked.connect(self._apply_settings)

        group = QGroupBox("Board-wide Hall-effect settings")
        inner = QVBoxLayout(group)
        inner.addLayout(form)
        inner.addWidget(button)
        inner.addStretch(1)
        return group

    def _calibration_tab(self) -> QWidget:
        self.calibration_button = QPushButton("Start calibration")
        self.calibration_button.clicked.connect(self._calibrate)
        hint = QLabel(f"Press every key fully while it runs ({CALIBRATION_SECONDS:.0f} s).")
        self.calibration_table = QTableWidget(0, 4)
        self.calibration_table.setHorizontalHeaderLabels(["key", "status", "measured travel", "full travel"])
        self.calibration_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.calibration_table.verticalHeader().hide()

        panel = QWidget()
        inner = QVBoxLayout(panel)
        inner.addWidget(self.calibration_button)
        inner.addWidget(hint)
        inner.addWidget(self.calibration_table, 1)
        return panel

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

    # ------------------------------------------------------------------- state

    def _current_slot(self) -> int:
        return self.key_group.checkedId()

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
        slot = self._current_slot()
        self._write("rt", 8 * slot, self._rt_from_form(), f"slot {slot} sensitivity saved")

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
        self._write("rt", 8 * self._current_slot(), bytes(8), "key reset to the board default")

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

        def job() -> str:
            with Keyboard.open(self.vid, self.pid) as kb:
                for slot in slots:
                    kb.write_bytes("keys", 4 * slot, record)
            return message

        self.jobs.submit("socd", job)

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

    def _write(self, region: str, offset: int, data: bytes, message: str) -> None:
        """Write one record. Whole regions go through `_write_region`."""
        def job() -> str:
            with Keyboard.open(self.vid, self.pid) as kb:
                kb.write_bytes(region, offset, data)
                kb.commit()
            return message

        self.jobs.submit("write", job)

    def _calibrate(self) -> None:
        self.calibration_button.setEnabled(False)
        self.status.setText("calibrating…")

        def job() -> dict:
            with Keyboard.open(self.vid, self.pid) as kb:
                return kb.calibrate_for(CALIBRATION_SECONDS, self.jobs.progress.emit)

        self.jobs.submit("calibrate", job)

    # --------------------------------------------------------------- callbacks

    def _on_done(self, name: str, result) -> None:
        if name == "load":
            self.regions = result
            self._fill_all()
        elif name == "calibrate":
            self._fill_calibration(result)
            self.calibration_button.setEnabled(True)
            self.status.setText(f"calibration finished, {len(result)} keys reported")
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
