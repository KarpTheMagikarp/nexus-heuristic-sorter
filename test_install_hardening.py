"""
covers the installer's file handling and the commit-path guards added
alongside it. the installer half is plain filesystem work so it runs
against real temp folders, only the maya calls are stubbed.

the original bug: reinstalling on a laptop with OneDrive-synced Documents
died with WinError 5 because rmtree couldn't delete the old __pycache__.
"""
import os
import stat
import sys

import maya.cmds as cmds
import pytest

import drag_to_install_nexus as installer
from nexus_sorter import batch_processor as bp
from nexus_sorter import hierarchy as hier


def _make_source(tmp_path, files):
    src = tmp_path / "src" / "nexus_sorter"
    for rel, body in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return src


def test_reinstall_never_touches_old_pycache(tmp_path, monkeypatch):
    """the actual WinError 5 case. if anything tries to delete or rewrite
    __pycache__ this blows up the same way OneDrive did."""
    src = _make_source(tmp_path, {"__init__.py": "v = 2\n"})
    dest = tmp_path / "scripts" / "nexus_sorter"
    (dest / "__pycache__").mkdir(parents=True)
    (dest / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"old")
    (dest / "__init__.py").write_text("v = 1\n")

    real_remove = os.remove

    def guarded_remove(path, *a, **k):
        if "__pycache__" in str(path):
            raise PermissionError(5, "Access is denied", str(path))
        return real_remove(path, *a, **k)

    monkeypatch.setattr(os, "remove", guarded_remove)
    installer.sync_package(str(src), str(dest))

    assert (dest / "__init__.py").read_text() == "v = 2\n"
    assert (dest / "__pycache__" / "__init__.cpython-311.pyc").exists()


def test_skips_pycache_and_bak_from_source(tmp_path):
    src = _make_source(tmp_path, {
        "__init__.py": "",
        "main_window.py.bak": "junk",
        "__pycache__/main.cpython-311.pyc": "junk",
    })
    dest = tmp_path / "out" / "nexus_sorter"
    installer.sync_package(str(src), str(dest))

    assert (dest / "__init__.py").exists()
    assert not (dest / "main_window.py.bak").exists()
    assert not (dest / "__pycache__").exists()


def test_stale_module_from_old_version_gets_removed(tmp_path):
    src = _make_source(tmp_path, {"__init__.py": "", "naming.py": ""})
    dest = tmp_path / "out" / "nexus_sorter"
    dest.mkdir(parents=True)
    (dest / "retired_module.py").write_text("old")

    installer.sync_package(str(src), str(dest))

    assert not (dest / "retired_module.py").exists()
    assert (dest / "naming.py").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="posix permission bits")
def test_read_only_source_doesnt_poison_next_reinstall(tmp_path):
    """copy2 used to carry read-only bits over, so the second install
    hit access denied on its own files"""
    src = _make_source(tmp_path, {"__init__.py": "v = 1\n"})
    os.chmod(src / "__init__.py", stat.S_IREAD)
    dest = tmp_path / "out" / "nexus_sorter"

    installer.sync_package(str(src), str(dest))
    # mode bits, not os.access, root passes os.access no matter what
    assert os.stat(dest / "__init__.py").st_mode & stat.S_IWUSR


def test_falls_back_when_first_root_is_not_writable(tmp_path, monkeypatch):
    src = _make_source(tmp_path, {"__init__.py": ""})
    blocked = str(tmp_path / "onedrive_docs")
    fallback = str(tmp_path / "appdata")
    real_sync = installer.sync_package

    def sync(source, dest):
        if dest.startswith(blocked):
            raise PermissionError(5, "Access is denied", dest)
        return real_sync(source, dest)

    monkeypatch.setattr(installer, "sync_package", sync)
    root, first_error = installer._install_package(str(src), roots=[blocked, fallback])

    assert root == fallback
    assert isinstance(first_error, PermissionError)
    assert os.path.isfile(os.path.join(fallback, "nexus_sorter", "__init__.py"))


def test_every_root_failing_raises_a_readable_error(tmp_path, monkeypatch):
    def sync(source, dest):
        raise PermissionError(5, "Access is denied", dest)

    monkeypatch.setattr(installer, "sync_package", sync)
    with pytest.raises(installer.InstallError) as exc:
        installer._install_package("unused", roots=[str(tmp_path / "a"), str(tmp_path / "b")])
    assert "OneDrive" in str(exc.value)


def test_shelf_command_carries_its_own_sys_path(tmp_path):
    root = str(tmp_path / "Users" / "someone" / "AppData" / "Local" / "NexusSorter")
    cmd = installer.shelf_command(root)
    compile(cmd, "<shelf>", "exec")  # windows backslashes have to survive as a valid literal
    assert repr(root) in cmd
    assert cmd.index("sys.path.insert") < cmd.index("import nexus_sorter")


def test_reinstall_drops_cached_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, "nexus_sorter_fake_sibling", object())
    snapshot = {k: v for k, v in sys.modules.items() if k == "nexus_sorter" or k.startswith("nexus_sorter.")}
    assert snapshot  # conftest-loaded package has to be there for this to mean anything
    try:
        installer._forget_old_modules()
        assert not any(k in sys.modules for k in snapshot)
        assert "nexus_sorter_fake_sibling" in sys.modules  # prefix match, not substring
    finally:
        # same module objects back, other test files hold references to them
        sys.modules.update(snapshot)


@pytest.fixture
def clean_engine_scene():
    cmds.reset_mock(side_effect=True, return_value=True)
    cmds.progressWindow.side_effect = lambda *a, **k: False if k.get("isCancelled") else None
    cmds.objExists.return_value = False
    cmds.createNode.side_effect = lambda _type, name=None, **_kw: name
    cmds.listRelatives.return_value = []
    cmds.ls.return_value = ["|GRP_02_PROPS|Pr_cart"]
    orig = hier.HierarchyManager.is_node_locked_or_referenced
    hier.HierarchyManager.is_node_locked_or_referenced = classmethod(lambda cls, node: False)
    yield
    hier.HierarchyManager.is_node_locked_or_referenced = orig


def _plan_item():
    return {
        "long_path": "|SM_Cart", "original": "Pr_cart", "proposed": "Pr_cart",
        "destination": "GRP_02_PROPS", "confidence": None, "source": None,
        "is_frozen": True, "is_shielded": False,
    }


def test_failed_parent_is_not_counted_as_sorted(clean_engine_scene):
    cmds.parent.side_effect = RuntimeError("simulated parent failure")
    result = bp.NexusBatchProcessor().execute_sorting_pass([_plan_item()])

    assert result["status"] == "success"
    assert result["count"] == 0
    assert result["move_failed"] == ["|SM_Cart"]


def test_renamed_root_group_aborts_before_moving_anything(clean_engine_scene):
    """createNode handing back GRP_02_PROPS1 used to go unnoticed and
    every parent() aimed at a folder that didn't exist"""
    cmds.createNode.side_effect = lambda _type, name=None, **_kw: f"{name}1" if name == "GRP_02_PROPS" else name
    result = bp.NexusBatchProcessor().execute_sorting_pass([_plan_item()])

    assert result["status"] == "error"
    assert "GRP_02_PROPS1" in result["message"]
    cmds.parent.assert_not_called()
    deleted = {c.args[0] for c in cmds.delete.call_args_list}
    assert {"GRP_02_PROPS1", "|GRP_01_CHARACTERS"} <= deleted  # cleaned up the empty ones it made


def test_full_drop_installs_and_evicts_itself(tmp_path, monkeypatch):
    """the second WinError 5 report wasn't the new code failing, it was
    Maya 2022+ running the cached OLD installer from an earlier drop in
    the same session. after a drop the module has to be gone so the next
    drag imports whatever file was actually dragged in."""
    cmds.reset_mock(side_effect=True, return_value=True)
    cmds.shelfLayout.return_value = []
    monkeypatch.setattr(installer, "default_install_roots", lambda: [str(tmp_path / "scripts")])
    monkeypatch.setattr(installer, "_forget_old_modules", lambda: None)  # other tests hold these modules
    monkeypatch.setitem(sys.modules, installer.__name__, installer)

    installer.onMayaDroppedPythonFile(None)

    assert installer.__name__ not in sys.modules
    assert (tmp_path / "scripts" / "nexus_sorter" / "__init__.py").exists()
    assert not (tmp_path / "scripts" / "nexus_sorter" / "main_window.py.bak").exists()
    shelf_cmd = cmds.shelfButton.call_args.kwargs["command"]
    assert repr(str(tmp_path / "scripts")) in shelf_cmd
    assert cmds.confirmDialog.call_args.kwargs["title"] == "Nexus Sorter"  # success dialog, not the failure one


def test_drop_evicts_itself_even_when_install_blows_up(monkeypatch):
    monkeypatch.setitem(sys.modules, installer.__name__, installer)
    monkeypatch.setattr(installer, "_run_install", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        installer.onMayaDroppedPythonFile(None)
    assert installer.__name__ not in sys.modules
