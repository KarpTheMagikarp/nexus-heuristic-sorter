"""
the destination dropdown used to open on a click anywhere in the empty
space right of the arrow, since that column stretches to the window edge.

needs real Qt for font metrics, so it skips on CI where conftest stubs
PySide6. run it locally with PySide6 installed to actually exercise it.
"""
import os
from unittest.mock import MagicMock

import pytest

from nexus_sorter.qt_compat import QtCore, QtWidgets

pytestmark = pytest.mark.skipif(isinstance(QtCore, MagicMock), reason="needs real PySide, conftest stubbed it")


@pytest.fixture(scope="module")
def zones():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from nexus_sorter.delegates import DestinationDelegate
    cell = QtCore.QRect(540, 100, 520, 20)
    text_rect, arrow_rect = DestinationDelegate.cell_zones(cell, app.font(), "Environment")
    return cell, text_rect, arrow_rect, DestinationDelegate.hit_zone(text_rect, arrow_rect)


def test_word_and_arrow_open_the_menu(zones):
    _cell, text_rect, arrow_rect, hit = zones
    assert hit.contains(text_rect.center())
    assert hit.contains(arrow_rect.center())


def test_empty_space_past_the_arrow_does_not(zones):
    cell, _text_rect, arrow_rect, hit = zones
    for x in (arrow_rect.right() + 6, cell.right() - 60):
        assert not hit.contains(QtCore.QPoint(x, cell.center().y()))


def test_arrow_never_spills_out_of_a_narrow_cell():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from nexus_sorter.delegates import DestinationDelegate
    narrow = QtCore.QRect(0, 0, 40, 20)
    _text, arrow = DestinationDelegate.cell_zones(narrow, app.font(), "Environment")
    assert arrow.width() >= 0
    assert arrow.right() <= narrow.right() or arrow.width() == 0
