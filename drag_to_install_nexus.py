"""
drag and drop installer for Nexus Sorter. artist drags THIS FILE onto the
Maya viewport once. no folders to find, no sys.path edits, no restart.

distribution layout, all ships together in one folder:

    NexusSorterInstall/
        drag_to_install_nexus.py    <- the only file the artist touches
        nexus_sorter/               <- the actual package
        nexus_config.json           <- optional, studio prefix/purge overrides

never deletes a folder to reinstall. the old version did rmtree then
copytree, and on a lot of real Windows machines Documents is synced by
OneDrive or guarded by Controlled Folder Access, so deleting the old
__pycache__ came back as WinError 5 and the install died halfway. now it
overwrites files in place and leaves __pycache__ alone, python already
throws out a stale .pyc on its own when the source changes.

if Documents/maya isn't writable at all, it installs to a per-user app
data folder instead. the shelf button carries its own sys.path entry, so
it works from either spot without Maya.env edits or a restart.
"""

import importlib
import os
import shutil
import sys

import maya.cmds as cmds
import maya.mel as mel

_SHELF_NAME = "Custom"
_SHELF_BUTTON_LABEL = "NexusSorter"
_PACKAGE_NAME = "nexus_sorter"
_CONFIG_FILE = "nexus_config.json"
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".bak")


class InstallError(Exception):
    pass


def onMayaDroppedPythonFile(_obj):
    """required entry point, signature has to match exactly or Maya
    won't call it at all."""
    try:
        source_dir = os.path.dirname(os.path.abspath(__file__))
        source_pkg = os.path.join(source_dir, _PACKAGE_NAME)
        if not os.path.isfile(os.path.join(source_pkg, "__init__.py")):
            _fail(
                f"Couldn't find the {_PACKAGE_NAME!r} folder next to this installer.\n\n"
                f"Make sure everything was unzipped together before dragging "
                f"this installer in."
            )
            return

        # an open window from the old version has to go before its module
        # does, or its queued Qt events fire into code that no longer exists
        _close_running_ui()

        install_root, primary_error = _install_package(source_pkg)
        config_note = _copy_config(source_dir, install_root)

        _forget_old_modules()
        _create_or_update_shelf_button(install_root)
        _succeed(install_root, primary_error, config_note)

    except InstallError as err:
        _fail(str(err))
    except Exception as err:
        # never show an artist a raw traceback, tell them what happened
        _fail(f"Install failed: {err}")


def default_install_roots():
    """where to try, in order. maya's own scripts folder first since
    that's where a TD would look for it, app data second because it
    isn't synced by OneDrive or covered by Controlled Folder Access."""
    return [cmds.internalVar(userScriptDir=True), fallback_install_root()]


def fallback_install_root():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "NexusSorter")


def _install_package(source_pkg, roots=None):
    """returns (root that worked, error from the first root or None).
    the first error gets kept so the success dialog can say why it
    ended up in the fallback spot instead of just silently moving."""
    roots = roots or default_install_roots()
    errors = []
    for root in roots:
        try:
            sync_package(source_pkg, os.path.join(root, _PACKAGE_NAME))
            return root, (errors[0][1] if errors else None)
        except OSError as err:
            errors.append((root, err))

    details = "\n".join(f"- {root}\n  {err}" for root, err in errors)
    raise InstallError(
        "Install failed, Windows/macOS wouldn't let Maya write to any install folder:\n\n"
        f"{details}\n\n"
        "Usual causes: OneDrive syncing your Documents folder, Windows Security's "
        "'Controlled folder access' blocking Maya, or a locked-down work machine.\n\n"
        "Try pausing OneDrive sync or allowing maya.exe in Controlled folder access, "
        "then drag the installer in again."
    )


def sync_package(source_pkg, dest_pkg):
    """makes dest_pkg match source_pkg without deleting any folders.

    probe write first so a read-only destination fails before a single
    real file changes, that's what lets the caller fall back cleanly
    instead of leaving half an install behind.

    copyfile on purpose, not copy2: copy2 drags over permission bits, so
    a read-only file from a zip or a synced folder lands read-only here
    and the NEXT reinstall hits the same access denied. it also keeps
    the source mtime, which is what python checks to decide if a .pyc
    is stale, a fresh mtime guarantees a recompile."""
    os.makedirs(dest_pkg, exist_ok=True)
    probe = os.path.join(dest_pkg, ".nexus_write_probe")
    with open(probe, "w"):
        pass
    os.remove(probe)

    shipped = set()
    for dirpath, dirnames, filenames in os.walk(source_pkg):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, source_pkg)
        target_dir = os.path.normpath(os.path.join(dest_pkg, rel_dir))
        os.makedirs(target_dir, exist_ok=True)

        for name in filenames:
            if name.endswith(_SKIP_SUFFIXES):
                continue
            target = os.path.join(target_dir, name)
            _make_writable(target)
            shutil.copyfile(os.path.join(dirpath, name), target)
            shipped.add(os.path.normcase(target))

    _remove_stale_files(dest_pkg, shipped)


def _make_writable(path):
    # Windows read-only flag is the other classic WinError 5 on overwrite
    if os.path.exists(path) and not os.access(path, os.W_OK):
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass  # copyfile right after will raise the real error


def _remove_stale_files(dest_pkg, shipped):
    """a module that got renamed or deleted upstream would otherwise
    stay importable forever. best effort only, a file we can't remove is
    dead weight, not a broken install."""
    for dirpath, dirnames, filenames in os.walk(dest_pkg):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.normcase(path) in shipped:
                continue
            try:
                _make_writable(path)
                os.remove(path)
            except OSError:
                pass


def _copy_config(source_dir, install_root):
    """config sits next to the package because config.py looks one level
    up from itself. a failed config copy shouldn't sink the whole
    install, the tool runs fine on built-in defaults."""
    src = os.path.join(source_dir, _CONFIG_FILE)
    if not os.path.isfile(src):
        return "No nexus_config.json found next to the installer, using built-in defaults."
    dest = os.path.join(install_root, _CONFIG_FILE)
    try:
        _make_writable(dest)
        shutil.copyfile(src, dest)
    except OSError as err:
        return f"Couldn't copy nexus_config.json ({err}), using built-in defaults."
    return "Studio config (nexus_config.json) installed."


def _close_running_ui():
    old_window = sys.modules.get(f"{_PACKAGE_NAME}.main_window")
    instance = getattr(old_window, "_nexus_ui_instance", None)
    if instance is not None:
        try:
            instance.close()
            instance.deleteLater()
        except RuntimeError:
            pass
    control = "NexusAutoSorterUIWorkspaceControl"
    if cmds.workspaceControl(control, exists=True):
        cmds.deleteUI(control, control=True)


def _forget_old_modules():
    # without this an update in the same Maya session keeps running the
    # old code, `import nexus_sorter` just hands back the cached module
    for name in [m for m in sys.modules if m == _PACKAGE_NAME or m.startswith(_PACKAGE_NAME + ".")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def shelf_command(install_root):
    # path baked into the button so it doesn't care whether this folder
    # was on sys.path when Maya started, which is the fresh-prefs case
    return (
        "import sys\n"
        f"_nexus_root = {install_root!r}\n"
        "if _nexus_root not in sys.path:\n"
        "    sys.path.insert(0, _nexus_root)\n"
        f"import {_PACKAGE_NAME}\n"
        f"{_PACKAGE_NAME}.show_ui()"
    )


def _create_or_update_shelf_button(install_root):
    """idempotent on purpose, dragging this a second time replaces the
    button instead of stacking a duplicate next to it."""
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
        command=shelf_command(install_root),
        sourceType="python",
    )


def _succeed(install_root, primary_error, config_note):
    lines = [
        "Installed.",
        f"Look for the {_SHELF_BUTTON_LABEL!r} button on the {_SHELF_NAME!r} shelf.",
        config_note,
    ]
    if primary_error is not None:
        lines.append(
            "Maya's scripts folder wasn't writable (often OneDrive or Controlled "
            f"folder access): {primary_error}\nInstalled to a local app data folder instead."
        )
    lines.append(f"(Files placed in: {install_root})")
    cmds.confirmDialog(title="Nexus Sorter", message="\n\n".join(lines), button=["Great"])


def _fail(message):
    cmds.confirmDialog(
        title="Nexus Sorter - Install Failed",
        message=message,
        button=["OK"],
        icon="critical",
    )
