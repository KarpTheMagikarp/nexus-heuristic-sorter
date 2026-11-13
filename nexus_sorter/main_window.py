from __future__ import annotations

import re
import datetime
from contextlib import contextmanager
from typing import Any, Iterator

import maya.cmds as cmds
from .qt_compat import QtWidgets, QtCore, QtGui
from maya.app.general.mayaMixin import MayaQWidgetDockableMixin

from .config import logger
from .hierarchy import MayaUndoChunk
from .naming import NamingResolver
from .categorization import CategorizationEngine, Confidence, AssignmentSource
from .batch_processor import NexusBatchProcessor
from .lifecycle import is_stray_category_folder
from .delegates import DestinationDelegate, NexusCheckboxStyle, AssetCheckDelegate
from .schema import PlanItem


class _WrappingCheckboxLabel(QtWidgets.QLabel):
    """toggles a paired checkbox on left-click. has to be a real subclass,
    not a monkeypatched mousePressEvent - Qt dispatches events through
    the vtable so a patched instance method just never fires"""
    def __init__(self, text: str, checkbox: QtWidgets.QCheckBox, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._checkbox = checkbox
        self.setWordWrap(True)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton:
            self._checkbox.toggle()
        super().mousePressEvent(event)


class NexusSorterUI(MayaQWidgetDockableMixin, QtWidgets.QMainWindow):
    # tooltip for every asset row, set once in run_analysis
    ROW_TOOLTIP = "Double-click to frame item in 3D viewport • Right-click, or click the ▼ on Destination, to reassign category."

    # fixed pixel buffer added to column widths to prevent text clipping
    COLUMN_BREATHING_ROOM = 18
    MAX_ASSET_COLUMN_WIDTH = 220

    # sections with this many rows or fewer open by default, so a small
    # manual-review pile or candidate list doesn't need an extra click to
    # see. bigger sections stay collapsed, a wall of hundreds of rows on
    # first Analyze is a worse experience than one click to expand it
    AUTO_EXPAND_THRESHOLD = 20

    # shown on the deprecated folder candidates section, main thing an
    # artist needs to know: nothing gets deleted here, only moved
    DEPRECATED_INFO_TEXT = (
        "Items checked here are moved to a safe holding folder and are "
        "NOT deleted from your scene."
    )

    DESTINATION_CHOICES = [
        ("Characters", "GRP_01_CHARACTERS"),
        ("Props", "GRP_02_PROPS"),
        ("Environment", "GRP_03_ENVIRONMENT"),
        ("VFX", "GRP_04_VFX"),
        ("Lighting", "GRP_05_LIGHTING"),
        ("Unassigned", "UNASSIGNED_ASSETS"),
    ]
    # reverse of the above, so the tree can show "Characters" instead of
    # the raw group name. GRP_DEPRECATED_FOLDERS isn't a real destination (can't
    # be reassigned to) but gets a friendly label too for clarity and raw was 
    # clipping the column
    _DESTINATION_LABELS = {folder_name: label for label, folder_name in DESTINATION_CHOICES}
    _DESTINATION_LABELS[NexusBatchProcessor.DEPRECATED_HOLDING_GROUP] = "Deprecated Folder"

    @classmethod
    def _display_destination(cls, destination: str) -> str:
        return cls._DESTINATION_LABELS.get(destination, destination)

    # small palette on purpose, tried 5 colors first and it just looked busy
    COLOR_GOOD = QtGui.QColor(142, 196, 147)     # high confidence
    COLOR_WARN = QtGui.QColor(224, 168, 96)      # low confidence
    COLOR_BAD = QtGui.QColor(214, 120, 120)      # actual errors only
    COLOR_NEUTRAL = QtGui.QColor(190, 190, 190)
    # not COLOR_GOOD on purpose, a reassigned row reads as "staged" not "done"
    COLOR_STAGED = QtGui.QColor(114, 170, 224)
    # COLOR_WARN as a row background, only for stray folder rows
    COLOR_STRAY_BG = QtGui.QColor(224, 168, 96, 40)
    # brighter than COLOR_BAD, reserved for the purge drawer's permanent-delete warning
    COLOR_PURGE_WARNING = QtGui.QColor(255, 68, 68)

    # per-column text colors, independent of confidence (that's a row background now)
    COLOR_TEXT_ASSET = QtGui.QColor(0xA9, 0xA9, 0xA9)
    COLOR_TEXT_PROPOSED = QtGui.QColor(0x88, 0xC0, 0xD0)
    COLOR_TEXT_DESTINATION = QtGui.QColor(0xA3, 0xBE, 0x8C)
    COLOR_TEXT_STATUS_READY = QtGui.QColor(0x2E, 0xCC, 0x71)
    COLOR_TEXT_STATUS_NEEDS_FREEZE = QtGui.QColor(0xFF, 0x6B, 0x6B)

    def _confidence_color(self, confidence: Confidence) -> QtGui.QColor | None:
        # medium gets no special color on purpose, it's the normal case
        if confidence == Confidence.HIGH:
            return self.COLOR_GOOD
        if confidence == Confidence.LOW:
            return self.COLOR_WARN
        return None

    def _confidence_background(self, confidence: Confidence) -> QtGui.QColor | None:
        """same HIGH/LOW signal as _confidence_color but as a translucent
        row background instead of text color, reuses the same RGB rather
        than picking a second color"""
        base = self._confidence_color(confidence)
        if base is None:
            return None
        return QtGui.QColor(base.red(), base.green(), base.blue(), 40)

    @staticmethod
    def _build_wrapping_checkbox_row(text: str) -> tuple[QtWidgets.QWidget, QtWidgets.QCheckBox]:
        """QCheckBox can't wrap its own text, long labels just clip in a
        narrow window instead. splits into a bare checkbox + a wrapping
        label, returns the real checkbox so isChecked/setChecked/signals
        elsewhere don't need to change at all"""
        container = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(3)
        checkbox = QtWidgets.QCheckBox()
        label = _WrappingCheckboxLabel(text, checkbox)
        row_layout.addWidget(checkbox, 0, QtCore.Qt.AlignTop)
        row_layout.addWidget(label, 1)
        return container, checkbox

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NexusAutoSorterUI")
        self.setWindowTitle("Nexus Sorter")
        self.resize(680, 640)
        # floor so a docked panel can't break (headers cut mid-word, buttons
        # overlapping), not the same as the default floating width. used to
        # be pinned to purge_confirm_checkbox's unwrappable text, that's
        # gone now with the wrapping checkboxes, could probably go lower -
        # still just an estimate, worth eyeballing live
        self.setMinimumWidth(600)
        self.setAttribute(QtCore.Qt.WA_DeleteOnClose)  # actually frees the widget on close, paired with closeEvent()

        self.engine = NexusBatchProcessor()
        self.current_execution_plan = []
        self.manual_group = None
        self.skipped_group = None
        self.stray_folder_group = None
        self.stray_select_all_checkbox = None
        self.stray_select_all_container = None
        self._selected_scope_seeded = False  # seeded from viewport once per tree, see _apply_scope_checkboxes

        main_widget = QtWidgets.QWidget()
        self.setCentralWidget(main_widget)
        layout = QtWidgets.QVBoxLayout(main_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        top_layout = QtWidgets.QHBoxLayout()
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.addItems(["Selected Assets", "All Assets"])
        self.scope_combo.setCurrentIndex(1)  # defaults to All Assets, index 0 checks elsewhere depend on this order
        self.scope_combo.currentIndexChanged.connect(self._on_scope_changed)

        self.force_combo = QtWidgets.QComboBox()
        self.force_combo.addItems(["Auto-Detect", "01_CHARACTERS", "02_PROPS", "03_ENVIRONMENT", "04_VFX", "05_LIGHTING"])
        # lets Override double as a bulk-reassign shortcut for whatever
        # the current scope considers in play, not just a pre-Analyze setting
        self.force_combo.currentIndexChanged.connect(self._on_force_override_changed)

        top_layout.addWidget(QtWidgets.QLabel("Scope:"))
        top_layout.addWidget(self.scope_combo)
        top_layout.addWidget(QtWidgets.QLabel("Override:"))
        top_layout.addWidget(self.force_combo)
        top_layout.addStretch()
        layout.addLayout(top_layout)

        self.search_field = QtWidgets.QLineEdit()
        self.search_field.setPlaceholderText("Filter assets...")
        # debounced, only walks the tree once typing pauses instead of every keystroke
        self._filter_timer = QtCore.QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._apply_pending_filter)
        self.search_field.textChanged.connect(self._on_filter_text_changed)
        layout.addWidget(self.search_field)
        self._column_resize_timer = QtCore.QTimer(self)
        self._column_resize_timer.setSingleShot(True)
        self._column_resize_timer.setInterval(50)
        self._column_resize_timer.timeout.connect(self._finalize_column_widths)

        self.preview_tree = QtWidgets.QTreeWidget()
        self.preview_tree.setHeaderLabels(["Asset", "Status", "Proposed", "Destination"])
        self.preview_tree.setAlternatingRowColors(True)
        self.preview_tree.setIndentation(12)
        self.preview_tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)  # ctrl/shift click, not just single-select
        self.preview_tree.setColumnWidth(0, 170)  # Asset
        self.preview_tree.setColumnWidth(1, 170)  # Status, prioritized so it's not the first thing cut in a narrow view
        self.preview_tree.setColumnWidth(2, 150)  # Proposed
        self.preview_tree.setColumnWidth(3, 140)  # Destination

        # kept as real attributes or PySide garbage collects them and the
        # column just silently stops responding, no error either
        self._destination_delegate = DestinationDelegate(self, self.preview_tree)
        self.preview_tree.setItemDelegateForColumn(3, self._destination_delegate)
        self._asset_check_delegate = AssetCheckDelegate(self, self.preview_tree)
        self.preview_tree.setItemDelegateForColumn(0, self._asset_check_delegate)
        self._checkbox_style = NexusCheckboxStyle(self.preview_tree.style(), self.COLOR_STAGED)
        self.preview_tree.setStyle(self._checkbox_style)

        # everything goes through _on_item_clicked instead of native edit
        # triggers, or Qt's DoubleClicked handling fights with double-click-to-frame
        self.preview_tree.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.preview_tree.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.preview_tree.customContextMenuRequested.connect(self._show_reassign_menu)
        self.preview_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.preview_tree.itemClicked.connect(self._on_item_clicked)
        self.preview_tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.preview_tree.itemExpanded.connect(self._on_tree_item_expanded)
        self.preview_tree.itemCollapsed.connect(self._on_tree_item_collapsed)
        self.preview_tree.itemChanged.connect(self._on_tree_item_changed)
        self.preview_tree.installEventFilter(self)  # clears row highlight on focus loss, see eventFilter

        layout.addWidget(self.preview_tree)

        self.freeze_checkbox = QtWidgets.QCheckBox(
            "Freeze unfrozen transforms on commit (rigs and skeletons are always exempt)"
        )
        self.freeze_checkbox.setToolTip(
            "Off by default on purpose. Freezing bakes translate/rotate/scale to\n"
            "identity - safe for a plain static prop, but maya will refuse it on\n"
            "anything with an incoming connection (constraints, expressions, anim\n"
            "curves) or on instanced geometry. Rig-shielded assets are always\n"
            "skipped regardless of this checkbox."
        )
        self.freeze_checkbox.toggled.connect(self._on_freeze_checkbox_toggled)
        layout.addWidget(self.freeze_checkbox)

        # this is the "what did you actually do" readout - statusBar text is too
        # easy to miss, artists' eyes are on the tree not the bottom edge of the window
        self.feedback_label = QtWidgets.QLabel("")
        self.feedback_label.setTextFormat(QtCore.Qt.RichText)
        self.feedback_label.setWordWrap(True)
        self.feedback_label.setStyleSheet("padding: 2px 4px;")
        layout.addWidget(self.feedback_label)

        btn_layout = QtWidgets.QHBoxLayout()
        self.scan_btn = QtWidgets.QPushButton("Analyze")
        self.scan_btn.clicked.connect(self.run_analysis)

        self.commit_btn = QtWidgets.QPushButton("Commit Changes")
        self.commit_btn.setEnabled(False)
        self.commit_btn.clicked.connect(self.execute_commit)

        btn_layout.addWidget(self.scan_btn)
        btn_layout.addWidget(self.commit_btn)
        layout.addLayout(btn_layout)

        # purge drawer lives in this same window, collapsed by default.
        # a second popup window felt like overkill for a rare action,
        # and closed-by-default means it costs nothing during normal use
        self.purge_toggle_btn = QtWidgets.QToolButton()
        self.purge_toggle_btn.setText("▸ Advanced: Purge Deprecated Folders")
        self.purge_toggle_btn.setCheckable(True)
        self.purge_toggle_btn.setChecked(False)
        self.purge_toggle_btn.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        self.purge_toggle_btn.setStyleSheet("QToolButton { border: none; text-align: left; padding: 4px 0; }")
        self.purge_toggle_btn.toggled.connect(self._on_purge_drawer_toggled)
        layout.addWidget(self.purge_toggle_btn)

        self.purge_drawer = QtWidgets.QWidget()
        self.purge_drawer.setVisible(False)  # collapsed by default - see above
        drawer_layout = QtWidgets.QVBoxLayout(self.purge_drawer)
        drawer_layout.setContentsMargins(16, 4, 4, 4)  # indented under the disclosure header
        # spacing between rows, without it the colored status lines mashed together
        drawer_layout.setSpacing(10)

        threshold_row = QtWidgets.QHBoxLayout()
        threshold_row.addWidget(QtWidgets.QLabel("Purge folders quarantined more than:"))
        self.purge_threshold_spin = QtWidgets.QSpinBox()
        self.purge_threshold_spin.setRange(0, 3650)
        self.purge_threshold_spin.setSuffix(" day(s)")
        # same getter execute_purge uses, so this always matches what
        # actually governs the purge, never a stale separate number
        self.purge_threshold_spin.setValue(NexusBatchProcessor.get_purge_age_threshold_days())
        threshold_row.addWidget(self.purge_threshold_spin)
        threshold_row.addStretch()
        drawer_layout.addLayout(threshold_row)

        # own full-width row instead of packed next to the spinbox, that
        # was cramming the date text into multiple squished lines
        self.purge_date_label = QtWidgets.QLabel("")
        self.purge_date_label.setTextFormat(QtCore.Qt.RichText)
        self.purge_date_label.setWordWrap(True)
        self.purge_date_label.setStyleSheet("padding: 2px 4px;")
        drawer_layout.addWidget(self.purge_date_label)

        # separate row for the eligibility verdict, same reason as above.
        # hidden (not just empty text) when nothing's eligible, see
        # _update_purge_date_label - an empty-but-visible label still
        # reserves its own row plus spacing on both sides of it
        self.purge_status_label = QtWidgets.QLabel("")
        self.purge_status_label.setTextFormat(QtCore.Qt.RichText)
        self.purge_status_label.setWordWrap(True)
        self.purge_status_label.setStyleSheet("padding: 2px 4px;")
        self.purge_status_label.setVisible(False)
        drawer_layout.addWidget(self.purge_status_label)

        # skips the age check entirely (None), not the same as setting
        # threshold to 0, a broken/missing date would survive threshold=0
        # but None truly means every folder, no exceptions
        bypass_row, self.purge_bypass_checkbox = self._build_wrapping_checkbox_row(
            "Bypass threshold and delete all backups immediately"
        )
        self.purge_bypass_checkbox.setChecked(False)
        self.purge_bypass_checkbox.toggled.connect(self._on_purge_bypass_toggled)
        drawer_layout.addWidget(bypass_row)

        # replaces a separate confirmation popup, reset to unchecked after
        # every purge so re-arming it takes a deliberate click each time
        confirm_row, self.purge_confirm_checkbox = self._build_wrapping_checkbox_row(
            "I understand this permanently deletes these folders - this is not another quarantine step."
        )
        self.purge_confirm_checkbox.setChecked(False)
        self.purge_confirm_checkbox.stateChanged.connect(self._update_purge_button_state)
        drawer_layout.addWidget(confirm_row)

        self.purge_execute_btn = QtWidgets.QPushButton("Purge Old Backups")
        self.purge_execute_btn.setEnabled(False)  # needs eligible folders AND the checkbox above
        self.purge_execute_btn.clicked.connect(self.execute_purge)
        drawer_layout.addWidget(self.purge_execute_btn)

        layout.addWidget(self.purge_drawer)

        # same debounce idea as the filter timer, no reason to rescan on
        # every spinbox tick. date label above updates instantly though,
        # that's cheap local math with no scene access
        self._purge_preview_timer = QtCore.QTimer(self)
        self._purge_preview_timer.setSingleShot(True)
        self._purge_preview_timer.setInterval(150)
        self._purge_preview_timer.timeout.connect(self._refresh_purge_preview)
        self.purge_threshold_spin.valueChanged.connect(self._update_purge_date_label)
        self.purge_threshold_spin.valueChanged.connect(lambda _val: self._purge_preview_timer.start())

        # execute_purge acts on exactly this manifest, never a fresh rescan
        self._last_purge_manifest = None
        self._update_purge_date_label()  # free to compute, might as well do it now

        self.statusBar().showMessage("Ready")
        self._set_feedback("Run Analyze to preview a sorting plan.", self.COLOR_NEUTRAL)

    def _set_feedback(self, text: str, color: QtGui.QColor | None = None) -> None:
        # QLabel has no "color just this message" API, rich text is the easiest way there
        if color:
            self.feedback_label.setText(f'<span style="color: {color.name()};">{text}</span>')
        else:
            self.feedback_label.setText(text)

    def _on_scope_changed(self, index: int) -> None:
        # forgetting to select something first makes this look identical to
        # All Assets - same full tree, just nothing checked, you won't know
        # until Commit says there's nothing staged
        if index == 0:
            self.statusBar().showMessage("Pre-checks whatever's currently selected in the viewport.")
        else:
            self.statusBar().clearMessage()

        # scope change only needs checkboxes added/removed, not a fresh
        # analyze, used to force a full re-analyze which threw away good data
        if self.preview_tree.topLevelItemCount() > 0:
            self._apply_scope_checkboxes(select_scope_active=(index == 0))

    def _apply_scope_checkboxes(self, select_scope_active: bool) -> None:
        """adds/removes the selected-assets checkbox on ordinary rows,
        leaves stray candidates and skipped rows alone. only seeds from
        viewport selection the FIRST time (see _selected_scope_seeded),
        after that a scope round-trip preserves whatever got manually
        checked instead of re-seeding over it."""
        first_seed = select_scope_active and not self._selected_scope_seeded
        selected_paths = set(cmds.ls(sl=True, long=True) or []) if first_seed else set()

        # row.data() mutation doesn't reliably persist back to the item,
        # so build a lookup into current_execution_plan instead, that's
        # the one source everything else in this file already trusts
        plan_by_path = {
            d.get("long_path"): d for d in self.current_execution_plan if d.get("long_path")
        }

        for i in range(self.preview_tree.topLevelItemCount()):
            top = self.preview_tree.topLevelItem(i)
            if top is self.stray_folder_group or top is self.skipped_group:
                continue
            for j in range(top.childCount()):
                row = top.child(j)
                data = row.data(0, QtCore.Qt.UserRole)
                if not data:
                    continue
                # long_path itself is safe to read off the row directly,
                # only _included needs the plan_by_path lookup
                plan_data = plan_by_path.get(data.get("long_path"), data)
                if select_scope_active:
                    row.setFlags(row.flags() | QtCore.Qt.ItemIsUserCheckable)
                    if first_seed:
                        included = self._was_selected(plan_data.get("long_path"), selected_paths)
                        row.setCheckState(0, QtCore.Qt.Checked if included else QtCore.Qt.Unchecked)
                        plan_data["_included"] = included
                    else:
                        # restored from plan_data, not row.data(), see above
                        restored = bool(plan_data.get("_included", True))
                        row.setCheckState(0, QtCore.Qt.Checked if restored else QtCore.Qt.Unchecked)
                else:
                    row.setFlags(row.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                    # _included stays untouched on purpose, that's what a
                    # later switch back restores from. CheckStateRole data
                    # gets cleared here directly instead of relying on every
                    # future paint() call to suppress it
                    row.setData(0, QtCore.Qt.CheckStateRole, None)

        if first_seed:
            self._selected_scope_seeded = True

        # this used to skip the width cap and let col 3 get crushed on
        # scope switch (stretch fills whatever col 0 leaves). just route
        # through the real function now
        self._finalize_column_widths()

    def _on_selection_changed(self) -> None:
        # no-op now, tooltip handles the hint instead of the status bar
        return

    def _toggle_checked(self, items: list[QtWidgets.QTreeWidgetItem]) -> None:
        """toggles every checkable row in items as one batch, shared by
        the space shortcut and click-to-toggle below. unchecks only if
        EVERY row is already checked, otherwise checks all, doesn't flip
        each row independently or a mixed batch gets unpredictable."""
        checkable = [it for it in items if it.childCount() == 0
                     and (it.flags() & QtCore.Qt.ItemIsUserCheckable)]
        if not checkable:
            return
        all_checked = all(it.checkState(0) == QtCore.Qt.Checked for it in checkable)
        new_state = QtCore.Qt.Unchecked if all_checked else QtCore.Qt.Checked
        for it in checkable:
            it.setCheckState(0, new_state)

    def _on_item_clicked(self, item: QtWidgets.QTreeWidgetItem | None, column: int) -> None:
        # column 0 checkable rows. plain click toggles this row (or the
        # whole selection if part of one). shift/ctrl click always CHECKS
        # rather than toggling, growing a selection shouldn't risk
        # unchecking what you already had (space still handles that)
        if (column == 0 and item is not None and item.childCount() == 0
                and (item.flags() & QtCore.Qt.ItemIsUserCheckable)):
            modifiers = QtWidgets.QApplication.keyboardModifiers()
            if modifiers == QtCore.Qt.NoModifier:
                selected = self.preview_tree.selectedItems()
                targets = selected if (item in selected and len(selected) > 1) else [item]
                self._toggle_checked(targets)
            elif modifiers & (QtCore.Qt.ShiftModifier | QtCore.Qt.ControlModifier):
                # selectedItems() already reflects this click's result,
                # Qt updates selection on mouse press before this fires
                for it in self.preview_tree.selectedItems():
                    if it.childCount() == 0 and (it.flags() & QtCore.Qt.ItemIsUserCheckable):
                        it.setCheckState(0, QtCore.Qt.Checked)
            return

        # column 3's arrow click is now handled directly in
        # DestinationDelegate.editorEvent, off the real mouse event
        # instead of reconstructing click position from QCursor.pos()
        # after the fact - that reconstruction was the actual bug,
        # confirmed with a real click simulation: itemClicked always
        # fired, but the position check almost never passed

    def _on_item_double_clicked(self, item: QtWidgets.QTreeWidgetItem | None, column: int) -> None:
        # only explicit action allowed to touch viewport selection,
        # everything else in analysis stays passive
        if item is None or item.childCount() > 0:
            return
        if column == 3:
            # this column already has its own single-click interaction -
            # the arrow opens a dropdown (see DestinationDelegate /
            # _on_item_clicked). a fast double-click while using that
            # dropdown shouldn't ALSO yank the viewport to a different
            # object mid-interaction, so this column opts out of framing
            return
        data = item.data(0, QtCore.Qt.UserRole)
        if not data:
            return
        long_path = data.get("long_path")
        if not long_path or not cmds.objExists(long_path):
            self.statusBar().showMessage("That object no longer exists in the scene.")
            return
        cmds.select(long_path, replace=True)
        try:
            cmds.viewFit()  # frames current selection in the viewport
        except RuntimeError:
            pass

    def _show_reassign_menu(self, pos: QtCore.QPoint) -> None:
        item = self.preview_tree.itemAt(pos)
        if item is None or item.childCount() > 0:
            return  # header/group row - nothing to reassign

        data = item.data(0, QtCore.Qt.UserRole)
        if not data or data.get("source") == AssignmentSource.SKIPPED_REFERENCED:
            # no menu, execute_sorting_pass refuses locked/referenced nodes anyway
            return

        menu = QtWidgets.QMenu(self.preview_tree)
        for label, folder_name in self.DESTINATION_CHOICES:
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(data["destination"] == folder_name)
            action.triggered.connect(
                lambda checked, it=item, d=data, f=folder_name: self._reassign_destination_for_selection(it, d, f)
            )
        # PySide2 (Maya 2022-2024) only has exec_, PySide6 renamed it to exec
        run_menu = getattr(menu, "exec", None) or menu.exec_
        run_menu(self.preview_tree.viewport().mapToGlobal(pos))

    def _reassign_destination_for_selection(
        self,
        primary_item: QtWidgets.QTreeWidgetItem,
        primary_data: PlanItem,
        folder_name: str,
    ) -> None:
        """reassigns every editable row in the current multi-selection if
        the triggering row is part of one, otherwise just that one row"""
        selected = [
            (it, it.data(0, QtCore.Qt.UserRole)) for it in self.preview_tree.selectedItems()
            if it.childCount() == 0 and it.data(0, QtCore.Qt.UserRole)
            and it.data(0, QtCore.Qt.UserRole).get("source") != AssignmentSource.SKIPPED_REFERENCED
        ]
        selected_items = [pair[0] for pair in selected]
        targets = selected if (primary_item in selected_items and len(selected) > 1) else [(primary_item, primary_data)]

        for it, d in targets:
            self._reassign_destination(it, d, folder_name)

        # "· Pending Commit" (and a possibly longer re-derived name) can
        # push a cell past its current column width - same debounced
        # remeasure resizeEvent already uses, not a fresh one-off
        self._column_resize_timer.start()

        if len(targets) > 1:
            self.statusBar().showMessage(f"Staged {len(targets)} selected assets \u2192 {folder_name}.")

    def _on_force_override_changed(self, _index: int) -> None:
        text = self.force_combo.currentText()
        if text == "Auto-Detect":
            self._apply_auto_detect_to_scope_targets()
            return
        folder_name = NexusBatchProcessor.FORCE_CATEGORY_MAP.get(text)
        if not folder_name:
            return
        self._apply_override_to_scope_targets(folder_name)

    def _apply_auto_detect_to_scope_targets(self) -> None:
        """restores whichever rows the current scope considers "in play"
        back to whatever real anatomy/name detection says right now,
        undoing any earlier manual override. genuinely different from
        _apply_override_to_scope_targets: every OTHER Override option is
        one fixed folder for the whole batch, but "what would auto-detect
        say" is its own separate answer for every individual row, so
        each target gets its own fresh analyze_asset_dna call rather
        than one shared folder_name.

        shielded assets are skipped entirely - shielding was decided
        once at Analyze time and determines which node the row even
        represents (the whole rig root, not just this object). safely
        redoing that live would mean re-grouping the tree itself, not
        just changing where one existing row points, so it's out of
        scope for a per-row revert like this one."""
        select_scope_active = (self.scope_combo.currentIndex() == 0)

        targets = []
        for i in range(self.preview_tree.topLevelItemCount()):
            top = self.preview_tree.topLevelItem(i)
            if top is self.stray_folder_group or top is self.skipped_group:
                continue
            for j in range(top.childCount()):
                row = top.child(j)
                data = row.data(0, QtCore.Qt.UserRole)
                if not data or data.get("source") == AssignmentSource.SKIPPED_REFERENCED:
                    continue
                if not select_scope_active or row.checkState(0) == QtCore.Qt.Checked:
                    targets.append((row, data))

        if not targets:
            self.statusBar().showMessage("Check the assets you want, then pick Auto-Detect.")
            return

        reverted = 0
        for it, d in targets:
            if d.get("is_shielded"):
                continue
            if not cmds.objExists(d["long_path"]):
                continue
            dest, confidence, _needs_shield = CategorizationEngine.analyze_asset_dna(d["long_path"], d["original"])
            self._reassign_destination(it, d, dest, source=AssignmentSource.AUTO_DETECTED, confidence=confidence)
            reverted += 1
        self._column_resize_timer.start()

        self.statusBar().showMessage(f"Reverted {reverted} asset(s) to their auto-detected category.")

    def _apply_override_to_scope_targets(self, folder_name: str) -> None:
        """bulk-reassigns whichever rows the CURRENT SCOPE considers "in
        play" - checked rows under Selected Assets (real per-row
        curation), or every eligible row under All Assets, since there's
        no checkbox there at all and "everything" is the only coherent
        meaning - same rule _checked_asset_long_paths already applies
        for what gets committed, just reused here for what gets
        overridden. deliberately NOT based on tree highlight/selection -
        that's what shift/ctrl-click uses to batch a checkbox toggle in
        one go, but it's also exactly what clicking into this combo box
        immediately clears (see eventFilter's FocusOut handling)."""
        select_scope_active = (self.scope_combo.currentIndex() == 0)

        targets = []
        for i in range(self.preview_tree.topLevelItemCount()):
            top = self.preview_tree.topLevelItem(i)
            if top is self.stray_folder_group or top is self.skipped_group:
                continue
            for j in range(top.childCount()):
                row = top.child(j)
                data = row.data(0, QtCore.Qt.UserRole)
                if not data or data.get("source") == AssignmentSource.SKIPPED_REFERENCED:
                    continue
                if not select_scope_active or row.checkState(0) == QtCore.Qt.Checked:
                    targets.append((row, data))

        if not targets:
            self.statusBar().showMessage("Check the assets you want, then pick an override.")
            return

        for it, d in targets:
            self._reassign_destination(it, d, folder_name)
        self._column_resize_timer.start()

        self.statusBar().showMessage(f"Staged {len(targets)} asset(s) → {folder_name}.")

    def _reassign_destination(
        self,
        item: QtWidgets.QTreeWidgetItem,
        data: PlanItem,
        folder_name: str,
        source: AssignmentSource = AssignmentSource.USER_FORCED,
        confidence: Confidence = Confidence.HIGH,
    ) -> None:
        # data IS the same dict object stored on the row (not a copy) so
        # mutating it already reaches current_execution_plan, the explicit
        # re-sync below is just a second guarantee in case that ever changes
        # defaults preserve every existing caller's behavior (a manual
        # pick is always a USER_FORCED/HIGH override) - only the new
        # Auto-Detect revert path passes real values through, since
        # calling that one "User Override" would be backwards - it's
        # literally UN-overriding the row, not overriding it again
        data["destination"] = folder_name
        data["source"] = source
        data["confidence"] = confidence

        # rig-shielded roots never get prefix surgery in the first place
        # (sanitize_name_strict doesn't take a destination), nothing to
        # re-derive there. everything else gets re-run through the same
        # naming pipeline analyze_sorting_plan itself uses, so a
        # reassigned Prop doesn't sit in GRP_03_ENVIRONMENT still wearing
        # a Pr_ prefix. checked against the same two pools the engine
        # checks: what's really already in that folder in the scene,
        # plus anything else in this not-yet-committed plan already
        # headed there (so two reassigns to the same folder in one
        # batch still can't collide with each other either)
        if not data.get("is_shielded"):
            sibling_names = NexusBatchProcessor._existing_children_names(folder_name)
            sibling_names.update(
                plan_item["proposed"] for plan_item in self.current_execution_plan
                if plan_item.get("destination") == folder_name
                and plan_item.get("long_path") != data.get("long_path")
            )
            data["proposed"] = NamingResolver.get_stable_name(
                raw_current_name=data["original"],
                sibling_names=sibling_names,
                name_deriver=lambda canonical: CategorizationEngine.derive_proposed_name(canonical, folder_name),
            )

        for plan_item in self.current_execution_plan:
            if plan_item.get("long_path") == data.get("long_path"):
                plan_item["destination"] = folder_name
                plan_item["source"] = source
                plan_item["confidence"] = confidence
                plan_item["proposed"] = data["proposed"]
                break

        item.setText(2, data["proposed"])
        item.setText(3, self._display_destination(folder_name))
        # refreshes Ready/Needs Freeze text AND coloring, not just text - a
        # reassignment can move a row into or out of a freeze-blocked
        # category, and without this a formerly amber row could keep
        # showing stale amber even after its status text correctly
        # flips to "Ready"
        self._apply_freeze_status_style(item, data)
        # destination only, not the whole row - that's the actual thing
        # that changed, coloring all 4 columns buried the real signal
        item.setForeground(3, QtGui.QBrush(self.COLOR_STAGED))

        self.statusBar().showMessage(f"Staged {data['original']} → {folder_name} as {data['proposed']}.")

    def _status_text_for(self, data: PlanItem) -> str:
        # only claims a shield when one's actually on. used to say "Rig
        # Shielded" for anything in characters/vfx, shield or not.
        # shielded stuff never gets frozen, so Needs Freeze would be a lie
        if data.get("is_shielded"):
            return "Ready · Rig Shield"
        # characters/vfx never show Needs Freeze, baking those breaks rigs
        if CategorizationEngine.is_freeze_blocked(data["destination"]):
            return "Ready"
        # checking "freeze on commit" resolves an unfrozen transform
        # automatically at commit time (see execute_commit's
        # freeze_eligible pass-through) - showing amber "Needs Freeze"
        # here anyway would be flagging a problem the tool's already
        # been told to go fix by itself
        return "Ready" if (data["is_frozen"] or self.freeze_checkbox.isChecked()) else "Needs Freeze"

    def _apply_freeze_status_style(self, item: QtWidgets.QTreeWidgetItem, data: PlanItem) -> None:
        """(re)applies the Ready-vs-Needs-Freeze text and coloring for one
        main-group row. pulled out on its own so toggling the freeze-on-
        commit checkbox can refresh every already-displayed row live
        (see _on_freeze_checkbox_toggled) using the exact same styling
        run_analysis itself uses on first build - resets both directions
        explicitly (font weight, background), since a live refresh has
        to be able to undo the OTHER branch's styling too, not just
        layer its own on top of whatever's already sitting there."""
        status_text = self._status_text_for(data)
        item.setText(1, status_text)
        if status_text.startswith("Ready"):
            item.setForeground(0, QtGui.QBrush(self.COLOR_TEXT_ASSET))
            item.setForeground(1, QtGui.QBrush(self.COLOR_TEXT_STATUS_READY))
            item.setForeground(2, QtGui.QBrush(self.COLOR_TEXT_PROPOSED))
            item.setForeground(3, QtGui.QBrush(self.COLOR_TEXT_DESTINATION))
            status_font = item.font(1)
            status_font.setBold(False)
            item.setFont(1, status_font)
            row_bg = self._confidence_background(data["confidence"])
            for col in range(4):
                item.setBackground(col, QtGui.QBrush(row_bg) if row_bg is not None else QtGui.QBrush())
        elif "Needs Freeze" in status_text:
            for col in (0, 2, 3):
                item.setForeground(col, QtGui.QBrush(self.COLOR_WARN))
            item.setForeground(1, QtGui.QBrush(self.COLOR_TEXT_STATUS_NEEDS_FREEZE))
            status_font = item.font(1)
            status_font.setBold(True)
            item.setFont(1, status_font)
            for col in range(4):
                item.setBackground(col, QtGui.QBrush())

    def _on_freeze_checkbox_toggled(self, _checked: bool) -> None:
        """live-refreshes every already-displayed asset row's Ready/Needs
        Freeze text and coloring the instant this checkbox changes,
        instead of leaving stale amber warnings on rows the commit is
        now going to freeze automatically anyway. only touches the main
        per-category groups - manual/skipped/stray rows never showed
        freeze status to begin with."""
        for i in range(self.preview_tree.topLevelItemCount()):
            top = self.preview_tree.topLevelItem(i)
            if top is self.manual_group or top is self.skipped_group or top is self.stray_folder_group:
                continue
            for j in range(top.childCount()):
                row = top.child(j)
                data = row.data(0, QtCore.Qt.UserRole)
                if not data:
                    continue
                self._apply_freeze_status_style(row, data)

    @contextmanager
    def _ui_busy(self) -> Iterator[None]:
        """disables Analyze/Commit/Purge widgets for the block so a
        second click can't start a second scene-mutating op mid-run.
        drawer toggle stays enabled (just visibility), preview_tree stays
        enabled too (selection/inspect don't touch scene topology).

        exit state is left to the caller, not restored here, since
        "correct" differs per caller (execute_commit wants it to stay
        disabled after success but re-enable on failure, execute_purge
        wants it back to whatever it was). plain try/finally so controls
        never get stuck disabled if something raises unexpectedly."""
        self.scan_btn.setEnabled(False)
        self.commit_btn.setEnabled(False)
        self.purge_threshold_spin.setEnabled(False)
        self.purge_confirm_checkbox.setEnabled(False)
        self.purge_execute_btn.setEnabled(False)
        QtWidgets.QApplication.processEvents()
        try:
            yield
        finally:
            self.scan_btn.setEnabled(True)
            # NOT an unconditional True - if the bypass checkbox is
            # checked, the spinbox is supposed to stay disabled even
            # once this busy window ends (see _on_purge_bypass_toggled,
            # which owns that rule everywhere else)
            self.purge_threshold_spin.setEnabled(not self.purge_bypass_checkbox.isChecked())
            self.purge_confirm_checkbox.setEnabled(True)
            self._update_purge_button_state()

    def _on_purge_drawer_toggled(self, expanded: bool) -> None:
        self.purge_toggle_btn.setText(
            ("▾ " if expanded else "▸ ") + "Advanced: Purge Deprecated Folders"
        )
        self.purge_drawer.setVisible(expanded)
        if expanded:
            self._refresh_purge_preview()

    def _current_purge_threshold(self) -> int | float | None:
        """resolves older_than_days from the bypass checkbox + spinbox,
        one place so the preview and the actual purge can't disagree.
        None (not 0) for a true bypass, see the checkbox setup for why."""
        if self.purge_bypass_checkbox.isChecked():
            return None
        return self.purge_threshold_spin.value()

    def _on_purge_bypass_toggled(self, checked: bool) -> None:
        self.purge_threshold_spin.setEnabled(not checked)
        self._update_purge_date_label()
        self._refresh_purge_preview()

    def _update_purge_date_label(self, *_args: Any, eligible_count: int | None = None) -> None:
        """builds the html for the drawer's cutoff-date line and the
        eligibility verdict line. called twice: instantly from the
        spinbox/checkbox (eligible_count=None, only updates the date
        line since counting needs a scene scan) and again from
        _refresh_purge_preview once the real count is known.

        eligible_count is keyword-only since Qt's valueChanged(int)
        passes a positional int that would otherwise get mistaken for
        the count. spells out the year since the spinbox goes up to
        3650 days and a bare "Oct 24" gets ambiguous. avoids the
        %-d/%#d leading-zero flag since it's not portable across OSes."""
        red = self.COLOR_PURGE_WARNING.name()
        gold = self.COLOR_WARN.name()

        if self.purge_bypass_checkbox.isChecked():
            date_html = (
                f'<span style="color:{red}; font-weight:bold;">'
                f"Affecting ALL files, including today's archives</span>"
            )
        else:
            threshold_days = self.purge_threshold_spin.value()
            cutoff = datetime.date.today() - datetime.timedelta(days=threshold_days)
            cutoff_str = f"{cutoff.strftime('%b')} {cutoff.day}, {cutoff.year}"
            date_html = (
                f'<span style="color:{red};">Affecting files quarantined on or before:</span> '
                f'<span style="color:{gold}; font-weight:bold;">{cutoff_str}</span>'
            )
        self.purge_date_label.setText(date_html)

        if eligible_count is not None:
            if eligible_count > 0:
                status_html = (
                    f'<span style="color:{red}; font-weight:bold;">'
                    f"{eligible_count} folder(s) will be PERMANENTLY DELETED</span>"
                )
                self.purge_status_label.setText(status_html)
                self.purge_status_label.setVisible(True)
            else:
                # hidden, not just cleared - an empty-but-visible label
                # still reserves its row and the spacing on both sides of it
                self.purge_status_label.setVisible(False)

    def _refresh_purge_preview(self) -> None:
        """recomputes the manifest at the current threshold and updates
        the eligibility label. build_purge_manifest is read-only so this
        is safe to call on every drawer open and settled spinbox change.
        also stores the manifest for execute_purge and refreshes the
        purge button state (confirm checkbox still needs to be ticked too)"""
        threshold_days = self._current_purge_threshold()
        manifest = self.engine.build_purge_manifest(older_than_days=threshold_days)
        self._last_purge_manifest = manifest

        for entry in manifest["eligible"]:
            logger.info(
                f"Purge candidate: {entry['path']} (quarantined "
                f"{entry['quarantined_date'] or 'unknown date'}, "
                f"{entry['descendant_count']} descendant object(s))"
            )

        self._update_purge_date_label(eligible_count=len(manifest["eligible"]))
        self._update_purge_button_state()

    def _update_purge_button_state(self, *_args: Any) -> None:
        """purge is reachable when confirm is ticked AND one of:
        1. a candidate row is checked in the tree, doesn't need bypass
           since that never meant anything for unquarantined rows
        2. the manifest has an eligible already-quarantined folder (or
           bypass clears the age check entirely)

        also updates the button's own label since which case is driving
        it changes what a click actually does. called on every relevant
        signal so ticking a candidate row live-updates this with no
        extra click needed"""
        manifest = self._last_purge_manifest
        has_manifest_eligible = bool(manifest and manifest["eligible"])
        checked_candidates = self._checked_stray_folder_paths()

        if checked_candidates and has_manifest_eligible:
            self.purge_execute_btn.setText(
                f"Purge {len(checked_candidates)} Selected + Old Backups"
            )
        elif checked_candidates:
            n = len(checked_candidates)
            self.purge_execute_btn.setText(f"Purge {n} Selected Folder{'s' if n != 1 else ''}")
        else:
            self.purge_execute_btn.setText("Purge Old Backups")

        can_purge = (has_manifest_eligible or bool(checked_candidates)) and self.purge_confirm_checkbox.isChecked()
        self.purge_execute_btn.setEnabled(can_purge)
        # same red as COLOR_PURGE_WARNING, low-alpha tint like COLOR_STRAY_BG
        # but stronger since a full button needs more alpha to actually show
        # tied to can_purge so the color can never disagree with setEnabled
        if can_purge:
            self.purge_execute_btn.setStyleSheet("background-color: rgba(255, 68, 68, 140);")
        else:
            self.purge_execute_btn.setStyleSheet("")

    def _on_tree_item_expanded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        # only the stray candidates section has an embedded widget,
        # everything else is a no-op. attaches the widget to the cell,
        # not just setVisible, see _on_tree_item_collapsed for why
        if item is self.stray_folder_group and self.stray_select_all_container is not None:
            try:
                self.preview_tree.setItemWidget(self.stray_folder_group, 1, self.stray_select_all_container)
                self.stray_select_all_container.setVisible(True)
            except RuntimeError:
                # a queued expand event from a previous tool instance can
                # still fire after show_ui() tears it down and rebuilds,
                # widget's already gone so nothing to do but drop it quietly
                pass

    def _on_tree_item_collapsed(self, item: QtWidgets.QTreeWidgetItem) -> None:
        # detaches the widget from the cell entirely, not just setVisible,
        # since this checkbox drives a real batch action and needs to be
        # genuinely unreachable when its section isn't open
        if item is self.stray_folder_group and self.stray_select_all_container is not None:
            try:
                self.preview_tree.removeItemWidget(self.stray_folder_group, 1)
            except RuntimeError:
                pass  # same stale-event race as above, nothing to detach

    def _on_tree_item_changed(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        # fires for any item change in the whole tree, branches by which
        # kind of checkable row this is (stray candidates vs asset rows
        # are two totally separate systems that happen to share one tree)
        if self.stray_folder_group is not None and item.parent() is self.stray_folder_group:
            # keeps the master tristate honest, and a freshly ticked
            # candidate can itself make purge reachable under bypass
            self._sync_stray_select_all_state()
            self._update_purge_button_state()
            return

        # otherwise a normal asset row. item.data() mutation doesn't
        # persist, so look it up in current_execution_plan by long_path
        # instead, same pattern as _reassign_destination
        if item.flags() & QtCore.Qt.ItemIsUserCheckable:
            data = item.data(0, QtCore.Qt.UserRole)
            if data is not None:
                long_path = data.get("long_path")
                checked = (item.checkState(0) == QtCore.Qt.Checked)
                for plan_item in self.current_execution_plan:
                    if plan_item.get("long_path") == long_path:
                        plan_item["_included"] = checked
                        break

    def _on_stray_select_all_clicked(self) -> None:
        """only fires on a real user click (connected to clicked, not
        stateChanged, so a programmatic setCheckState never loops back
        here). binary decision: all checked = deselect all, anything
        else = select all. overrides Qt's tristate click-cycling since a
        direct master click landing on PartiallyChecked has no real
        meaning. only ever touches stray_folder_group's own children."""
        if self.stray_folder_group is None:
            return
        child_count = self.stray_folder_group.childCount()
        if child_count == 0:
            return

        all_checked = all(
            self.stray_folder_group.child(i).checkState(0) == QtCore.Qt.Checked
            for i in range(child_count)
        )
        new_state = QtCore.Qt.Unchecked if all_checked else QtCore.Qt.Checked
        for i in range(child_count):
            self.stray_folder_group.child(i).setCheckState(0, new_state)
        # each setCheckState fires itemChanged already, this just guarantees
        # the master reflects the final settled state, not mid-loop
        self._sync_stray_select_all_state()

    def _sync_stray_select_all_state(self) -> None:
        """keeps the master checkbox and its label always computed from
        the children's actual states, never holds its own independent
        state. "Deselect All" only shows when EVERY row is checked,
        both Unchecked and PartiallyChecked show "Select All" since a
        click from either state selects everything. blockSignals just
        guards against a future stateChanged connection looping back here."""
        if self.stray_folder_group is None or self.stray_select_all_checkbox is None:
            return
        child_count = self.stray_folder_group.childCount()
        if child_count == 0:
            return

        checked_count = sum(
            1 for i in range(child_count)
            if self.stray_folder_group.child(i).checkState(0) == QtCore.Qt.Checked
        )
        if checked_count == 0:
            new_state = QtCore.Qt.Unchecked
        elif checked_count == child_count:
            new_state = QtCore.Qt.Checked
        else:
            new_state = QtCore.Qt.PartiallyChecked

        try:
            self.stray_select_all_checkbox.blockSignals(True)
            self.stray_select_all_checkbox.setCheckState(new_state)
            self.stray_select_all_checkbox.setText(
                "Deselect All" if new_state == QtCore.Qt.Checked else "Select All"
            )
            self.stray_select_all_checkbox.blockSignals(False)
        except RuntimeError:
            # same stale-widget race as _on_tree_item_expanded/_collapsed,
            # belt and suspenders on top of the ordering fix above
            pass

    def _was_selected(self, long_path: str, selected_paths: set[str]) -> bool:
        """true if long_path was part of the viewport selection, or is
        an ancestor/descendant of something that was. exact match alone
        isn't enough since a shielded row is keyed by its root group but
        someone might've clicked a mesh under that root instead, or vice
        versa. plain prefix check both directions instead of re-deriving
        the engine's root-ancestor logic here."""
        if long_path in selected_paths:
            return True
        descendant_prefix = long_path + "|"
        return any(
            sel.startswith(descendant_prefix) or long_path.startswith(sel + "|")
            for sel in selected_paths
        )

    def run_analysis(self) -> None:
        # reset BEFORE clear(), not after - clear() can fire itemChanged
        # synchronously while tearing down checked rows, and these are
        # exactly the `is None` guards that stop that from touching an
        # already-deleted widget. had this backwards before, which is
        # the "RuntimeError: Internal C++ object already deleted" crash
        self.manual_group = None
        self.skipped_group = None
        self.stray_folder_group = None
        self.stray_select_all_checkbox = None
        self.stray_select_all_container = None
        self.preview_tree.clear()

        # captured once here, not re-read later, tree reflects scope AT
        # this analyze click not a live binding to the combo
        select_scope_active = (self.scope_combo.currentIndex() == 0)
        # a fresh analyze starts a new seeding cycle, so the first later
        # switch into Selected Assets still gets the viewport-seed
        self._selected_scope_seeded = select_scope_active

        # reset purge confirm/bypass on every fresh analyze too, otherwise
        # an old confirm check could carry over onto a brand new candidate
        # list with purge already enabled, no extra click needed
        self.purge_confirm_checkbox.setChecked(False)
        self.purge_bypass_checkbox.setChecked(False)

        # both scopes load every mesh/light now, "light" is maya's abstract
        # base type so this catches all 4 light types in one query (missed
        # this at first and All Assets was quietly skipping every light)
        pool = cmds.ls(type=["mesh", "light"], long=True) or []

        # snapshot at this analyze click, never re-read later
        selected_paths = set(cmds.ls(sl=True, long=True) or []) if select_scope_active else set()

        targets = set()
        for node in pool:
            # node might already be a shape - happens a lot with the "All Assets"
            # query above, or if someone selected a shape directly in the outliner
            if cmds.ls([node], type=["mesh", "light"]):
                targets.add(node)
                continue
            targets.update(cmds.listRelatives(node, shapes=True, fullPath=True) or [])

        # sorted, not list(set). string hashing is randomized per Maya
        # session, so set order was deciding which duplicate got the _02
        # suffix and the order commit moved things in
        targets = sorted(targets)
        if not targets:
            self._set_feedback("No valid assets found in the current scope.", self.COLOR_WARN)
            self.statusBar().showMessage("Nothing to sort.")
            return

        result = self.engine.analyze_sorting_plan(targets, self.force_combo.currentText())
        self.current_execution_plan = result["plan"]

        groups = {}
        manual_pile = []
        skipped_pile = []

        for data in result["plan"]:
            if data["source"] == AssignmentSource.SKIPPED_REFERENCED:
                skipped_pile.append(data)
                continue
            # manual review means NO destination found at all. gating on
            # confidence == LOW instead was the actual old bug, a hierarchy
            # context guess IS a real destination just low confidence, and
            # lumping it into manual review threw that guess away
            if data["destination"] == "UNASSIGNED_ASSETS":
                manual_pile.append(data)
                continue

            grp = groups.setdefault(
                data["destination"],
                QtWidgets.QTreeWidgetItem([self._display_destination(data["destination"])]),
            )
            if grp.parent() is None:
                self.preview_tree.addTopLevelItem(grp)
                grp.setExpanded(False)
                header_font = grp.font(0)
                header_font.setBold(True)
                grp.setFont(0, header_font)

            item = QtWidgets.QTreeWidgetItem([
                data["original"], "", data["proposed"], self._display_destination(data["destination"]),
            ])
            # without this, DestinationDelegate.editorEvent can detect the
            # arrow click correctly and still get nothing - Qt's own
            # edit() silently refuses on any index whose item lacks
            # ItemIsEditable, independent of editTriggers entirely
            item.setFlags(item.flags() | QtCore.Qt.ItemIsEditable)
            # right-click/double-click handlers pull this back out via column-0 UserRole
            item.setData(0, QtCore.Qt.UserRole, data)
            # set on every column, not just column 0, so the tooltip fires
            # no matter which column the cursor happens to be over
            for col in range(4):
                item.setToolTip(col, self.ROW_TOOLTIP)

            if select_scope_active:
                # checkbox only exists at all under this scope, never
                # created under All Assets, genuinely not just hidden.
                # Selected Assets is a curation seed now, pre-checked
                # wherever it overlaps the viewport selection, adjust
                # from there with search before committing. data here is
                # the same dict object in current_execution_plan (loop
                # variable, not item.data() round-trip) so this write is reliable
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                included = self._was_selected(data["long_path"], selected_paths)
                item.setCheckState(0, QtCore.Qt.Checked if included else QtCore.Qt.Unchecked)
                data["_included"] = included
            else:
                # explicitly stripped, not left alone, a fresh item's
                # baseline flags in this PySide6 build can't be trusted to
                # NOT include ItemIsUserCheckable (caught this because All
                # Assets rows were showing the flag despite this branch
                # never running)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                item.setData(0, QtCore.Qt.CheckStateRole, None)

            # text + Ready/Needs Freeze coloring both live in one place now
            # (_apply_freeze_status_style) so toggling the freeze-on-commit
            # checkbox later can refresh an already-built row the exact
            # same way this initial build does, instead of duplicating
            # the same text-vs-color branching in two places
            self._apply_freeze_status_style(item, data)

            grp.addChild(item)

        if manual_pile:
            self.manual_group = QtWidgets.QTreeWidgetItem(["Manual Assignment Required"])
            self.preview_tree.addTopLevelItem(self.manual_group)
            header_font = self.manual_group.font(0)
            header_font.setBold(True)
            self.manual_group.setFont(0, header_font)
            # collapsed by default so the header itself carries the "look
            # at this" signal. same amber as the rows inside, not a new
            # color, since amber already means "needs eyes on it" everywhere
            for col in range(4):
                self.manual_group.setForeground(col, QtGui.QBrush(self.COLOR_WARN))

            for data in manual_pile:
                # right-click to pick a destination instead of an inline combo
                row = QtWidgets.QTreeWidgetItem([
                    data["original"], "Needs Review", data["proposed"], self._display_destination(data["destination"]),
                ])
                row.setFlags(row.flags() | QtCore.Qt.ItemIsEditable)
                for col in range(4):
                    row.setForeground(col, QtGui.QBrush(self.COLOR_WARN))
                    row.setToolTip(col, self.ROW_TOOLTIP)
                row.setData(0, QtCore.Qt.UserRole, data)
                if select_scope_active:
                    # same curation-seed checkbox as the main groups above
                    row.setFlags(row.flags() | QtCore.Qt.ItemIsUserCheckable)
                    included = self._was_selected(data["long_path"], selected_paths)
                    row.setCheckState(0, QtCore.Qt.Checked if included else QtCore.Qt.Unchecked)
                    data["_included"] = included
                else:
                    row.setFlags(row.flags() & ~QtCore.Qt.ItemIsUserCheckable)
                    row.setData(0, QtCore.Qt.CheckStateRole, None)
                self.manual_group.addChild(row)
            # collapsed by default, a huge manual pile forcing itself open
            # every analyze would be a worse first impression than one click
            self.manual_group.setExpanded(False)

        if skipped_pile:
            # no context menu here, execute_sorting_pass refuses these nodes anyway
            self.skipped_group = QtWidgets.QTreeWidgetItem(["Skipped (Referenced / Locked)"])
            self.preview_tree.addTopLevelItem(self.skipped_group)
            header_font = self.skipped_group.font(0)
            header_font.setBold(True)
            self.skipped_group.setFont(0, header_font)

            for data in skipped_pile:
                row = QtWidgets.QTreeWidgetItem([data["original"], "Skipped", data["proposed"], "-"])
                for col in range(4):
                    row.setForeground(col, QtGui.QBrush(self.COLOR_WARN))
                    # not the shared ROW_TOOLTIP, no right-click menu exists here
                    row.setToolTip(col, "Double-click to frame item in 3D viewport. "
                                         "Referenced/locked - reassignment is disabled.")
                row.setData(0, QtCore.Qt.UserRole, data)
                self.skipped_group.addChild(row)
            self.skipped_group.setExpanded(False)

        # deprecated folder candidates - root-level folders that look like
        # stray sort debris (see is_stray_category_folder), detection only,
        # nothing here has actually been touched. checked by default (see
        # the opt-out reasoning below), quarantine only happens on Commit
        stray_folders = result.get("stray_folders") or []
        if stray_folders:
            self.stray_folder_group = QtWidgets.QTreeWidgetItem(["Deprecated Folder Candidates"])
            self.stray_folder_group.setToolTip(0, self.DEPRECATED_INFO_TEXT)
            self.preview_tree.addTopLevelItem(self.stray_folder_group)
            header_font = self.stray_folder_group.font(0)
            header_font.setBold(True)
            self.stray_folder_group.setFont(0, header_font)

            # tristate select-all lives right on the header row itself.
            # wrapped in a container instead of embedding the checkbox
            # directly, a bare checkbox mismatched the row's natural
            # height (bold header font) and clipped into the row above/below
            self.stray_select_all_container = QtWidgets.QWidget()
            container_layout = QtWidgets.QHBoxLayout(self.stray_select_all_container)
            container_layout.setContentsMargins(2, 0, 2, 0)
            container_layout.setSpacing(0)

            self.stray_select_all_checkbox = QtWidgets.QCheckBox("Select All")
            self.stray_select_all_checkbox.setTristate(True)
            # only way to flip [box][text] to [text][box] without a fully
            # custom widget is mirroring layoutDirection, scoped to just this checkbox
            self.stray_select_all_checkbox.setLayoutDirection(QtCore.Qt.RightToLeft)
            _accent = self.COLOR_STAGED.name()
            # QSS image:url() approach for the indicator never worked
            # reliably in maya's bundled qt, _checkbox_style paints it
            # directly via drawPrimitive instead, only spacing/font/color left to QSS
            self.stray_select_all_checkbox.setStyleSheet(
                "QCheckBox { spacing: 4px; font-size: 11px; }"
                f"QCheckBox {{ color: {_accent}; }}"
            )
            self.stray_select_all_checkbox.setStyle(self._checkbox_style)

            # only fires on real clicks, never a programmatic setCheckState
            self.stray_select_all_checkbox.clicked.connect(self._on_stray_select_all_clicked)
            container_layout.addWidget(self.stray_select_all_checkbox)
            container_layout.addStretch()

            # not attached to the tree cell yet, that happens in
            # _on_tree_item_expanded only once the section is actually opened
            self.stray_select_all_container.setVisible(False)

            for candidate in stray_folders:
                occupancy = "empty" if candidate["child_count"] == 0 else f"{candidate['child_count']} objects inside"
                row = QtWidgets.QTreeWidgetItem([
                    candidate["name"], f"Not sorted ({occupancy})", "",
                    self._display_destination(self.engine.DEPRECATED_HOLDING_GROUP),
                ])
                row.setFlags(row.flags() | QtCore.Qt.ItemIsUserCheckable)
                # opt-out here unlike the asset checkboxes above, these are
                # confident debris guesses so starting fully checked with
                # deselect-all available beats checking each one by hand
                row.setCheckState(0, QtCore.Qt.Checked)
                row.setData(0, QtCore.Qt.UserRole, candidate)
                for col in range(4):
                    row.setForeground(col, QtGui.QBrush(self.COLOR_WARN))
                    row.setBackground(col, QtGui.QBrush(self.COLOR_STRAY_BG))
                    row.setToolTip(col, self.DEPRECATED_INFO_TEXT)
                self.stray_folder_group.addChild(row)
            # explicit sync since itemChanged only fires on later changes,
            # not this initial construction, master needs to read "Deselect
            # All" the first time the section opens, not "Select All"
            self._sync_stray_select_all_state()
            self.stray_folder_group.setExpanded(False)

        # small sections open by default so an artist doesn't need an extra
        # click to see what actually needs attention - the summary line
        # can say "1 need manual review" but if that section is sitting
        # collapsed underneath it, the artist has to go find it anyway
        for i in range(self.preview_tree.topLevelItemCount()):
            top_item = self.preview_tree.topLevelItem(i)
            if 0 < top_item.childCount() <= self.AUTO_EXPAND_THRESHOLD:
                top_item.setExpanded(True)

        QtCore.QTimer.singleShot(0, self._finalize_column_widths)

        self.commit_btn.setEnabled(True)
        summary = f"{len(result['plan'])} assets found."
        if manual_pile:
            summary += f" {len(manual_pile)} need review."
        if skipped_pile:
            summary += f" {len(skipped_pile)} skipped (locked/referenced)."
        if stray_folders:
            summary += f" {len(stray_folders)} deprecated folder(s) flagged."
        self._set_feedback(summary, self.COLOR_WARN if (manual_pile or skipped_pile or stray_folders) else None)
        self.statusBar().showMessage("Ready to commit.")

    def _finalize_column_widths(self) -> None:
        # resizeColumnToContents only measures VISIBLE rows, and every
        # group here starts collapsed, so it was under-measuring off empty
        # header rows and clipping real text once a group got opened.
        # fix: briefly expand everything, measure, restore expand state
        top_level_items = [
            self.preview_tree.topLevelItem(i)
            for i in range(self.preview_tree.topLevelItemCount())
        ]
        original_expand_states = [item.isExpanded() for item in top_level_items]
        for item in top_level_items:
            item.setExpanded(True)

        for col in range(4):
            self.preview_tree.resizeColumnToContents(col)
            # content-fit width alone leaves zero gap before the next
            # column, breathing room keeps a visual break regardless of name length
            self.preview_tree.setColumnWidth(
                col, self.preview_tree.columnWidth(col) + self.COLUMN_BREATHING_ROOM
            )

        for item, was_expanded in zip(top_level_items, original_expand_states):
            item.setExpanded(was_expanded)
        self.preview_tree.setColumnWidth(
            0, min(self.preview_tree.columnWidth(0), self.MAX_ASSET_COLUMN_WIDTH)
        )

        # column 1 (Status now, was Proposed before the reorder) still needs
        # a floor since the Deselect All widget lives there too and only
        # attaches while expanded, the loop above restores collapsed state
        # right after, so this measure never actually sees it
        self.preview_tree.setColumnWidth(1, max(self.preview_tree.columnWidth(1), 140))

        # floor it against a direct measurement instead of trusting
        # resizeColumnToContents alone for this column
        deprecated_folder_text = self._DESTINATION_LABELS[NexusBatchProcessor.DEPRECATED_HOLDING_GROUP]
        metrics = QtGui.QFontMetrics(self.preview_tree.font())
        needed_width = metrics.horizontalAdvance(deprecated_folder_text) + self.COLUMN_BREATHING_ROOM + 30
        self.preview_tree.setColumnWidth(3, max(self.preview_tree.columnWidth(3), needed_width))
        self.preview_tree.viewport().update()

    def _on_filter_text_changed(self, _text: str) -> None:
        # just restart the debounce window - the actual filtering happens in
        # _apply_pending_filter once the artist stops typing for 120ms
        self._filter_timer.start()

    def _apply_pending_filter(self) -> None:
        self.filter_tree_items(self.search_field.text())

    def filter_tree_items(self, text: str) -> None:
        # nothing fancy - artists just want to type "SM_" and see their meshes.
        # hides non-matching rows and collapses groups that end up empty.
        query = text.strip().lower()

        # batches every setHidden/setExpanded into one repaint instead of
        # one per row, that's where the stutter was actually coming from
        self.preview_tree.setUpdatesEnabled(False)
        try:
            for i in range(self.preview_tree.topLevelItemCount()):
                top_item = self.preview_tree.topLevelItem(i)
                visible_children = 0

                for j in range(top_item.childCount()):
                    child = top_item.child(j)
                    haystack = " ".join(child.text(col) for col in range(4)).lower()
                    matches = query in haystack
                    child.setHidden(not matches)
                    if matches:
                        visible_children += 1

                top_item.setHidden(top_item.childCount() > 0 and visible_children == 0)
                if query and visible_children:
                    top_item.setExpanded(True)
        finally:
            self.preview_tree.setUpdatesEnabled(True)

    def _checked_stray_folder_paths(self) -> list[str]:
        """long paths of exactly the checked stray candidate rows, the
        only thing that authorizes quarantining a folder. read by both
        execute_commit (always) and execute_purge (immediately follows
        the same quarantine with a purge of what it just quarantined)"""
        if self.stray_folder_group is None:
            return []
        checked = []
        for i in range(self.stray_folder_group.childCount()):
            row = self.stray_folder_group.child(i)
            if row.checkState(0) == QtCore.Qt.Checked:
                checked.append(row.data(0, QtCore.Qt.UserRole)["long_path"])
        return checked

    def _checked_asset_long_paths(self) -> set[str]:
        """long paths of every asset row to include, gated on the scope
        dropdown directly rather than N rows each agreeing with it.
        All Assets includes everything, Selected Assets reads
        checkState() live off the widget. still includes skipped rows
        (they never move anyway, but excluding them here would zero out
        execute_commit's own skipped_count message). stray candidates
        are a totally separate system, excluded outright regardless of scope"""
        select_scope_active = (self.scope_combo.currentIndex() == 0)
        included = set()
        for i in range(self.preview_tree.topLevelItemCount()):
            top = self.preview_tree.topLevelItem(i)
            if top is self.stray_folder_group:
                continue
            for j in range(top.childCount()):
                row = top.child(j)
                data = row.data(0, QtCore.Qt.UserRole)
                if not data:
                    continue
                long_path = data.get("long_path")
                if long_path is None:
                    continue
                if not select_scope_active or row.checkState(0) == QtCore.Qt.Checked:
                    included.add(long_path)
        return included

    def execute_commit(self) -> None:
        stray_paths = self._checked_stray_folder_paths()
        # filtered against a live checkState() read, not the mirrored
        # _included flag which only reliably tracks the initial seed state
        checked_paths = self._checked_asset_long_paths()
        plan = [d for d in self.current_execution_plan if d.get("long_path") in checked_paths]
        excluded_count = len(self.current_execution_plan) - len(plan)

        if not plan and not stray_paths:
            if self.current_execution_plan:
                # a real plan exists, everything's just unchecked right
                # now, deliberate state so it gets a real message not silence
                self._set_feedback(
                    "Nothing to commit - every asset is unchecked.",
                    self.COLOR_NEUTRAL,
                )
            return

        # undo after analyze can invalidate cached long_paths (renamed,
        # deleted, reparented). checked up front so a stale plan fails
        # clean instead of dying mid-batch on whichever entry hits first
        stale_paths = [
            d.get("long_path") for d in plan
            if not d.get("long_path") or not cmds.objExists(d["long_path"])
        ]
        if stale_paths:
            self._set_feedback(
                f"{len(stale_paths)} asset(s) no longer match the last Analyze "
                f"(likely an undo) - re-run Analyze before committing.",
                self.COLOR_BAD,
            )
            self.statusBar().showMessage("Plan is stale - re-run Analyze.")
            return

        self._set_feedback("Sorting...", self.COLOR_NEUTRAL)

        with self._ui_busy():
            try:
                fallback_count = sum(1 for d in plan if d["confidence"] == Confidence.LOW and d["source"] != AssignmentSource.SKIPPED_REFERENCED)
                skipped_count = sum(1 for d in plan if d["source"] == AssignmentSource.SKIPPED_REFERENCED)
                override_count = sum(1 for d in plan if d["source"] == AssignmentSource.USER_FORCED)
                unfrozen_count = sum(
                    1 for d in plan
                    if not d["is_frozen"] and not d.get("is_shielded")
                    and not CategorizationEngine.is_freeze_blocked(d["destination"])
                )

                result = self.engine.execute_sorting_pass(
                    plan, freeze_eligible=self.freeze_checkbox.isChecked(),
                    quarantine_folder_paths=stray_paths,
                )
            except Exception as err:
                # kept broad on purpose, this is the last line of defense
                # before an artist sees a raw traceback instead of a real
                # message. narrowed the actual bug-vs-condition distinction
                # at the engine boundary instead (see execute_sorting_pass)
                logger.error(f"Commit failed unexpectedly: {err}")
                self.commit_btn.setEnabled(True)
                self._set_feedback(
                    "Commit failed - the scene may have changed since the "
                    "last Analyze (e.g. an undo). Re-run Analyze and try again.",
                    self.COLOR_BAD,
                )
                self.statusBar().showMessage("Commit failed.")
                return

            if result.get("status") == "success":
                # same fix as run_analysis: reset before clear(), not after
                self.manual_group = None
                self.skipped_group = None
                self.stray_folder_group = None
                self.stray_select_all_checkbox = None
                self.stray_select_all_container = None
                self.preview_tree.clear()

                summary = f"Sorted {result['count']} assets."
                if result.get("cancelled"):
                    summary += " (cancelled partway through)"
                move_failed = result.get("move_failed") or []
                if move_failed:
                    summary += f" {len(move_failed)} couldn't be moved (see script editor)."
                if excluded_count:
                    summary += f" {excluded_count} left unchecked, not sorted."
                if override_count:
                    # confirms a manual reassignment actually landed in the
                    # scene, not just in the UI
                    summary += f" Applied {override_count} manual override(s)."
                if fallback_count:
                    summary += f" {fallback_count} were low-confidence fallbacks."
                if skipped_count:
                    summary += f" {skipped_count} skipped (locked/referenced)."
                drained_quarantined = result.get("drained_folders_quarantined") or []
                if drained_quarantined:
                    summary += (
                        f" Moved {len(drained_quarantined)} emptied category folder(s) "
                        f"to a safe holding folder (not deleted)."
                    )
                stray_quarantined = result.get("stray_folders_quarantined") or []
                if stray_quarantined:
                    summary += (
                        f" Moved {len(stray_quarantined)} approved deprecated folder(s) "
                        f"to a safe holding folder (not deleted)."
                    )
                stray_skipped = result.get("stray_folders_skipped") or []
                if stray_skipped:
                    summary += (
                        f" {len(stray_skipped)} approved folder(s) no longer existed "
                        f"at commit time - skipped."
                    )
                all_flagged = (result.get("drained_folders_flagged") or []) + (result.get("stray_folders_flagged") or [])
                if all_flagged:
                    summary += (
                        f" {len(all_flagged)} folder(s) left untouched - referenced/locked, "
                        f"or carry custom attributes/connections (see script editor for detail)."
                    )
                if self.freeze_checkbox.isChecked():
                    if result.get("frozen"):
                        summary += f" Froze {result['frozen']} transforms."
                    if result.get("freeze_failed"):
                        summary += f" {result['freeze_failed']} couldn't be frozen (constrained or instanced - see script editor)."
                elif unfrozen_count:
                    summary += f" {unfrozen_count} still need transforms frozen."

                # green if it went clean, amber if there's something worth a second look -
                # still a success either way, just different levels of "you're done"
                tone = self.COLOR_WARN if (fallback_count or skipped_count or all_flagged or move_failed or
                                            result.get("freeze_failed") or
                                            (not self.freeze_checkbox.isChecked() and unfrozen_count) or
                                            result.get("cancelled")) else self.COLOR_GOOD
                self._set_feedback(summary, tone)
                self.statusBar().showMessage("Done.")
                # commit_btn stays disabled, only a fresh analyze re-enables it
            else:
                self.commit_btn.setEnabled(True)
                self._set_feedback(f"Sort failed: {result.get('message', 'see script editor')}", self.COLOR_BAD)
                self.statusBar().showMessage("Execution failed.")

    def _purge_checked_stray_candidates(self, original_paths: list[str]) -> dict[str, Any]:
        """direct tree-to-purge lane for checked stray candidates, not
        gated on bypass since that checkbox's meaning (ignore age
        threshold) never applied to unquarantined rows anyway. still
        routes through the same two engine calls as the normal
        commit-then-purge flow (quarantine, then purge), nothing about
        the safety net gets skipped, just the wait between the two
        stages collapses into one click. manifest written to disk before
        the delete, same discipline as the age-based purge path.

        original_paths passed in rather than re-read from the tree, so
        this acts on exactly what execute_purge already decided on.
        returns a summary dict, this only touches the scene, never the UI"""
        quarantine_result = self.engine.execute_sorting_pass(
            [], freeze_eligible=False, quarantine_folder_paths=original_paths,
        )
        skipped = set(quarantine_result.get("stray_folders_skipped") or [])
        flagged = set(quarantine_result.get("stray_folders_flagged") or [])
        quarantined_new_paths = quarantine_result.get("stray_folders_quarantined") or []
        # order-preserving, execute_sorting_pass appends each path to
        # exactly one of these three lists, so this lines up 1:1 with quarantined_new_paths
        succeeded_originals = [p for p in original_paths if p not in skipped and p not in flagged]

        purge_result = {"purged": [], "skipped_unsafe": [], "skipped_missing": []}
        if quarantined_new_paths:
            candidate_manifest = {
                "generated": datetime.datetime.now().isoformat(),
                "older_than_days": None,
                "source": "tree_selected_candidates",
                "eligible": [{"path": p} for p in quarantined_new_paths],
            }
            self.engine.write_purge_manifest_to_disk(candidate_manifest)
            with MayaUndoChunk():
                purge_result = self.engine.purge_deprecated_folders(quarantined_new_paths)

        return {
            "purged_count": len(purge_result["purged"]),
            "purge_skipped_count": len(purge_result["skipped_unsafe"]) + len(purge_result["skipped_missing"]),
            "quarantine_skipped_count": len(skipped) + len(flagged),
            # anything that made it into quarantine is no longer a stray
            # candidate regardless of what happened to it next, remove from tree
            "removed_original_paths": set(succeeded_originals),
        }

    def _remove_stray_candidate_rows(self, original_paths: set[str]) -> None:
        """removes exactly the candidate rows named in original_paths,
        called right after those paths get processed. never touches a
        row not in this set, unchecked/pending candidates stay untouched"""
        if self.stray_folder_group is None:
            return
        to_remove = set(original_paths)
        for i in reversed(range(self.stray_folder_group.childCount())):
            row = self.stray_folder_group.child(i)
            data = row.data(0, QtCore.Qt.UserRole)
            if data and data.get("long_path") in to_remove:
                self.stray_folder_group.removeChild(row)

        if self.stray_folder_group.childCount() == 0:
            # nothing left, drop the whole section instead of an empty
            # header with a select-all that has nothing to select
            idx = self.preview_tree.indexOfTopLevelItem(self.stray_folder_group)
            if idx != -1:
                self.preview_tree.takeTopLevelItem(idx)
            self.stray_folder_group = None
            self.stray_select_all_checkbox = None
            self.stray_select_all_container = None
        else:
            self._sync_stray_select_all_state()

    def execute_purge(self) -> None:
        """only path in this UI that reaches purge_deprecated_folders.
        no separate popup, drawer starts collapsed and the confirm
        checkbox has to be ticked before this button's even clickable,
        same "can't happen by accident" guarantee without a second window.

        handles two independent sources in one click:
        1. checked candidate rows in the tree (see
           _purge_checked_stray_candidates), the primary targeted path,
           bypass has no bearing here since it never meant anything for
           folders that were never quarantined
        2. folders already in GRP_DEPRECATED_FOLDERS past the age
           threshold (or all of them if bypass is checked), the original
           bulk-sweep path, recomputed fresh here rather than trusting
           a possibly stale _last_purge_manifest

        both run their own safety checks and fold into one feedback message"""
        stray_candidate_paths = self._checked_stray_folder_paths()
        self._refresh_purge_preview()
        manifest = self._last_purge_manifest
        manifest_eligible = bool(manifest and manifest["eligible"])

        if not manifest_eligible and not stray_candidate_paths:
            # button shouldn't be reachable here, but the scene or the
            # date could've moved since the label was last shown
            self._set_feedback(
                "Nothing eligible anymore - the scene or the date changed "
                "since this was last checked. Nothing was purged.",
                self.COLOR_NEUTRAL,
            )
            return

        # snapshot commit's pre-purge state, restored on every exit via the finally below
        was_commit_enabled = self.commit_btn.isEnabled()
        try:
            with self._ui_busy():
                candidate_summary = None
                if stray_candidate_paths:
                    candidate_summary = self._purge_checked_stray_candidates(stray_candidate_paths)
                    if candidate_summary["removed_original_paths"]:
                        self._remove_stray_candidate_rows(candidate_summary["removed_original_paths"])

                # recomputed again since the candidate pass above might've
                # quarantined something new. not a double-purge, that pass
                # already deleted what it quarantined before this manifest is built
                self._refresh_purge_preview()
                manifest = self._last_purge_manifest
                manifest_result = None
                manifest_path = None
                if manifest and manifest["eligible"]:
                    # written before deletion so the record survives even
                    # if the delete step below partially fails
                    manifest_path = self.engine.write_purge_manifest_to_disk(manifest)
                    with MayaUndoChunk():
                        eligible_paths = [e["path"] for e in manifest["eligible"]]
                        manifest_result = self.engine.purge_deprecated_folders(eligible_paths)

                summary_parts = []
                any_skipped = False
                if candidate_summary is not None:
                    part = f"{candidate_summary['purged_count']} selected candidate folder(s) purged."
                    skipped_here = candidate_summary["quarantine_skipped_count"] + candidate_summary["purge_skipped_count"]
                    if skipped_here:
                        part = part[:-1] + f" ({skipped_here} skipped - see script editor for detail)."
                        any_skipped = True
                    summary_parts.append(part)
                if manifest_result is not None:
                    part = f"{len(manifest_result['purged'])} backup folder(s) purged."
                    skipped_here = len(manifest_result["skipped_unsafe"]) + len(manifest_result["skipped_missing"])
                    if skipped_here:
                        part = part[:-1] + f" ({skipped_here} became unsafe or no longer existed - skipped)."
                        any_skipped = True
                    summary_parts.append(part)
                    if manifest_path:
                        summary_parts.append(f"Manifest saved to {manifest_path}.")

                tone = self.COLOR_WARN if any_skipped else self.COLOR_GOOD
                self._set_feedback(" ".join(summary_parts), tone)
                self.statusBar().showMessage("Purge complete.")

                # re-arming needs its own deliberate click, reset both
                # checkboxes so a second purge can't happen just because
                # they were left checked from last time
                self.purge_confirm_checkbox.setChecked(False)
                self.purge_bypass_checkbox.setChecked(False)
                self._refresh_purge_preview()
        except Exception as err:
            # same reasoning as execute_commit's except, kept broad on
            # purpose as the artist-facing safety net
            logger.error(f"Purge failed unexpectedly: {err}")
            self._set_feedback(
                "Purge failed - see script editor for detail. Nothing "
                "further was purged after the failure point.",
                self.COLOR_BAD,
            )
            self.statusBar().showMessage("Purge failed.")
        finally:
            self.commit_btn.setEnabled(was_commit_enabled)

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        # clears the tree's row highlight when focus leaves it. the
        # "outside the tree" check matters: column 3's dropdown editor is
        # a QComboBox parented to the tree's own viewport, so opening it
        # ALSO fires FocusOut even though nothing was actually clicked
        # away. isAncestorOf tells those two cases apart, that mixup was
        # the real cause of "the arrow doesn't do anything", not a hit-rect bug
        if obj is self.preview_tree and event.type() == QtCore.QEvent.FocusOut:
            new_focus = QtWidgets.QApplication.focusWidget()
            if new_focus is None or not self.preview_tree.isAncestorOf(new_focus):
                self.preview_tree.clearSelection()
        elif (obj is self.preview_tree and event.type() == QtCore.QEvent.KeyPress
                and event.key() == QtCore.Qt.Key_Space):
            # space turns a highlighted range into a checked one, see _toggle_checked
            self._toggle_checked(self.preview_tree.selectedItems())
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        # catches the docking workspace control's own settle-resize after
        # first show, not just manual dragging - that settle is what was
        # racing run_analysis's one-shot finalize and winning
        super().resizeEvent(event)
        if self.preview_tree.topLevelItemCount() > 0:
            self._column_resize_timer.start()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        # breaks signal connections holding a live reference back to this
        # window, so closing actually frees it instead of leaking a
        # disconnected-but-alive widget on every re-run
        for signal, slot in (
            (self.preview_tree.itemSelectionChanged, self._on_selection_changed),
            (self.preview_tree.itemDoubleClicked, self._on_item_double_clicked),
            (self.preview_tree.itemClicked, self._on_item_clicked),
            (self.preview_tree.customContextMenuRequested, self._show_reassign_menu),
            (self.preview_tree.itemExpanded, self._on_tree_item_expanded),
            (self.preview_tree.itemCollapsed, self._on_tree_item_collapsed),
            (self.preview_tree.itemChanged, self._on_tree_item_changed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self.preview_tree.removeEventFilter(self)
        self._filter_timer.stop()
        self._purge_preview_timer.stop()  # same reasoning, don't tick past window close
        self._column_resize_timer.stop()
        self.current_execution_plan = []
        super().closeEvent(event)

# only keep one instance around - re-running this script shouldn't spawn duplicates
_nexus_ui_instance: NexusSorterUI | None = None

def show_ui() -> None:
    global _nexus_ui_instance

    # closes the old instance first now, before force-deleting the
    # workspace control. used to be the other order, which is what the
    # "Internal C++ object already deleted" crashes traced back to,
    # cmds.deleteUI was tearing down the old widget hierarchy before its
    # own close()/deleteLater() ran, so a queued expand/collapse event
    # fired against objects that no longer existed
    if _nexus_ui_instance is not None:
        try:
            _nexus_ui_instance.close()
            _nexus_ui_instance.deleteLater()
        except RuntimeError:
            pass
        _nexus_ui_instance = None

    workspace_control_name = "NexusAutoSorterUIWorkspaceControl"
    if cmds.workspaceControl(workspace_control_name, exists=True):
        cmds.deleteUI(workspace_control_name, control=True)

    _nexus_ui_instance = NexusSorterUI()
    _nexus_ui_instance.show(dockable=True, area='right', floating=True)
