"""
renames used to go through an OpenMaya MDagModifier, which never lands on
Maya's undo queue outside a command plugin. ctrl+z after a sort restored
the hierarchy and left every new name behind. these pin the switch to
cmds.rename and the namespace handling that came with it.
"""
import maya.api.OpenMaya as om
import maya.cmds as cmds
import pytest

from nexus_sorter.hierarchy import HierarchyManager


@pytest.fixture(autouse=True)
def unlocked(monkeypatch):
    cmds.reset_mock(side_effect=True, return_value=True)
    om.reset_mock()
    monkeypatch.setattr(HierarchyManager, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))


def _uuid_lookup(final_long_path):
    def ls(node, uuid=False, long=False, **_kw):
        if uuid:
            return ["UUID-1"]
        return [final_long_path] if node == "UUID-1" else []
    return ls


def test_rename_goes_through_undoable_cmds_not_the_api():
    cmds.ls.side_effect = _uuid_lookup("|Env_rock")
    assert HierarchyManager.execute_safe_rename("|rock", "Env_rock") == "|Env_rock"
    cmds.rename.assert_called_once_with("|rock", ":Env_rock", ignoreShape=True)
    om.MDagModifier.assert_not_called()


def test_node_keeps_its_own_namespace():
    """a bare name would drop into whatever namespace is current, so the
    rocks would quietly leave stylized_rocks: on rename"""
    cmds.ls.side_effect = _uuid_lookup("|stylized_rocks:Env_stylized_rock_01")
    result = HierarchyManager.execute_safe_rename("|stylized_rocks:stylized_rock_01", "Env_stylized_rock_01")
    cmds.rename.assert_called_once_with(
        "|stylized_rocks:stylized_rock_01", ":stylized_rocks:Env_stylized_rock_01", ignoreShape=True)
    assert result == "|stylized_rocks:Env_stylized_rock_01"


def test_nested_namespace_and_parent_path():
    cmds.ls.side_effect = _uuid_lookup("|grp|a:b:Env_rock")
    HierarchyManager.execute_safe_rename("|grp|a:b:rock", "Env_rock")
    assert cmds.rename.call_args.args[1] == ":a:b:Env_rock"


def test_result_comes_from_uuid_not_the_requested_name():
    """maya appends digits on a clash, the returned path has to be what
    actually exists or every later step targets a node that isn't there"""
    cmds.ls.side_effect = _uuid_lookup("|Env_rock1")
    assert HierarchyManager.execute_safe_rename("|rock", "Env_rock") == "|Env_rock1"


def test_failed_rename_returns_none():
    cmds.ls.side_effect = _uuid_lookup("|rock")
    cmds.rename.side_effect = RuntimeError("simulated")
    assert HierarchyManager.execute_safe_rename("|rock", "Env_rock") is None


def test_locked_node_is_never_touched(monkeypatch):
    monkeypatch.setattr(HierarchyManager, "is_node_locked_or_referenced", classmethod(lambda cls, node: True))
    assert HierarchyManager.execute_safe_rename("|rock", "Env_rock") is None
    cmds.rename.assert_not_called()
