"""
single place this package figures out which Qt binding is actually
available, so main_window.py and delegates.py don't each have to guess
independently and risk landing on two different ones.

Maya only started shipping PySide6 with Maya 2025. every version before
that (2022/2023/2024, still very much in real production use at studios
that lag a version or two on purpose) ships PySide2 instead - importing
PySide6 directly on one of those crashes on the very first line, before
this tool ever gets a chance to show its own UI or a real error message.
tries PySide6 first since that's what this tool is built and tested
against, falls back to PySide2 so it still runs on older Maya rather
than dying outright. if somehow neither is available, raises a message
that actually says what's wrong instead of a generic "no module" trace.
"""
try:
    from PySide6 import QtWidgets, QtCore, QtGui
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore, QtGui
        QT_BINDING = "PySide2"
    except ImportError as err:
        raise ImportError(
            "Nexus Sorter needs PySide6 (Maya 2025+) or PySide2 (Maya "
            "2022-2024) and neither is available in this Maya session. "
            "This usually means something's off with the Maya install "
            "itself, not with Nexus Sorter."
        ) from err

# the one real API difference this package actually touches between the
# two bindings: Qt6/PySide6's scoped enum style
# (QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator) vs Qt5/PySide2's
# flatter one (HasCheckIndicator sitting directly on the class). tries the
# newer style first, falls back if that attribute doesn't exist - so this
# doesn't need to know in advance which binding actually loaded above.
try:
    HAS_CHECK_INDICATOR_FEATURE = QtWidgets.QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
except AttributeError:
    HAS_CHECK_INDICATOR_FEATURE = QtWidgets.QStyleOptionViewItem.HasCheckIndicator
