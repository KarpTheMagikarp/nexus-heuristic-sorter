"""
covers analyze_asset_dna's anatomy-first passes, the stuff that routes off
what's actually attached to the node instead of what it's named. nothing
here existed before the VFX shape check got added, analyze_asset_dna had
zero test coverage at all until now.

needs the maya stub from conftest.py since this calls straight into cmds,
unlike test_naming_resolver.py which stays pure string logic.
"""
import pytest
from unittest.mock import MagicMock

import maya.cmds as cmds
from nexus_sorter.categorization import CategorizationEngine, Confidence


@pytest.fixture(autouse=True)
def reset_cmds_mocks():
    """cmds is one shared MagicMock across the whole process (see
    conftest.py), so every test needs a clean slate or an earlier test's
    side_effect leaks into the next one. reset_mock() alone does NOT
    clear side_effect, only call history - found this the hard way when
    a dispatch function from one test kept answering the next test's
    calls even after this fixture reset return_value."""
    cmds.reset_mock(side_effect=True, return_value=True)
    cmds.listRelatives.return_value = []
    cmds.nodeType.return_value = "mesh"
    yield


VFX_SHAPE_TYPES = [
    "nParticle", "particle", "nCloth", "hairSystem", "fluidShape",
    "bifrostGraphShape", "instancer",
]


@pytest.mark.parametrize("vfx_shape_type", VFX_SHAPE_TYPES)
def test_vfx_shape_routes_to_vfx_regardless_of_name(vfx_shape_type):
    """the whole point: a real sim shape wins even against a name that
    reads as a totally different category, same as a joint chain already
    beats a name for characters."""
    cmds.listRelatives.return_value = ["|SM_Barrel|SM_BarrelShape"]
    cmds.nodeType.return_value = vfx_shape_type

    dest, confidence, needs_shield = CategorizationEngine.analyze_asset_dna(
        "|SM_Barrel", "SM_Barrel"
    )
    assert dest == "GRP_04_VFX"
    assert confidence == Confidence.HIGH
    assert needs_shield is True


def test_vfx_detection_does_not_fire_on_a_plain_mesh():
    """a normal mesh shape must not accidentally match, or every prop
    in the scene would get routed to VFX."""
    def list_relatives_dispatch(*args, **kwargs):
        if kwargs.get("type") == "joint":
            return []  # no joints under this node
        if kwargs.get("shapes"):
            return ["|SM_Barrel|SM_BarrelShape"]
        return []
    cmds.listRelatives.side_effect = list_relatives_dispatch
    cmds.nodeType.return_value = "mesh"
    cmds.listConnections.return_value = []
    cmds.sets.return_value = []

    dest, _confidence, needs_shield = CategorizationEngine.analyze_asset_dna(
        "|SM_Barrel", "SM_Barrel"
    )
    assert dest != "GRP_04_VFX"
    assert needs_shield is False


def test_light_check_still_wins_when_checked_before_vfx():
    """light detection has to stay ahead of the new VFX check in the
    function, this pins the order down as a real test instead of just
    trusting the code doesn't drift."""
    cmds.listRelatives.return_value = ["|Key|KeyShape"]
    cmds.nodeType.return_value = "directionalLight"

    dest, confidence, needs_shield = CategorizationEngine.analyze_asset_dna("|Key", "Key")
    assert dest == "GRP_05_LIGHTING"
    assert confidence == Confidence.HIGH
    assert needs_shield is False


def _joint_count_dispatch(joint_count, curve_count=0):
    """cmds.listRelatives gets called with different `type` kwargs for
    different questions (joints, control curves, shapes) - a blanket
    return_value answers all of them identically, which is exactly the
    mock bug that broke the plain-mesh VFX test earlier. dispatch by
    kwarg instead so each query gets the right answer."""
    def dispatch(*args, **kwargs):
        if kwargs.get("type") == "joint":
            return [f"|Hinge|joint{i}" for i in range(joint_count)]
        if kwargs.get("type") == "nurbsCurve":
            return [f"|Hinge|curve{i}" for i in range(curve_count)]
        if kwargs.get("shapes"):
            return []
        return []
    return dispatch


@pytest.mark.parametrize("joint_count", [1, 2])
def test_low_joint_count_is_weak_signal_not_veto(joint_count):
    """a hinge or a chest lid gets by on 1-2 joints same as a real rig's
    first bone. below the threshold, that should nudge the score, not
    veto everything else outright - this is the actual bug fix."""
    cmds.listRelatives.side_effect = _joint_count_dispatch(joint_count, curve_count=0)
    cmds.nodeType.return_value = "transform"

    scores, needs_shield = CategorizationEngine.score_node_anatomy("|Hinge")
    assert scores.get("GRP_01_CHARACTERS", 0) < CategorizationEngine.SCORE_VETO
    assert needs_shield is False


def test_joint_count_at_threshold_still_vetoes():
    """a real joint chain has to keep working exactly like before,
    this fix only changes the 1-2 joint case."""
    cmds.listRelatives.side_effect = _joint_count_dispatch(
        CategorizationEngine._MIN_JOINTS_FOR_RIG_VETO, curve_count=0
    )
    cmds.nodeType.return_value = "transform"

    scores, needs_shield = CategorizationEngine.score_node_anatomy("|Skeleton")
    assert scores["GRP_01_CHARACTERS"] == CategorizationEngine.SCORE_VETO
    assert needs_shield is True


def test_low_joint_count_with_control_curves_still_vetoes():
    """joints plus control curves together is as strong a tell as a full
    joint chain on its own - a 1-joint tail with an FK circle control is
    still obviously a rig, not a hinge."""
    cmds.listRelatives.side_effect = _joint_count_dispatch(1, curve_count=1)
    cmds.nodeType.return_value = "transform"

    scores, needs_shield = CategorizationEngine.score_node_anatomy("|TailControl")
    assert scores["GRP_01_CHARACTERS"] == CategorizationEngine.SCORE_VETO
    assert needs_shield is True


def test_bare_joint_node_still_vetoes_regardless_of_threshold():
    """selecting the joint itself has to keep working the same way it
    always did, the threshold only applies to descendant joint counts."""
    cmds.listRelatives.side_effect = _joint_count_dispatch(0, curve_count=0)
    cmds.nodeType.return_value = "joint"

    scores, needs_shield = CategorizationEngine.score_node_anatomy("|Skeleton|root_joint")
    assert scores["GRP_01_CHARACTERS"] == CategorizationEngine.SCORE_VETO
    assert needs_shield is True
