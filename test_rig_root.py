"""
the rig shield used to climb straight to the top-level group, so a
character inside |LEVEL dragged the whole level into characters.
find_rig_root anchors on the skeleton instead and only climbs while the
group above holds nothing but that rig.

the failure that matters most here is the opposite one, splitting a rig
apart. most of these tests are about NOT doing that.

a tiny fake scene answers the handful of cmds queries the rig logic
makes, mocking each call by hand gets unreadable fast for hierarchy stuff.
"""
import maya.cmds as cmds
import pytest

from nexus_sorter import batch_processor as bp
from nexus_sorter.categorization import AssignmentSource
from nexus_sorter.hierarchy import HierarchyManager as HM
from nexus_sorter.hierarchy import RigSpansGroups

RESERVED = {"GRP_01_CHARACTERS", "GRP_02_PROPS", "GRP_03_ENVIRONMENT", "GRP_04_VFX", "GRP_05_LIGHTING",
            "UNASSIGNED_ASSETS"}


class FakeScene:
    def __init__(self):
        self.types = {}     # long path -> node type, plus a few DG nodes by bare name
        self.skins = {}     # skinCluster name -> joints it's bound to
        self.inputs = {}    # node -> nodes feeding it (one hop upstream)

    def add(self, path, node_type="transform"):
        parts = path.strip("|").split("|")
        for i in range(1, len(parts)):
            self.types.setdefault("|" + "|".join(parts[:i]), "transform")
        self.types[path] = node_type
        return path

    def mesh(self, transform, skinned_to=None, driven_by=None):
        self.add(transform)
        shape = self.add(f"{transform}|{transform.rsplit('|', 1)[-1]}Shape", "mesh")
        if skinned_to:
            skin = f"skin_{len(self.skins)}"
            self.types[skin] = "skinCluster"
            self.skins[skin] = list(skinned_to)
            self.inputs[shape] = [skin]
        if driven_by:
            constraint = f"{transform}_parentConstraint"
            self.types[constraint] = "parentConstraint"
            self.inputs[constraint] = list(driven_by)
            self.inputs[transform] = [constraint]
        return transform

    def joints(self, *paths):
        return [self.add(p, "joint") for p in paths]

    def _children(self, node):
        depth = node.count("|")
        return sorted(p for p in self.types if p.startswith(node + "|") and p.count("|") == depth + 1)

    def _descendants(self, node):
        return sorted(p for p in self.types if p.startswith(node + "|"))

    def _type_ok(self, node, node_type):
        if node_type is None:
            return True
        wanted = node_type if isinstance(node_type, (list, tuple)) else [node_type]
        return self.types.get(node) in wanted

    def ls(self, nodes=None, type=None, long=False, noIntermediate=False, **_kw):
        nodes = [nodes] if isinstance(nodes, str) else list(nodes or [])
        return [n for n in nodes if n in self.types and self._type_ok(n, type)]

    def listRelatives(self, nodes, parent=False, children=False, allDescendents=False, shapes=False,
                      type=None, fullPath=False, noIntermediate=False, **_kw):
        nodes = [nodes] if isinstance(nodes, str) else list(nodes)
        out = []
        for node in nodes:
            if parent:
                up = node.rsplit("|", 1)[0]
                out += [up] if up else []
            elif shapes:
                out += [c for c in self._children(node) if self.types[c] not in ("transform", "joint")]
            elif children:
                out += self._children(node)
            elif allDescendents:
                out += [d for d in self._descendants(node) if self._type_ok(d, type)]
        return out or None

    def listConnections(self, nodes, source=True, destination=False, **_kw):
        nodes = [nodes] if isinstance(nodes, str) else list(nodes)
        return [src for n in nodes for src in self.inputs.get(n, [])] or None

    def skinCluster(self, skin, query=False, influence=False):
        return list(self.skins.get(skin, []))

    def install(self):
        for name in ("ls", "listRelatives", "listConnections", "skinCluster"):
            getattr(cmds, name).side_effect = getattr(self, name)


@pytest.fixture
def scene():
    cmds.reset_mock(side_effect=True, return_value=True)
    fake = FakeScene()
    yield fake


def _character(scene, root, layout="split"):
    """layout split: geo and skeleton in separate groups under root, the
    normal rig layout. returns (body mesh transform, joints)"""
    jnts = scene.joints(f"{root}|skel|root_jnt", f"{root}|skel|root_jnt|spine_jnt", f"{root}|skel|root_jnt|spine_jnt|head_jnt")
    body = scene.mesh(f"{root}|geo|body", skinned_to=jnts)
    scene.add(f"{root}|ctrls|root_ctrl")
    scene.add(f"{root}|ctrls|root_ctrl|root_ctrlShape", "nurbsCurve")
    return body, jnts


def test_character_inside_a_level_stops_at_its_own_rig_group(scene):
    """the actual bug: this used to return |LEVEL"""
    body, _ = _character(scene, "|LEVEL|chars|hero_rig")
    _character(scene, "|LEVEL|chars|villain_rig")
    scene.mesh("|LEVEL|props|crate")
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|LEVEL|chars|hero_rig"


def test_standalone_rig_at_world_matches_old_behavior(scene):
    body, _ = _character(scene, "|hero_rig")
    scene.mesh("|crate")
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|hero_rig"


def test_climbs_through_groups_that_only_hold_this_rig(scene):
    body, _ = _character(scene, "|hero|hero_rig")
    scene.mesh("|hero_other_char|geo|body", skinned_to=scene.joints("|hero_other_char|skel|j"))
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|hero"


def test_controls_and_proxy_geo_outside_the_skeleton_group_stay_with_the_rig(scene):
    """the split case: mesh and skeleton share a LOWER group than the rig
    root, with controls and constraint-driven proxy geo sitting beside it.
    stopping at the lower group would leave those behind"""
    jnts = scene.joints("|hero_rig|deform|skel|root_jnt", "|hero_rig|deform|skel|root_jnt|arm_jnt")
    body = scene.mesh("|hero_rig|deform|geo|body", skinned_to=jnts)
    scene.add("|hero_rig|ctrls|arm_ctrl")
    scene.add("|hero_rig|ctrls|arm_ctrl|arm_ctrlShape", "nurbsCurve")
    scene.mesh("|hero_rig|proxy|arm_proxy", driven_by=["|hero_rig|deform|skel|root_jnt|arm_jnt"])
    scene.joints("|hero_rig|ik_helpers|ik_jnt")  # unskinned helper joints never count as foreign
    scene.mesh("|crate")
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|hero_rig"


def test_unrelated_static_mesh_next_to_the_rig_stops_the_climb(scene):
    body, _ = _character(scene, "|LEVEL|hero_rig")
    scene.mesh("|LEVEL|rock")
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|LEVEL|hero_rig"


def test_prop_with_hinge_joints_under_its_own_mesh(scene):
    """joint-veto case where the joints live under the mesh transform"""
    chest = scene.mesh("|LEVEL|props|chest")
    scene.joints("|LEVEL|props|chest|hinge_a", "|LEVEL|props|chest|hinge_a|hinge_b", "|LEVEL|props|chest|hinge_c")
    scene.mesh("|LEVEL|props|crate")
    scene.install()
    assert HM.find_rig_root(chest, RESERVED) == "|LEVEL|props|chest"


def test_already_sorted_rig_stops_at_our_folder(scene):
    body, _ = _character(scene, "|GRP_01_CHARACTERS|hero_rig")
    _character(scene, "|GRP_01_CHARACTERS|villain_rig")
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|GRP_01_CHARACTERS|hero_rig"


def test_no_skeleton_means_no_answer_so_the_old_climb_runs(scene):
    """blendShape-only meshes and sims have nothing to anchor on"""
    face = scene.mesh("|LEVEL|hero_rig|geo|brows")
    scene.install()
    assert HM.find_rig_root(face, RESERVED) is None


def test_blender_armature_next_to_its_mesh_is_flagged_not_split(scene):
    """the armature + shirt case. nothing holds both, so moving the mesh
    would leave the skeleton behind. flagged so the caller leaves it alone"""
    jnts = scene.joints("|armature|root_jnt", "|armature|root_jnt|spine_jnt")
    shirt = scene.mesh("|FBXASC055_shirt_0_0_0", skinned_to=jnts)
    scene.install()
    with pytest.raises(RigSpansGroups):
        HM.find_rig_root(shirt, RESERVED)


def test_mesh_and_skeleton_sorted_apart_into_our_folder_is_flagged(scene):
    jnts = scene.joints("|GRP_01_CHARACTERS|armature|root_jnt")
    shirt = scene.mesh("|GRP_01_CHARACTERS|shirt", skinned_to=jnts)
    scene.install()
    with pytest.raises(RigSpansGroups):
        HM.find_rig_root(shirt, RESERVED)


def test_cape_stored_away_from_its_character_is_flagged(scene):
    """only shared group is |LEVEL, which also holds another character.
    treating |LEVEL as this cape's rig would be the original bug again"""
    hero_jnts = scene.joints("|LEVEL|chars|hero_rig|skel|root_jnt")
    scene.mesh("|LEVEL|chars|hero_rig|geo|body", skinned_to=hero_jnts)
    _character(scene, "|LEVEL|chars|villain_rig")
    cape = scene.mesh("|LEVEL|props|cape", skinned_to=hero_jnts)
    scene.install()
    with pytest.raises(RigSpansGroups):
        HM.find_rig_root(cape, RESERVED)


def test_rig_holding_blendshape_targets_is_not_flagged(scene):
    """loose static meshes inside a rig group are normal, skipping every
    rig that kept its targets around would make the tool useless for rigs"""
    body, _ = _character(scene, "|LEVEL|hero_rig")
    scene.mesh("|LEVEL|hero_rig|bs_targets|smile")
    scene.mesh("|LEVEL|hero_rig|bs_targets|frown")
    scene.mesh("|LEVEL|rock")
    scene.install()
    assert HM.find_rig_root(body, RESERVED) == "|LEVEL|hero_rig"


def test_every_mesh_of_one_rig_lands_on_the_same_root(scene):
    jnts = scene.joints("|LEVEL|hero_rig|skel|root_jnt", "|LEVEL|hero_rig|skel|root_jnt|head_jnt")
    meshes = [scene.mesh(f"|LEVEL|hero_rig|geo|{part}", skinned_to=jnts) for part in ("body", "head", "boots")]
    scene.mesh("|LEVEL|rock")
    scene.install()
    memo = {}
    assert {HM.find_rig_root(m, RESERVED, memo=memo) for m in meshes} == {"|LEVEL|hero_rig"}


def test_plan_falls_back_to_the_old_climb_when_there_is_no_skeleton(monkeypatch):
    cmds.reset_mock(side_effect=True, return_value=True)
    hm = bp.HierarchyManager
    monkeypatch.setattr(hm, "get_dag_path", staticmethod(lambda node: True))
    monkeypatch.setattr(hm, "get_safe_short_name", staticmethod(lambda node: node.strip("|").split("|")[-1]))
    monkeypatch.setattr(hm, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))
    monkeypatch.setattr(hm, "find_rig_root", classmethod(lambda cls, node, reserved, memo=None: None))
    monkeypatch.setattr(hm, "get_root_ancestor", staticmethod(lambda node, reserved: "|LEVEL"))
    monkeypatch.setattr(bp.CategorizationEngine, "analyze_asset_dna",
                        classmethod(lambda cls, node, name: ("GRP_01_CHARACTERS", bp.Confidence.HIGH, True)))
    monkeypatch.setattr(bp.CategorizationEngine, "check_frozen_transforms", staticmethod(lambda node: True))
    monkeypatch.setattr(bp.NexusBatchProcessor, "_remembered_category", lambda self, node: None)
    monkeypatch.setattr(bp.NexusBatchProcessor, "is_quarantine_root", classmethod(lambda cls, node: False))
    cmds.listRelatives.side_effect = lambda shape, **kw: [shape.rsplit("|", 1)[0]] if kw.get("parent") else []
    cmds.objExists.return_value = False
    cmds.ls.return_value = []

    plan = bp.NexusBatchProcessor().analyze_sorting_plan(["|LEVEL|brows|browsShape"], "Auto-Detect")["plan"]
    assert [item["long_path"] for item in plan] == ["|LEVEL"]


def _plan_env(monkeypatch, rig_root):
    cmds.reset_mock(side_effect=True, return_value=True)
    hm = bp.HierarchyManager
    monkeypatch.setattr(hm, "get_dag_path", staticmethod(lambda node: True))
    monkeypatch.setattr(hm, "get_safe_short_name", staticmethod(lambda node: node.strip("|").split("|")[-1]))
    monkeypatch.setattr(hm, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))
    monkeypatch.setattr(hm, "find_rig_root", classmethod(rig_root))
    monkeypatch.setattr(bp.CategorizationEngine, "analyze_asset_dna",
                        classmethod(lambda cls, node, name: ("GRP_01_CHARACTERS", bp.Confidence.HIGH, True)))
    monkeypatch.setattr(bp.CategorizationEngine, "check_frozen_transforms", staticmethod(lambda node: True))
    monkeypatch.setattr(bp.NexusBatchProcessor, "_remembered_category", lambda self, node: None)
    monkeypatch.setattr(bp.NexusBatchProcessor, "is_quarantine_root", classmethod(lambda cls, node: False))
    cmds.listRelatives.side_effect = lambda shape, **kw: [shape.rsplit("|", 1)[0]] if kw.get("parent") else []
    cmds.objExists.return_value = False
    cmds.ls.return_value = []


def _raise_split(cls, node, reserved, memo=None):
    raise RigSpansGroups(node)


def test_split_rig_lands_in_skipped_with_its_name_untouched(monkeypatch):
    _plan_env(monkeypatch, _raise_split)
    item = bp.NexusBatchProcessor().analyze_sorting_plan(["|shirt|shirtShape"], "Auto-Detect")["plan"][0]
    assert item["source"] == AssignmentSource.SKIPPED_SPLIT_RIG
    assert (item["long_path"], item["proposed"]) == ("|shirt", "shirt")
    assert "skeleton" in item["skip_reason"]


def test_split_rig_is_skipped_even_under_a_forced_override(monkeypatch):
    """the override picks a folder, it doesn't make splitting a rig safe"""
    _plan_env(monkeypatch, _raise_split)
    item = bp.NexusBatchProcessor().analyze_sorting_plan(["|shirt|shirtShape"], "02_PROPS")["plan"][0]
    assert item["source"] == AssignmentSource.SKIPPED_SPLIT_RIG


@pytest.mark.parametrize("source", [AssignmentSource.SKIPPED_SPLIT_RIG, AssignmentSource.SKIPPED_REFERENCED])
def test_commit_never_moves_or_renames_a_skipped_row(monkeypatch, source):
    cmds.reset_mock(side_effect=True, return_value=True)
    monkeypatch.setattr(bp.HierarchyManager, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))
    cmds.progressWindow.side_effect = lambda *a, **k: False if k.get("isCancelled") else None
    cmds.objExists.return_value = False
    cmds.createNode.side_effect = lambda _type, name=None, **_kw: name
    cmds.listRelatives.return_value = []

    item = {"long_path": "|shirt", "original": "shirt", "proposed": "shirt", "destination": "UNASSIGNED_ASSETS",
            "confidence": bp.Confidence.LOW, "source": source, "is_frozen": True, "is_shielded": True}
    result = bp.NexusBatchProcessor().execute_sorting_pass([item])

    assert result["count"] == 0
    cmds.parent.assert_not_called()
    cmds.rename.assert_not_called()
    assert not [c for c in cmds.setAttr.call_args_list if c.args and str(c.args[0]).startswith("|shirt.")]
