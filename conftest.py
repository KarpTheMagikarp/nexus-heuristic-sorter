"""
makes `import nexus_sorter` work on a machine with no Maya and no PySide6
installed, i.e. a plain CI runner.

the nexus_sorter package imports maya.cmds, maya.api.OpenMaya,
maya.app.general.mayaMixin, and PySide6 across a few of its modules (see
hierarchy.py, categorization.py, batch_processor.py, delegates.py,
main_window.py). none of that exists on a bare interpreter. but the stuff
this test suite actually cares about protecting - NamingResolver,
CategorizationEngine, is_stray_category_folder - never touches any of it at
import time. only 5 classes in the whole package do, and only as BASE
classes:

    DestinationDelegate(QtWidgets.QStyledItemDelegate)
    AssetCheckDelegate(QtWidgets.QStyledItemDelegate)
    NexusCheckboxStyle(QtWidgets.QProxyStyle)
    NexusSorterUI(MayaQWidgetDockableMixin, QtWidgets.QMainWindow)
    _WrappingCheckboxLabel(QtWidgets.QLabel)

that's why the dummy classes below are real `class X: pass` objects instead
of just MagicMock everywhere. turns out "class Foo(some_mock_instance):"
does NOT reliably fail loudly the way you'd expect - found this the hard
way with QLabel, MagicMock has its own __mro_entries__ support for being
subclassed, so it silently turns the "subclass" into another MagicMock
instead of raising. everything else maya/qt-shaped in this package
(QtGui.QColor(...) calls, QtWidgets.QStyle.PE_IndicatorCheckBox reads) is
just a plain call/attribute lookup, MagicMock's automatic child-mock thing
handles those fine with zero extra work.

if something new ever subclasses a maya/qt type somewhere in this package,
add one more dummy class below - don't trust "it imported with no error"
as proof it worked, check what the class actually became first (see
_StubQLabel's own history below for exactly why).

checks for the real maya/PySide6 first before stubbing anything, in case
someone's running this through mayapy where the real packages actually
exist and work - don't want to silently clobber a real environment with
fake ones.
"""
import sys
from unittest.mock import MagicMock


def _install_maya_stub():
    try:
        import maya.cmds  # noqa: F401
        return  # real Maya is present (e.g. running under mayapy) - leave it alone
    except ImportError:
        pass

    maya_module = MagicMock(name="maya")
    maya_cmds = MagicMock(name="maya.cmds")
    maya_api = MagicMock(name="maya.api")
    maya_om = MagicMock(name="maya.api.OpenMaya")
    maya_mel = MagicMock(name="maya.mel")
    maya_app = MagicMock(name="maya.app")
    maya_app_general = MagicMock(name="maya.app.general")
    maya_mixin_module = MagicMock(name="maya.app.general.mayaMixin")

    class _StubMayaQWidgetDockableMixin:
        """empty on purpose, no test ever instantiates NexusSorterUI
        (needs a real QApplication + live Maya session), this just
        needs to be a valid base class, never a functional one"""
        pass

    maya_mixin_module.MayaQWidgetDockableMixin = _StubMayaQWidgetDockableMixin

    maya_module.cmds = maya_cmds
    maya_module.api = maya_api
    maya_module.mel = maya_mel
    maya_module.app = maya_app
    maya_api.OpenMaya = maya_om
    maya_app.general = maya_app_general
    maya_app_general.mayaMixin = maya_mixin_module

    sys.modules["maya"] = maya_module
    sys.modules["maya.cmds"] = maya_cmds
    sys.modules["maya.api"] = maya_api
    sys.modules["maya.api.OpenMaya"] = maya_om
    sys.modules["maya.mel"] = maya_mel
    sys.modules["maya.app"] = maya_app
    sys.modules["maya.app.general"] = maya_app_general
    sys.modules["maya.app.general.mayaMixin"] = maya_mixin_module


def _install_pyside_stub():
    try:
        import PySide6  # noqa: F401
        return  # real PySide6 is present - leave it alone
    except ImportError:
        pass

    pyside_module = MagicMock(name="PySide6")
    qtwidgets = MagicMock(name="PySide6.QtWidgets")
    qtcore = MagicMock(name="PySide6.QtCore")
    qtgui = MagicMock(name="PySide6.QtGui")

    class _StubQStyledItemDelegate:
        pass

    class _StubQProxyStyle:
        pass

    class _StubQMainWindow:
        pass

    class _StubQLabel:
        # added late, see the module docstring above for why a stub had
        # to exist here even though the class "imported fine" without it
        pass

    qtwidgets.QStyledItemDelegate = _StubQStyledItemDelegate
    qtwidgets.QProxyStyle = _StubQProxyStyle
    qtwidgets.QMainWindow = _StubQMainWindow
    qtwidgets.QLabel = _StubQLabel

    pyside_module.QtWidgets = qtwidgets
    pyside_module.QtCore = qtcore
    pyside_module.QtGui = qtgui

    sys.modules["PySide6"] = pyside_module
    sys.modules["PySide6.QtWidgets"] = qtwidgets
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtGui"] = qtgui


# conftest.py loads before any test_*.py file, so by the time
# test_naming_resolver.py runs `import nexus_sorter`, these stubs are
# already sitting in sys.modules waiting for it
_install_maya_stub()
_install_pyside_stub()
