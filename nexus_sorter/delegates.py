from __future__ import annotations

from typing import Any, TYPE_CHECKING

from .qt_compat import QtWidgets, QtCore, QtGui, HAS_CHECK_INDICATOR_FEATURE

from .categorization import AssignmentSource

if TYPE_CHECKING:
    # only imported for type checkers, never at runtime - main_window.py
    # imports THIS file, so a real import here would be circular
    from .main_window import NexusSorterUI


class DestinationDelegate(QtWidgets.QStyledItemDelegate):
    """destination column delegate. fakes a per-row combobox (arrow +
    dropdown) without actually paying for setItemWidget on a tree that
    can have thousands of rows. only opens on a click on the word or arrow,
    not the default edit trigger (tree runs NoEditTriggers on purpose).

    paint() draws the text and arrow itself using cell_zones() below,
    and the click handler uses the exact same function for hit testing.
    used to be two separate guesses at the same rectangle which is
    exactly how that stayed broken for two review rounds in a row.

    only activates on an actual reassignable row (has both source and
    destination in its data, not SKIPPED_REFERENCED). checking source
    alone isn't enough since a stray-folder-candidate row has neither
    key at all."""

    ARROW_GLYPH = "\u25bc"
    ARROW_GAP = 4  # matches cell_zones' own gap constant, kept in sync deliberately

    def __init__(self, ui: NexusSorterUI, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._ui = ui

    def sizeHint(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> QtCore.QSize:
        # default sizeHint has no idea this delegate manually paints an
        # arrow after the text, same blind spot AssetCheckDelegate already
        # had for its own checkbox. wasn't visible while destination text
        # was still the raw "GRP_01_CHARACTERS" (plenty long enough to
        # leave room for the arrow by accident), only showed up as dead
        # space once that shrank to the friendly "Characters" label
        size = super().sizeHint(option, index)
        item, _data = self._editable_row(index)
        if item is not None:
            metrics = QtGui.QFontMetrics(option.font)
            arrow_width = metrics.horizontalAdvance(self.ARROW_GLYPH)
            size = QtCore.QSize(size.width() + self.ARROW_GAP + arrow_width, size.height())
        return size

    def _editable_row(self, index: QtCore.QModelIndex) -> tuple[Any, dict[str, Any] | None]:
        item = self._ui.preview_tree.itemFromIndex(index)
        if item is None or item.childCount() > 0:
            return None, None
        data = item.data(0, QtCore.Qt.UserRole)
        if not data or "destination" not in data or "source" not in data:
            return None, None
        if data.get("source") == AssignmentSource.SKIPPED_REFERENCED:
            return None, None
        return item, data

    def editorEvent(
        self,
        event: QtCore.QEvent,
        model: QtCore.QAbstractItemModel,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> bool:
        """catches the arrow click directly off the real mouse event,
        instead of the old approach (main_window.py's _on_item_clicked
        reconstructing "where was the click" after the fact from
        QtGui.QCursor.pos()). that's a global OS-level cursor query with
        no guaranteed relationship to the actual click position by the
        time it runs - confirmed with an actual simulated click, not a
        guess: itemClicked fired every single time, but the QCursor.pos()
        position it read almost never matched arrow_rect, so editItem()
        just silently never got called. a QMouseEvent handed to
        editorEvent carries its own accurate position captured at the
        moment of the event, no cursor query needed at all.

        left button only - right-click already opens the full reassign
        menu for the whole row via a separate, working mechanism (see
        main_window.py's customContextMenuRequested handling), arrow
        included, so there's nothing missing to duplicate here."""
        if (event.type() == QtCore.QEvent.Type.MouseButtonRelease
                and event.button() == QtCore.Qt.MouseButton.LeftButton):
            item, _data = self._editable_row(index)
            if item is not None:
                plain_text = item.text(index.column())
                text_rect, arrow_rect = self.cell_zones(option.rect, option.font, plain_text)
                try:
                    click_pos = event.position().toPoint()
                except AttributeError:
                    click_pos = event.pos()
                if self.hit_zone(text_rect, arrow_rect).contains(click_pos):
                    self._ui.preview_tree.editItem(item, index.column())
                    return True
        return super().editorEvent(event, model, option, index)

    @staticmethod
    def cell_zones(
        cell_rect: QtCore.QRect, font: QtGui.QFont, plain_text: str
    ) -> tuple[QtCore.QRect, QtCore.QRect]:
        """single place this cell's layout gets computed - text, then a
        gap, then the arrow. paint() and the click handler both call
        this with the same inputs so drawn and clickable positions can't
        drift apart the way two separate formulas did before"""
        metrics = QtGui.QFontMetrics(font)
        left_margin = 4
        gap = DestinationDelegate.ARROW_GAP
        text_width = metrics.horizontalAdvance(plain_text)
        text_rect = QtCore.QRect(cell_rect.left() + left_margin, cell_rect.top(),
                                  text_width, cell_rect.height())
        arrow_left = text_rect.right() + gap
        # glyph width only, clipped to the cell. this used to run to the
        # cell's right edge, and destination is the stretch column, so a
        # click anywhere in the empty space past the arrow opened the menu
        arrow_width = min(metrics.horizontalAdvance(DestinationDelegate.ARROW_GLYPH),
                          max(cell_rect.right() - arrow_left + 1, 0))
        arrow_rect = QtCore.QRect(arrow_left, cell_rect.top(), arrow_width, cell_rect.height())
        return text_rect, arrow_rect

    @staticmethod
    def hit_zone(text_rect: QtCore.QRect, arrow_rect: QtCore.QRect) -> QtCore.QRect:
        # word + arrow reads as one combo box, so clicking either opens it.
        # couple px of slop on the ends so the arrow isn't a pixel hunt
        return text_rect.united(arrow_rect).adjusted(-2, 0, 3, 0)

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        item, _data = self._editable_row(index)
        if item is None:
            super().paint(painter, option, index)
            return

        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        # let native drawControl handle bg/selection/focus, just blank
        # the text so it doesn't ALSO draw it under our manual text below
        opt.text = ""
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        plain_text = item.text(index.column())
        text_rect, arrow_rect = self.cell_zones(option.rect, opt.font, plain_text)

        painter.save()
        painter.setFont(opt.font)
        text_color = opt.palette.color(
            QtGui.QPalette.HighlightedText if (opt.state & QtWidgets.QStyle.State_Selected)
            else QtGui.QPalette.Text
        )
        painter.setPen(text_color)
        painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, plain_text)
        # arrow matches the row's text color (mint on Ready, amber on Needs Freeze)
        painter.drawText(arrow_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self.ARROW_GLYPH)
        painter.restore()

    def createEditor(
        self,
        parent: QtWidgets.QWidget,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> QtWidgets.QWidget | None:
        item, data = self._editable_row(index)
        if item is None:
            return None
        combo = QtWidgets.QComboBox(parent)
        for label, folder_name in self._ui.DESTINATION_CHOICES:
            combo.addItem(label, folder_name)
        current_idx = combo.findData(data["destination"])
        if current_idx >= 0:
            combo.setCurrentIndex(current_idx)
        QtCore.QTimer.singleShot(0, combo.showPopup)
        return combo

    def setEditorData(self, editor: QtWidgets.QWidget, index: QtCore.QModelIndex) -> None:
        # no-op, createEditor already set the right index. Qt's default
        # would try to match currentText against "GRP_02_PROPS" instead
        # of the human label "Props" and silently reset it
        pass

    def setModelData(
        self,
        editor: QtWidgets.QWidget,
        model: QtCore.QAbstractItemModel,
        index: QtCore.QModelIndex,
    ) -> None:
        item, data = self._editable_row(index)
        if item is None:
            return
        folder_name = editor.currentData()
        if folder_name is None or folder_name == data["destination"]:
            return
        # same shared path the old right-click menu used, handles the
        # whole selected batch too if this row is part of one
        self._ui._reassign_destination_for_selection(item, data, folder_name)

    def updateEditorGeometry(
        self,
        editor: QtWidgets.QWidget,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        editor.setGeometry(option.rect)


class NexusCheckboxStyle(QtWidgets.QProxyStyle):
    """paints the checkbox glyph for the master select-all/deselect-all
    toggle on stray folder candidates. always blue (COLOR_STAGED) no
    matter what it's controlling, same blue as everything else
    staged/pending in this tool.

    used to also cover the per-row checkboxes but that kept rendering
    wrong in maya's bundled qt build no matter what got tried, so those
    moved to AssetCheckDelegate below instead, which draws its own
    checkbox directly rather than going through QStyle at all."""

    _INDICATOR_PRIMITIVES = (QtWidgets.QStyle.PE_IndicatorCheckBox,)

    def __init__(self, base_style: QtWidgets.QStyle, accent: QtGui.QColor) -> None:
        super().__init__(base_style)
        # passed in from NexusSorterUI.COLOR_STAGED instead of duplicated
        # as a second hardcoded color, same "one source of truth" idea
        self._accent = accent

    def drawPrimitive(
        self,
        element: QtWidgets.QStyle.PrimitiveElement,
        option: QtWidgets.QStyleOption,
        painter: QtGui.QPainter,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        if element not in self._INDICATOR_PRIMITIVES:
            super().drawPrimitive(element, option, painter, widget)
            return

        is_checked = bool(option.state & QtWidgets.QStyle.State_On)
        is_partial = bool(option.state & QtWidgets.QStyle.State_NoChange)

        original_state = option.state
        option.state = (
            (original_state & ~(QtWidgets.QStyle.State_On | QtWidgets.QStyle.State_NoChange))
            | QtWidgets.QStyle.State_Off
        )
        super().drawPrimitive(element, option, painter, widget)
        option.state = original_state

        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        inset = max(2, int(option.rect.width() * 0.2))
        r = option.rect.adjusted(inset, inset, -inset, -inset)

        if is_checked or is_partial:
            pen = QtGui.QPen(self._accent)
            pen.setWidthF(max(1.6, option.rect.height() * 0.12))
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            if is_partial:
                y = r.center().y()
                painter.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
            else:
                p1 = QtCore.QPointF(r.left(), r.top() + r.height() * 0.55)
                p2 = QtCore.QPointF(r.left() + r.width() * 0.4, r.bottom())
                p3 = QtCore.QPointF(r.right(), r.top())
                painter.drawLine(p1, p2)
                painter.drawLine(p2, p3)
        else:
            outline_pen = QtGui.QPen(QtGui.QColor(190, 190, 190, 170))
            outline_pen.setWidthF(1.2)
            painter.setPen(outline_pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(option.rect.adjusted(1, 1, -1, -1), 2, 2)

        painter.restore()


class AssetCheckDelegate(QtWidgets.QStyledItemDelegate):
    """asset column delegate. draws its own checkbox by hand instead of
    using a QStyle indicator at all, since proxying that indicator kept
    rendering wrong in maya's bundled qt build (invisible checked state,
    then invisible unchecked state, still inconsistent after fixing both
    of those). rather than chase a 4th theory, just draws it manually.

    item.checkState() is still the actual source of truth, this only
    changes how it gets drawn and how a click gets detected. editorEvent
    always returns False so Qt's own click handling never fires, this
    delegate + checkbox_rect() is now the only thing handling clicks on
    this column, drawn and clickable positions can't disagree anymore."""

    BOX_SIZE = 13

    def __init__(self, ui: NexusSorterUI, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._ui = ui

    def _is_checkbox_row(self, item: Any) -> bool:
        # childCount() == 0 check keeps group rows (destination groups,
        # "Deprecated Folder Candidates") from ever rendering as checkboxes
        return (item is not None and item.childCount() == 0
                and bool(item.flags() & QtCore.Qt.ItemIsUserCheckable))

    def _accent_for(self, item: Any) -> QtGui.QColor:
        stray_group = self._ui.stray_folder_group
        if stray_group is not None and item.parent() is stray_group:
            return self._ui.COLOR_WARN
        return self._ui.COLOR_STAGED

    def sizeHint(self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex) -> QtCore.QSize:
        # default sizeHint doesn't know about the manual checkbox box,
        # without this resizeColumnToContents(0) undersizes the column
        size = super().sizeHint(option, index)
        item = self._ui.preview_tree.itemFromIndex(index)
        if self._is_checkbox_row(item):
            size = QtCore.QSize(size.width() + self.BOX_SIZE + 9, size.height())
        return size

    @staticmethod
    def checkbox_rect(cell_rect: QtCore.QRect) -> QtCore.QRect:
        """single source of truth for the box position, paint() and the
        click handler both call this so they can't drift apart"""
        size = AssetCheckDelegate.BOX_SIZE
        y = cell_rect.top() + (cell_rect.height() - size) // 2
        return QtCore.QRect(cell_rect.left() + 3, y, size, size)

    def editorEvent(
        self,
        event: QtCore.QEvent,
        model: QtCore.QAbstractItemModel,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> bool:
        return False

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        item = self._ui.preview_tree.itemFromIndex(index)
        if not self._is_checkbox_row(item):
            # not checkable right now, but might still carry old
            # CheckStateRole data from a previous scope (left there on
            # purpose so curation survives switching scopes). Qt's default
            # paint would draw a native checkbox off that leftover data
            # regardless of the actual flag, so force the indicator off
            opt = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.features &= ~HAS_CHECK_INDICATOR_FEATURE
            style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
            style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)
            return

        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""  # drawn manually below, indented past the box
        # without clearing this, drawControl paints its OWN native
        # checkbox underneath the manual one below, doubled checkmark look
        opt.features &= ~HAS_CHECK_INDICATOR_FEATURE
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        box = self.checkbox_rect(option.rect)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        if item.checkState(0) == QtCore.Qt.Checked:
            accent = self._accent_for(item)
            pen = QtGui.QPen(accent)
            pen.setWidthF(1.8)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(box, 2, 2)
            inset = box.adjusted(3, 3, -3, -3)
            p1 = QtCore.QPointF(inset.left(), inset.top() + inset.height() * 0.55)
            p2 = QtCore.QPointF(inset.left() + inset.width() * 0.4, inset.bottom())
            p3 = QtCore.QPointF(inset.right(), inset.top())
            painter.drawLine(p1, p2)
            painter.drawLine(p2, p3)
        else:
            outline_pen = QtGui.QPen(QtGui.QColor(190, 190, 190, 170))
            outline_pen.setWidthF(1.2)
            painter.setPen(outline_pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawRoundedRect(box, 2, 2)
        painter.restore()

        text_left = box.right() + 6
        text_rect = QtCore.QRect(text_left, option.rect.top(),
                                  max(option.rect.right() - text_left, 0), option.rect.height())
        painter.save()
        painter.setFont(opt.font)
        text_color = opt.palette.color(
            QtGui.QPalette.HighlightedText if (opt.state & QtWidgets.QStyle.State_Selected)
            else QtGui.QPalette.Text
        )
        painter.setPen(text_color)
        metrics = QtGui.QFontMetrics(opt.font)
        elided = metrics.elidedText(item.text(0), QtCore.Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, elided)
        painter.restore()
