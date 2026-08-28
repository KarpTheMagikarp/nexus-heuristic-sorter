"""
drag and drop installer for Nexus Sorter. artist drags THIS FILE onto the
Maya viewport once. no folders to find, no sys.path, no restart, everything
after that click is automatic.

distribution layout, all ships together in one folder:

    NexusSorterInstall/
        drag_to_install_nexus.py    <- the only file the artist touches
        nexus_sorter/               <- the actual package (naming, engine, UI...)
        nexus_config.json           <- optional, studio prefix/purge overrides

nexus_config.json stays OUTSIDE the nexus_sorter package on purpose, sitting
next to it instead of nested inside - a TD editing prefixes shouldn't have to
dig into package internals to find it. it's optional: a missing config is a
totally supported state (see nexus_sorter/config.py's _load_nexus_config,
falls back to built-in defaults), so this copies it over if it's there and
just skips it if not, no install failure either way. the package folder
itself is NOT optional though, that's the actual tool.

copies the whole nexus_sorter folder instead of embedding its source inside
this installer, same reason as always: one copy of the code, not two slowly
drifting apart. wipes any existing nexus_sorter folder at the destination
first so a reinstall/update always ends up an exact copy of the source, not
some old file plus some new files mixed together.

Maya calls onMayaDroppedPythonFile(obj) by exact name on drag and drop, only
function it looks for, nothing at module level runs. that's also why
__file__ is reliably defined here (unlike pasting into the Script Editor).
"""

import os
import shutil
import maya.cmds as cmds
import maya.mel as mel

_SHELF_NAME = "Custom"
_SHELF_BUTTON_LABEL = "NexusSorter"
_TOOL_MODULE = "nexus_sorter"
_PACKAGE_NAME = "nexus_sorter"
_OPTIONAL_FILES = ("nexus_config.json",)


def onMayaDroppedPythonFile(_obj):
    """required entry point, signature has to match exactly or Maya
    won't call it at all."""
    try:
        source_dir = os.path.dirname(os.path.abspath(__file__))
        dest_dir = cmds.internalVar(userScriptDir=True)

        source_pkg = os.path.join(source_dir, _PACKAGE_NAME)
        if not os.path.isdir(source_pkg) or not os.path.isfile(os.path.join(source_pkg, "__init__.py")):
            _fail(
                f"Couldn't find the {_PACKAGE_NAME!r} folder next to this installer.\n\n"
                f"Make sure everything was unzipped together before dragging "
                f"this installer in."
            )
            return

        dest_pkg = os.path.join(dest_dir, _PACKAGE_NAME)
        if os.path.isdir(dest_pkg):
            shutil.rmtree(dest_pkg)  # clean slate so an update can't leave old files mixed in
        shutil.copytree(source_pkg, dest_pkg)

        # optional stuff, copied if the TD dropped one in, skipped quietly
        # if not - "quietly" meaning no install failure, not meaning the
        # artist has no way to know. _succeed below still says which one
        # happened so nobody's left guessing why their prefixes didn't show up.
        found_optional = [
            f for f in _OPTIONAL_FILES
            if os.path.isfile(os.path.join(source_dir, f))
        ]
        for filename in found_optional:
            shutil.copy2(
                os.path.join(source_dir, filename),
                os.path.join(dest_dir, filename),
            )

        _create_or_update_shelf_button()
        _succeed(dest_dir, found_optional)

    except Exception as err:
        # never show an artist a raw traceback, tell them what happened
        # in plain language same as everything else in this tool
        _fail(f"Install failed: {err}")


def _create_or_update_shelf_button():
    """idempotent on purpose, dragging this a second time (reinstall,
    update, testing it twice) replaces the button instead of stacking a
    duplicate next to it."""
    top_shelf = mel.eval('global string $gShelfTopLevel; $temp = $gShelfTopLevel;')

    if not cmds.shelfLayout(_SHELF_NAME, exists=True):
        cmds.shelfLayout(_SHELF_NAME, parent=top_shelf)

    for existing_button in (cmds.shelfLayout(_SHELF_NAME, query=True, childArray=True) or []):
        if (cmds.shelfButton(existing_button, query=True, exists=True)
                and cmds.shelfButton(existing_button, query=True, label=True) == _SHELF_BUTTON_LABEL):
            cmds.deleteUI(existing_button)

    cmds.shelfButton(
        parent=_SHELF_NAME,
        label=_SHELF_BUTTON_LABEL,
        annotation="Nexus Sorter - auto-organize scene assets",
        image1="menuIconModify.png",
        command=f"import {_TOOL_MODULE}; {_TOOL_MODULE}.show_ui()",
        sourceType="python",
    )


def _succeed(dest_dir, found_optional):
    if "nexus_config.json" in found_optional:
        config_line = "Studio config (nexus_config.json) installed."
    else:
        config_line = (
            "No nexus_config.json found next to the installer - running "
            "on this tool's built-in default categories and purge settings."
        )

    cmds.confirmDialog(
        title="Nexus Sorter",
        message=(
            f"Installed.\n\nLook for the {_SHELF_BUTTON_LABEL!r} button "
            f"on the {_SHELF_NAME!r} shelf.\n\n"
            f"{config_line}\n\n"
            f"(Files placed in: {dest_dir})"
        ),
        button=["Great"],
    )


def _fail(message):
    cmds.confirmDialog(
        title="Nexus Sorter - Install Failed",
        message=message,
        button=["OK"],
        icon="critical",
    )
