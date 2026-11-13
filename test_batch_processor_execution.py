"""
regression tests for two real bugs found and fixed once already: the
MayaUndoChunk import that went missing during the module split (NameError
crashed every commit/purge, and orphaned the progress window since its
cleanup never got a chance to run), and the exception handling in
execute_sorting_pass that used to swallow programming bugs into a generic
"Sort failed" message instead of letting them surface loudly.

both of these were only ever verified with one-off debugging scripts that
got thrown away once the fix shipped. this is that verification made
permanent, so neither regression can come back silently.
"""
import pytest

import maya.cmds as cmds
from nexus_sorter import batch_processor as bp
from nexus_sorter import hierarchy as hier
from nexus_sorter import categorization as cat


def _progress_window_dispatch(*args, **kwargs):
    if kwargs.get("query") and kwargs.get("isCancelled"):
        return False
    return None


@pytest.fixture(autouse=True)
def unlocked_scene():
    """minimal scene mock where nothing's referenced/locked, so a plan
    item actually reaches the real move/rename logic instead of getting
    filtered out as skipped before any of this matters. cmds is one
    shared mock across the whole process, needs a clean slate per test."""
    cmds.reset_mock(side_effect=True, return_value=True)
    cmds.progressWindow.side_effect = _progress_window_dispatch
    cmds.objExists.return_value = False
    # echoes the requested name back like real maya does when it's free,
    # a fixed return value would now trip the rename-collision guard
    cmds.createNode.side_effect = lambda _type, name=None, **_kw: name
    cmds.listRelatives.return_value = []
    cmds.rename.return_value = "|GRP_02_PROPS|Pr_cart"
    cmds.parent.return_value = ["Pr_cart"]
    cmds.ls.return_value = ["|GRP_02_PROPS|Pr_cart"]

    orig_locked_check = hier.HierarchyManager.is_node_locked_or_referenced
    hier.HierarchyManager.is_node_locked_or_referenced = classmethod(lambda cls, node: False)
    yield
    hier.HierarchyManager.is_node_locked_or_referenced = orig_locked_check


def _sample_plan_item(is_frozen=True):
    return {
        "long_path": "|SM_Cart|SM_CartShape", "original": "SM_Cart", "proposed": "Pr_cart",
        "destination": "GRP_02_PROPS", "confidence": None, "source": None,
        "is_frozen": is_frozen, "is_shielded": False,
    }


def test_execute_sorting_pass_does_not_raise_nameerror():
    """regression test for the MayaUndoChunk import going missing during
    the module split. execute_sorting_pass used to crash every commit
    and purge with NameError: name 'MayaUndoChunk' is not defined - this
    just has to complete without raising."""
    engine = bp.NexusBatchProcessor()
    result = engine.execute_sorting_pass([_sample_plan_item()], freeze_eligible=False, quarantine_folder_paths=[])
    assert result["status"] in ("success", "error")  # anything but a raised NameError


def test_programming_bug_escapes_the_engine_boundary_uncaught():
    """NameError/AttributeError/ImportError inside the sorting loop mean
    an actual coding defect, not a legitimate scene condition. these have
    to escape loudly here, not get silently reported as a generic "Sort
    failed" message the way the old broad except used to swallow them."""
    orig = cat.CategorizationEngine.is_freeze_blocked
    cat.CategorizationEngine.is_freeze_blocked = staticmethod(
        lambda *a, **k: (_ for _ in ()).throw(NameError("simulated typo bug"))
    )
    try:
        with pytest.raises(NameError):
            engine = bp.NexusBatchProcessor()
            engine.execute_sorting_pass(
                [_sample_plan_item(is_frozen=False)], freeze_eligible=True, quarantine_folder_paths=[],
            )
    finally:
        cat.CategorizationEngine.is_freeze_blocked = orig


def test_genuine_runtime_failure_is_caught_and_reported_cleanly():
    """a real maya command failure mid-batch is a legitimate condition,
    not a coding bug - has to be caught and reported through the normal
    status="error" payload instead of crashing the whole tool."""
    cmds.createNode.side_effect = RuntimeError("simulated: could not create transform node")
    engine = bp.NexusBatchProcessor()
    result = engine.execute_sorting_pass([_sample_plan_item()], freeze_eligible=False, quarantine_folder_paths=[])
    assert result["status"] == "error"
    assert "simulated" in result["message"]
