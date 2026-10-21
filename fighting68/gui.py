"""Qt interface for the Fighting 68 driver: per-key sensitivity, SOCD, the
board-wide Hall-effect settings and calibration."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
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
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
from .macro import (
    MacroAction,
    MacroEvent,
    TRIGGER_REPEAT,
    TRIGGER_ROUTINE,
    TRIGGER_TOGGLE,
    compile_timeline,
    decode_macros,
    decode_trigger,
    encode_macros,
    encode_trigger,
    events_to_actions,
    macro_keys,
)
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
    encode_key,
    encode_rt,
    encode_socd,
    patch_settings,
)

ROLE = Qt.ItemDataRole.UserRole

REPORT_RATES = {0: "0 (unset)", 1: "1 kHz", 2: "4 kHz", 3: "8 kHz"}
# Calibration board colours.
COLOR_RED = "#d9534f"  # not calibrated
COLOR_GREEN = "#5cb85c"  # calibrated
COLOR_BLUE = "#5bc0de"  # calibration improved
COLOR_UNCALIBRATED = "#777777"  # no successful calibration report in this session

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


class MacroTimeline(QWidget):
    """A video-editor style strip: each key action is a block on its own row,
    positioned by start time and sized by its hold duration. Click a block to
    select the action."""

    clicked = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.actions: list[MacroAction] = []
        self.selected: int | None = None
        self.setMinimumHeight(140)
        self._row = 22
        self._gap = 4
        self._left = 56
        self._top = 24

    def set_actions(self, actions: list[MacroAction], selected: int | None = None) -> None:
        self.actions = actions
        self.selected = selected
        self.update()

    def _time_span(self) -> int:
        return max(1000, max((a.start_ms + max(a.duration_ms, 10) for a in self.actions), default=0))

    def _block(self, index: int):
        span = self._time_span()
        px = (self.width() - self._left - 8) / span
        action = self.actions[index]
        x = self._left + action.start_ms * px
        w = max(action.duration_ms, 10) * px
        y = self._top + index * (self._row + self._gap)
        return (x, y, max(w, 6), self._row)

    def mousePressEvent(self, event) -> None:
        for index in range(len(self.actions)):
            x, y, w, h = self._block(index)
            if x <= event.position().x() <= x + w and y <= event.position().y() <= y + h:
                self.selected = index
                self.clicked.emit(index)
                self.update()
                return
        self.selected = None
        self.clicked.emit(-1)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1e1e1e"))
        span = self._time_span()
        px = (self.width() - self._left - 8) / span
        names = macro_keys()

        painter.setPen(QColor("#888888"))
        step = 100 if span <= 1000 else 500
        for ms in range(0, span + step, step):
            x = self._left + ms * px
            painter.drawLine(int(x), 0, int(x), self.height())
            painter.drawText(int(x) + 2, 14, f"{ms}ms")

        for index, action in enumerate(self.actions):
            x, y, w, h = self._block(index)
            colour = "#4a90d9" if index == self.selected else "#6b8fc0"
            painter.setPen(QPen(QColor("#1a1a1a")))
            painter.setBrush(QColor(colour))
            painter.drawRect(int(x), int(y), int(w), int(h))
            label = names.get(action.usage, f"0x{action.usage:02x}")
            painter.setPen(QColor("white"))
            painter.drawText(int(x) + 3, int(y) + h - 6, label)
        painter.end()


class Window(QWidget):
    def __init__(self, vid: int, pid: int) -> None:
        super().__init__()
        self.vid, self.pid = vid, pid
        self.regions: dict[str, bytes] = {}
        # Keep the key records that macro bindings temporarily replace. These
        # survive refreshes in this window; factory mappings are the fallback
        # when the application starts with an already-bound macro.
        self.macro_original_bindings: dict[str, dict[int, bytes]] = {"keys": {}, "fn": {}}
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
        tabs.addTab(self._macro_tab(), "Macro")

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
        self.calibration_button = QPushButton("Start recalibration")
        self.calibration_button.clicked.connect(self._calibrate)
        hint = QLabel("Press each key you want to recalibrate. Successful keys turn green; untouched keys keep their current calibration.")
        self.calibration_save_button = QPushButton("Save")
        self.calibration_save_button.setEnabled(False)
        self.calibration_save_button.clicked.connect(lambda: self._finish_calibration("save"))
        self.calibration_cancel_button = QPushButton("Cancel")
        self.calibration_cancel_button.setEnabled(False)
        self.calibration_cancel_button.clicked.connect(lambda: self._finish_calibration("cancel"))
        self._calibration_decision = None
        self._calibration_stop = threading.Event()
        self._calibration_active = False
        self._calibration_color_snapshot: dict[int, str] = {}
        self._calibration_seen_snapshot: dict[int, dict] = {}

        board, self.cal_buttons = self._board(checkable=False)
        board.setEnabled(False)  # display only
        self.cal_colors: dict[int, str] = {slot: COLOR_UNCALIBRATED for slot in self.cal_buttons}
        self._cal_seen: dict[int, dict] = {}
        self._paint_calibration()

        legend = QHBoxLayout()
        for colour, text in ((COLOR_GREEN, "Calibrated in this session"), (COLOR_UNCALIBRATED, "Not changed")):
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
        controls = QHBoxLayout()
        controls.addWidget(self.calibration_save_button)
        controls.addWidget(self.calibration_cancel_button)
        column.addLayout(controls)
        column.addWidget(hint)
        column.addLayout(legend)
        column.addWidget(board, 1)

        panel = QWidget()
        body = QHBoxLayout(panel)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(left, 1)
        body.addWidget(self.calibration_table, 1)
        return panel

    def _macro_tab(self) -> QWidget:
        self.macro_actions: dict[int, list[MacroAction]] = {}  # macro index -> timeline
        self.macro_triggers: dict[int, dict] = {}  # macro index -> {"keys": set, "fn": set, "mode", "repeat"}
        self.current_macro: int | None = None
        self._selected_macro_action: int | None = None

        # Compact macro library.
        self.macro_list = QListWidget()
        self.macro_list.currentRowChanged.connect(self._select_macro)
        add = QPushButton("Add macro")
        add.clicked.connect(self._add_macro)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_macro)
        left = QWidget()
        left_col = QVBoxLayout(left)
        left_col.addWidget(QLabel("Macros"))
        left_col.addWidget(self.macro_list, 1)
        row = QHBoxLayout()
        row.addWidget(add)
        row.addWidget(remove)
        left_col.addLayout(row)

        # Main editor: the timeline stays prominent and directly selectable.
        self.macro_timeline = MacroTimeline()
        self.macro_timeline.setMinimumHeight(190)
        self.macro_timeline.clicked.connect(self._select_action)

        self.macro_key_combo = QComboBox()
        for usage, label in sorted(macro_keys().items(), key=lambda item: item[1].lower()):
            self.macro_key_combo.addItem(label, usage)
        self.macro_type_combo = QComboBox()
        self.macro_type_combo.addItem("Hold", "hold")
        self.macro_type_combo.addItem("Tap", "tap")
        self.macro_start = QSpinBox()
        self.macro_start.setRange(0, 60_000)
        self.macro_start.setSuffix(" ms")
        self.macro_duration = QSpinBox()
        self.macro_duration.setRange(0, 60_000)
        self.macro_duration.setSuffix(" ms")

        add_action = QPushButton("Add")
        self.macro_add_action_button = add_action
        add_action.setEnabled(False)
        add_action.clicked.connect(self._add_action)
        update_action = QPushButton("Update")
        self.macro_update_action_button = update_action
        update_action.setEnabled(False)
        update_action.clicked.connect(self._update_action)
        del_action = QPushButton("Delete")
        self.macro_delete_action_button = del_action
        del_action.setEnabled(False)
        del_action.clicked.connect(self._delete_action)

        action_editor = QGroupBox("Action")
        action_row = QHBoxLayout(action_editor)
        for label, widget in (
            ("Key", self.macro_key_combo),
            ("Type", self.macro_type_combo),
            ("Start", self.macro_start),
            ("Hold", self.macro_duration),
        ):
            field = QWidget()
            field_col = QVBoxLayout(field)
            field_col.setContentsMargins(0, 0, 0, 0)
            field_col.addWidget(QLabel(label))
            field_col.addWidget(widget)
            action_row.addWidget(field, 1)
        action_row.addWidget(add_action)
        action_row.addWidget(update_action)
        action_row.addWidget(del_action)

        # Trigger mapping is available on demand, collapsed by default.
        self.macro_trigger_layer = QComboBox()
        self.macro_trigger_layer.addItem("Normal keys", "keys")
        self.macro_trigger_layer.setItemData(
            0, "A selected key starts this macro from the normal keyboard layer.", Qt.ItemDataRole.ToolTipRole
        )
        self.macro_trigger_layer.addItem("Fn + key combination", "fn")
        self.macro_trigger_layer.setItemData(
            1, "A selected key starts this macro while the keyboard's Fn layer is active.", Qt.ItemDataRole.ToolTipRole
        )
        self.macro_trigger_layer.setToolTip("Choose which keyboard layer contains the macro trigger.")
        self.macro_trigger_layer.currentIndexChanged.connect(lambda _: self._paint_trigger_board())
        self.macro_trigger_mode = QComboBox()
        self.macro_trigger_mode.addItem("Routine", TRIGGER_ROUTINE)
        self.macro_trigger_mode.setItemData(
            0, "Play the macro once each time its trigger is activated.", Qt.ItemDataRole.ToolTipRole
        )
        self.macro_trigger_mode.addItem("Repeat playback", TRIGGER_REPEAT)
        self.macro_trigger_mode.setItemData(
            1, "Play the macro the selected number of times for each activation.", Qt.ItemDataRole.ToolTipRole
        )
        self.macro_trigger_mode.addItem("Toggle (press again to stop)", TRIGGER_TOGGLE)
        self.macro_trigger_mode.setItemData(
            2, "Start continuous playback; activate the trigger again to stop it.", Qt.ItemDataRole.ToolTipRole
        )
        self.macro_trigger_mode.setToolTip("Choose how playback behaves after the trigger is activated.")
        self.macro_trigger_mode.currentIndexChanged.connect(lambda _: self._trigger_mode_changed())
        self.macro_trigger_count = QSpinBox()
        self.macro_trigger_count.setRange(1, 255)
        self.macro_trigger_count.setSuffix("x")
        self.macro_trigger_count.setToolTip("Number of playbacks per activation. Used only in Repeat playback mode.")

        trigger_board, self.macro_trigger_buttons = self._board(on_click=self._toggle_trigger_key)
        for slot, button in self.macro_trigger_buttons.items():
            button.setToolTip(
                f"{layout.LAYOUT[slot]}: click to add or remove this key as a macro trigger. "
                "Each selected key is stored as an individual trigger."
            )
        trigger_board.setStyleSheet("QPushButton:checked { background-color: #4a90d9; color: white; }")
        trigger_area = QScrollArea()
        trigger_area.setWidgetResizable(True)
        trigger_area.setMinimumHeight(145)
        trigger_area.setMaximumHeight(190)
        trigger_area.setWidget(trigger_board)

        self.macro_trigger_group = QGroupBox("Trigger keys and playback")
        self.macro_trigger_group.setToolTip(
            "Configure the trigger layer, playback mode, repeat count, and key or keys assigned to this macro."
        )
        self.macro_trigger_group.setCheckable(True)
        self.macro_trigger_group.setChecked(False)
        trigger_body = QWidget()
        trigger_col = QVBoxLayout(trigger_body)
        trigger_col.setContentsMargins(0, 0, 0, 0)
        trigger_row = QHBoxLayout()
        trigger_form = QFormLayout()
        trigger_form.addRow("Playback", self.macro_trigger_mode)
        trigger_form.addRow("Repeat", self.macro_trigger_count)
        trigger_row.addLayout(trigger_form)
        layer_form = QFormLayout()
        layer_form.addRow("Layer", self.macro_trigger_layer)
        trigger_row.addLayout(layer_form)
        trigger_row.addStretch(1)
        trigger_col.addLayout(trigger_row)
        trigger_hint = QLabel(
            "Choose a layer, then click keys to assign the trigger. Multiple selected keys are currently "
            "written as separate triggers for the same macro."
        )
        trigger_hint.setToolTip(
            "The keyboard protocol stores one macro trigger per key record; this driver does not encode an "
            "all-keys-at-once chord."
        )
        trigger_col.addWidget(trigger_hint)
        trigger_col.addWidget(trigger_area)
        trigger_body.setVisible(False)
        self.macro_trigger_group.toggled.connect(trigger_body.setVisible)
        trigger_group_layout = QVBoxLayout(self.macro_trigger_group)
        trigger_group_layout.addWidget(trigger_body)

        editor = QWidget()
        editor_col = QVBoxLayout(editor)
        editor_col.setContentsMargins(8, 4, 4, 4)
        header = QHBoxLayout()
        header.addWidget(QLabel("Timeline"))
        header.addWidget(QLabel("Select a block to edit it; overlapping blocks play together."), 1)
        save = QPushButton("Save to keyboard")
        save.clicked.connect(self._apply_macros)
        header.addWidget(save)
        editor_col.addLayout(header)
        editor_col.addWidget(self.macro_timeline, 1)
        editor_col.addWidget(action_editor)
        editor_col.addWidget(self.macro_trigger_group)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(editor)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 5)
        split.setSizes([170, 830])
        return split

    # --------------------------------------------------------------- macro model

    def _empty_trigger(self) -> dict:
        return {"keys": set(), "fn": set(), "mode": TRIGGER_ROUTINE, "repeat": 1}

    def _current_trigger(self) -> dict:
        return self.macro_triggers.setdefault(self.current_macro, self._empty_trigger())

    def _fill_macros(self) -> None:
        events = decode_macros(self.regions["macros"])
        self.macro_actions = {i: events_to_actions(m) for i, m in enumerate(events)}
        self.macro_triggers = {i: self._empty_trigger() for i in self.macro_actions}
        for layer in ("keys", "fn"):
            data = self.regions[layer]
            for slot in range(128):
                record = data[4 * slot : 4 * slot + 4]
                trigger = decode_trigger(record)
                if trigger is None:
                    self.macro_original_bindings[layer][slot] = record
                elif slot not in self.macro_original_bindings[layer]:
                    usage = layout.USAGE_BY_SLOT.get(slot) if layer == "keys" else None
                    self.macro_original_bindings[layer][slot] = encode_key(usage or 0) if usage else EMPTY_KEY
                if trigger and trigger["slot"] in self.macro_triggers:
                    self.macro_triggers[trigger["slot"]][layer].add(slot)
                    self.macro_triggers[trigger["slot"]]["mode"] = trigger["mode"]
                    self.macro_triggers[trigger["slot"]]["repeat"] = max(1, trigger["repeat"])
        self.macro_list.blockSignals(True)
        self.macro_list.clear()
        for i in self.macro_actions:
            self.macro_list.addItem(f"M{i + 1}")
        self.macro_list.blockSignals(False)
        if self.macro_list.count():
            self.macro_list.setCurrentRow(0)

    def _select_macro(self, row: int) -> None:
        if row < 0:
            return
        self.current_macro = row
        actions = self.macro_actions.get(row, [])
        self._selected_macro_action = 0 if actions else None
        self._refresh_macro_view()

    def _refresh_macro_view(self) -> None:
        actions = self.macro_actions.get(self.current_macro, [])
        if self._selected_macro_action is not None and not (0 <= self._selected_macro_action < len(actions)):
            self._selected_macro_action = None
        self.macro_timeline.set_actions(actions, self._selected_macro_action)
        selected = self._selected_macro_action
        if selected is not None:
            action = actions[selected]
            self.macro_key_combo.setCurrentIndex(max(0, self.macro_key_combo.findData(action.usage)))
            self.macro_type_combo.setCurrentIndex(0 if action.duration_ms else 1)
            self.macro_start.setValue(action.start_ms)
            self.macro_duration.setValue(action.duration_ms)
        self.macro_update_action_button.setEnabled(selected is not None)
        self.macro_delete_action_button.setEnabled(selected is not None)
        self.macro_add_action_button.setEnabled(self.current_macro is not None)
        trigger = self.macro_triggers.get(self.current_macro, self._empty_trigger())
        self.macro_trigger_mode.setCurrentIndex(max(0, self.macro_trigger_mode.findData(trigger["mode"])))
        self.macro_trigger_count.setValue(max(1, trigger["repeat"]))
        self.macro_trigger_count.setEnabled(trigger["mode"] == TRIGGER_REPEAT)
        self._paint_trigger_board()

    def _paint_trigger_board(self) -> None:
        trigger = self.macro_triggers.get(self.current_macro, self._empty_trigger())
        selected = trigger.get(self.macro_trigger_layer.currentData(), set())
        for slot, button in self.macro_trigger_buttons.items():
            button.setChecked(slot in selected)

    def _toggle_trigger_key(self, button: QPushButton) -> None:
        if self.current_macro is None:
            return
        slot = self._slot_of(button)
        layer = self.macro_trigger_layer.currentData()
        if slot in self._current_trigger()[layer]:
            self._current_trigger()[layer].discard(slot)
        else:
            self._current_trigger()[layer].add(slot)
        self._paint_trigger_board()

    def _trigger_mode_changed(self) -> None:
        if self.current_macro is None:
            return
        self._current_trigger()["mode"] = self.macro_trigger_mode.currentData()
        self.macro_trigger_count.setEnabled(self.macro_trigger_mode.currentData() == TRIGGER_REPEAT)

    def _current_actions(self) -> list[MacroAction]:
        return self.macro_actions.setdefault(self.current_macro, [])

    def _add_macro(self) -> None:
        index = len(self.macro_actions)
        self.macro_actions[index] = [MacroAction(4, 0, 0)]  # tap "A" at 0 ms
        self.macro_triggers[index] = self._empty_trigger()
        self.macro_list.addItem(f"M{index + 1}")
        self.macro_list.setCurrentRow(index)

    def _remove_macro(self) -> None:
        row = self.macro_list.currentRow()
        if row < 0:
            return
        self.macro_list.blockSignals(True)
        self.macro_list.takeItem(row)
        self.macro_actions = {i - 1 if i > row else i: a for i, a in self.macro_actions.items() if i != row}
        self.macro_triggers = {i - 1 if i > row else i: t for i, t in self.macro_triggers.items() if i != row}
        for i in range(self.macro_list.count()):
            self.macro_list.item(i).setText(f"M{i + 1}")
        if self.macro_list.count():
            next_row = min(row, self.macro_list.count() - 1)
            self.macro_list.setCurrentRow(next_row)
            self.macro_list.blockSignals(False)
            self._select_macro(next_row)
        else:
            self.macro_list.blockSignals(False)
            self.current_macro = None
            self._selected_macro_action = None
            self._refresh_macro_view()

    def _add_action(self) -> None:
        if self.current_macro is None:
            return
        action = MacroAction(
            self.macro_key_combo.currentData(),
            self.macro_start.value(),
            self.macro_duration.value() if self.macro_type_combo.currentData() == "hold" else 0,
        )
        self._current_actions().append(action)
        self._selected_macro_action = len(self._current_actions()) - 1
        self._refresh_macro_view()

    def _update_action(self) -> None:
        row = self._selected_macro_action
        if self.current_macro is None or row is None or not (0 <= row < len(self._current_actions())):
            return
        self._current_actions()[row] = MacroAction(
            self.macro_key_combo.currentData(),
            self.macro_start.value(),
            self.macro_duration.value() if self.macro_type_combo.currentData() == "hold" else 0,
        )
        self._refresh_macro_view()

    def _delete_action(self) -> None:
        row = self._selected_macro_action
        if self.current_macro is None or row is None or not (0 <= row < len(self._current_actions())):
            return
        del self._current_actions()[row]
        self._selected_macro_action = min(row, len(self._current_actions()) - 1) if self._current_actions() else None
        self._refresh_macro_view()

    def _select_action(self, row: int) -> None:
        actions = self.macro_actions.get(self.current_macro, [])
        if not (0 <= row < len(actions)):
            self._selected_macro_action = None
            self.macro_timeline.set_actions(actions, None)
            self.macro_update_action_button.setEnabled(False)
            self.macro_delete_action_button.setEnabled(False)
            return
        self._selected_macro_action = row
        self.macro_timeline.set_actions(actions, row)
        action = actions[row]
        self.macro_key_combo.setCurrentIndex(max(0, self.macro_key_combo.findData(action.usage)))
        self.macro_type_combo.setCurrentIndex(0 if action.duration_ms else 1)
        self.macro_start.setValue(action.start_ms)
        self.macro_duration.setValue(action.duration_ms)
        self.macro_update_action_button.setEnabled(True)
        self.macro_delete_action_button.setEnabled(True)

    def _apply_macros(self) -> None:
        if self.current_macro is not None:
            self._current_trigger()["mode"] = self.macro_trigger_mode.currentData()
            self._current_trigger()["repeat"] = self.macro_trigger_count.value()
        macros = []
        for i in sorted(self.macro_actions):
            actions = self.macro_actions[i]
            if not actions:
                QMessageBox.warning(self, "Macro", f"Macro M{i + 1} has no actions.")
                return
            macros.append(compile_timeline(actions))
        try:
            region = encode_macros(macros)
        except ValueError as exc:
            QMessageBox.warning(self, "Macro", str(exc))
            return
        keys = bytearray(self.regions.get("keys", bytes(512)))
        fn = bytearray(self.regions.get("fn", bytes(512)))
        for slot in range(128):  # this feature owns every macro binding
            if keys[4 * slot] == 6:
                keys[4 * slot : 4 * slot + 4] = self.macro_original_bindings["keys"].get(
                    slot, encode_key(layout.USAGE_BY_SLOT[slot]) if slot in layout.USAGE_BY_SLOT else EMPTY_KEY
                )
            if fn[4 * slot] == 6:
                fn[4 * slot : 4 * slot + 4] = self.macro_original_bindings["fn"].get(slot, EMPTY_KEY)
        for i in sorted(self.macro_actions):
            trigger = self.macro_triggers.get(i, self._empty_trigger())
            repeat = trigger["repeat"] if trigger["mode"] == TRIGGER_REPEAT else 0
            record = encode_trigger(i, trigger["mode"], repeat)
            for slot in trigger.get("keys", ()):
                if 0 <= slot < 128:
                    keys[4 * slot : 4 * slot + 4] = record
                    self.macro_original_bindings["keys"].setdefault(
                        slot, encode_key(layout.USAGE_BY_SLOT[slot]) if slot in layout.USAGE_BY_SLOT else EMPTY_KEY
                    )
            for slot in trigger.get("fn", ()):
                if 0 <= slot < 128:
                    fn[4 * slot : 4 * slot + 4] = record
                    self.macro_original_bindings["fn"].setdefault(slot, EMPTY_KEY)

        def job() -> str:
            with Keyboard.open(self.vid, self.pid) as kb:
                kb.write_region("macros", region)
                kb.write_region("keys", bytes(keys))
                kb.write_region("fn", bytes(fn))
                kb.commit()
            return f"{len(macros)} macro(s) saved"

        self.jobs.submit("macro", job)

    def _paint_calibration(self) -> None:
        for slot, button in self.cal_buttons.items():
            colour = self.cal_colors.get(slot, COLOR_UNCALIBRATED)
            button.setStyleSheet(f"background-color: {colour}; color: white;")
            button.setToolTip(
                f"{layout.LAYOUT[slot]}: "
                + ("not changed" if colour == COLOR_UNCALIBRATED else "improved" if colour == COLOR_BLUE else "calibrated")
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
        for name in ("key_buttons", "socd_buttons", "cal_buttons", "macro_trigger_buttons"):
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
        self._fill_macros()
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
            if not values["status"] or values["travel"] <= 0 or values["travel_max"] <= 0:
                continue
            self.cal_colors[slot] = COLOR_GREEN
            self._cal_seen[slot] = values
        self._paint_calibration()

        successful = {
            slot: values for slot, values in seen.items()
            if values["status"] and values["travel"] > 0 and values["travel_max"] > 0
        }
        self.calibration_table.setRowCount(len(successful))
        for row, slot in enumerate(sorted(successful)):
            values = successful[slot]
            cells = [
                f"{layout.LAYOUT.get(slot, slot)}  (slot {slot})",
                "ok" if values["status"] else "not calibrated",
                f"{values['travel'] / 100:.2f} mm",
                f"{values['travel_max'] / 100:.2f} mm",
            ]
            for column, text in enumerate(cells):
                self.calibration_table.setItem(row, column, QTableWidgetItem(text))

    # ----------------------------------------------------------------- actions

    def refresh(self) -> None:
        self.status.setText("reading the board…")

        def job() -> dict:
            with Keyboard.open(self.vid, self.pid) as kb:
                return {name: kb.read_region(name) for name in ("settings", "keys", "rt", "macros", "fn")}

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
        self.calibration_save_button.setEnabled(True)
        self.calibration_cancel_button.setEnabled(True)
        self._calibration_active = True
        self._calibration_decision = None
        self._calibration_stop.clear()
        self._calibration_color_snapshot = dict(self.cal_colors)
        self._calibration_seen_snapshot = {slot: dict(values) for slot, values in self._cal_seen.items()}
        self.status.setText("calibration active — press the keys to recalibrate")
        self._cal_seen = {}
        self._paint_calibration()

        def job() -> dict:
            with Keyboard.open(self.vid, self.pid) as kb:
                seen, decision = kb.calibration_session(
                    lambda: self._calibration_decision if self._calibration_stop.is_set() else None,
                    self.jobs.progress.emit,
                )
                if decision == "save" and any(
                    row["status"] and row["travel"] > 0 and row["travel_max"] > 0
                    for row in seen.values()
                ):
                    kb.save_calibration()
                return {"seen": seen, "decision": decision}

        self.jobs.submit("calibrate", job)

    def _finish_calibration(self, decision: str) -> None:
        if not self._calibration_active:
            return
        self._calibration_decision = decision
        self._calibration_stop.set()
        self.calibration_save_button.setEnabled(False)
        self.calibration_cancel_button.setEnabled(False)
        self.status.setText("saving calibration…" if decision == "save" else "cancelling calibration…")

    # --------------------------------------------------------------- callbacks

    def _on_done(self, name: str, result) -> None:
        if name == "load":
            self.regions = result
            self._fill_all()
        elif name == "calibrate":
            decision = result["decision"]
            if decision == "cancel":
                self.cal_colors = self._calibration_color_snapshot
                self._cal_seen = self._calibration_seen_snapshot
                self._paint_calibration()
                self.calibration_table.setRowCount(0)
                if self._cal_seen:
                    self._fill_calibration(self._cal_seen)
            else:
                self._fill_calibration(result["seen"])
            self._calibration_active = False
            self.calibration_button.setEnabled(True)
            self.calibration_save_button.setEnabled(False)
            self.calibration_cancel_button.setEnabled(False)
            if decision == "save":
                successful = sum(
                    bool(row["status"] and row["travel"] > 0 and row["travel_max"] > 0)
                    for row in result["seen"].values()
                )
                self.status.setText(
                    f"saved calibration for {successful} key(s)" if successful else "no keys changed; saved calibration preserved"
                )
            else:
                self.status.setText("calibration cancelled; saved calibration preserved")
        else:
            self.status.setText(str(result))
            if name in ("write", "socd", "restore", "macro"):
                self.refresh()

    def _on_failed(self, name: str, message: str) -> None:
        if name == "calibrate":
            self._calibration_active = False
            self.cal_colors = self._calibration_color_snapshot
            self._cal_seen = self._calibration_seen_snapshot
            self._paint_calibration()
            self.calibration_table.setRowCount(0)
            if self._cal_seen:
                self._fill_calibration(self._cal_seen)
            self.calibration_save_button.setEnabled(False)
            self.calibration_cancel_button.setEnabled(False)
        self.calibration_button.setEnabled(True)
        self.status.setText(f"{name} failed")
        QMessageBox.warning(self, "Fighting 68", message)


def run(vid: int, pid: int) -> None:
    app = QApplication([])
    window = Window(vid, pid)
    window.show()
    app.exec()
