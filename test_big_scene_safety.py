"""
two holes that only show up past test-scene size.

1. shared-material and sibling checks re-walked the whole material /
   sibling list for every object, n^2 maya calls. ~1M calls for a 1000
   piece kit on one material, which reads as Maya freezing on Analyze.
2. quarantine only held the empty folder. the next Analyze collected
   everything inside GRP_DEPRECATED_FOLDERS and sorted it back out.
"""
import maya.cmds as cmds
import pytest

from nexus_sorter import batch_processor as bp
from nexus_sorter import categorization as cat
from nexus_sorter.categorization import CategorizationEngine as CE
from nexus_sorter.categorization import Confidence

ENV = "GRP_03_ENVIRONMENT"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    cmds.reset_mock(side_effect=True, return_value=True)
    monkeypatch.setattr(cat, "_load_nexus_config", lambda: {})
    monkeypatch.setattr(CE, "_pass_cache", None)
    # real lookup goes through OpenMaya, which is a mock here and would hand
    # score_name_tokens a MagicMock that "starts with" every prefix
    monkeypatch.setattr(cat.HierarchyManager, "get_safe_short_name",
                        staticmethod(lambda path: path.rsplit("|", 1)[-1]))
    CE.reset_prefix_map_cache()
    yield
    CE.reset_prefix_map_cache()


def _kit(n_objects, members=None, group="|grp001", names=None):
    """n objects in one group, all on one material. dispatch by kwarg so
    each kind of listRelatives question gets its own answer."""
    names = names or [f"piece{i}" for i in range(n_objects)]
    objs = [f"{group}|{name}" for name in names]
    members = members if members is not None else [f"{name}Shape" for name in names]

    def list_relatives(node, **kw):
        if kw.get("shapes"):
            return [f"{node}|{node.rsplit('|', 1)[-1]}Shape"]
        if kw.get("children"):
            return list(objs)
        if kw.get("parent"):
            nodes = node if isinstance(node, list) else [node]
            return [group if n.startswith(group + "|") and n.count("|") == group.count("|") + 1
                    else f"{group}|{n.split('.')[0].replace('Shape', '')}" for n in nodes]
        return []

    cmds.listRelatives.side_effect = list_relatives
    cmds.listConnections.side_effect = lambda *a, **k: ["rockSG"]
    cmds.sets.side_effect = lambda sg, query=False: list(members)
    return objs


def test_material_members_get_walked_once_per_pass_not_once_per_object():
    objs = _kit(50)
    with CE.analysis_pass():
        assert all(CE.has_shared_material(o) for o in objs)
    assert cmds.sets.call_count == 1


def test_same_answer_with_or_without_the_cache():
    objs = _kit(5)
    uncached = [CE.has_shared_material(o) for o in objs]
    with CE.analysis_pass():
        assert [CE.has_shared_material(o) for o in objs] == uncached


@pytest.mark.parametrize("members, shared", [
    (["aShape", "bShape", "cShape"], True),                        # 3 owners clears the bar
    (["aShape", "bShape"], False),                                  # 2 doesn't, same threshold as before
    (["aShape.f[0:5]", "aShape.f[9]", "bShape.f[2]"], False),       # face assignments collapse to their node
])
def test_owner_threshold_unchanged(members, shared):
    objs = _kit(1, members=members)
    assert CE.has_shared_material(objs[0]) is shared


def test_cache_never_outlives_the_pass():
    """the scene can change between Analyze clicks, a stale owner count
    would be a silent wrong answer next time"""
    objs = _kit(3)
    with CE.analysis_pass():
        CE.has_shared_material(objs[0])
        with CE.analysis_pass():  # nested pass reuses the outer one
            CE.has_shared_material(objs[1])
        assert cmds.sets.call_count == 1
    assert CE._pass_cache is None


def test_sibling_lookup_walks_a_group_once():
    objs = _kit(40, names=[f"piece{i}" for i in range(39)] + ["rock"])
    with CE.analysis_pass():
        results = {CE.score_hierarchy_context(o) for o in objs[:-1]}
    assert results == {(ENV, CE.SCORE_HIERARCHY_SIBLING)}
    children_calls = [c for c in cmds.listRelatives.call_args_list if c.kwargs.get("children")]
    assert len(children_calls) == 1


def test_sibling_lookup_skips_the_asset_itself():
    """same rule as the old loop: an asset never borrows its own answer"""
    objs = _kit(3, names=["rock", "boulder", "piece"])
    with CE.analysis_pass():
        assert CE.score_hierarchy_context(objs[2]) == (ENV, CE.SCORE_HIERARCHY_SIBLING)
        # rock asking still gets an answer from boulder, the second hit
        assert CE.score_hierarchy_context(objs[0]) == (ENV, CE.SCORE_HIERARCHY_SIBLING)


@pytest.fixture
def plan_env(monkeypatch):
    hm = bp.HierarchyManager
    monkeypatch.setattr(hm, "get_dag_path", staticmethod(lambda node: True))
    monkeypatch.setattr(hm, "get_safe_short_name", staticmethod(lambda node: node.strip("|").split("|")[-1]))
    monkeypatch.setattr(hm, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))
    monkeypatch.setattr(CE, "analyze_asset_dna", classmethod(lambda cls, node, name: (ENV, Confidence.MEDIUM, False)))
    monkeypatch.setattr(CE, "check_frozen_transforms", staticmethod(lambda node: True))
    monkeypatch.setattr(bp.NexusBatchProcessor, "_remembered_category", lambda self, node: None)
    cmds.listRelatives.side_effect = lambda shape, **kw: [shape.rsplit("|", 1)[0]] if kw.get("parent") else []
    cmds.objExists.return_value = False
    cmds.ls.return_value = []


def _plan(targets):
    return [item["long_path"] for item in bp.NexusBatchProcessor().analyze_sorting_plan(targets, "Auto-Detect")["plan"]]


def test_quarantined_objects_stay_out_of_the_plan(plan_env, monkeypatch):
    queried = []

    def is_root(cls, node):
        queried.append(node)
        return node == "|GRP_DEPRECATED_FOLDERS"

    monkeypatch.setattr(bp.NexusBatchProcessor, "is_quarantine_root", classmethod(is_root))
    planned = _plan([
        "|GRP_DEPRECATED_FOLDERS|_PROPS1|crate|crateShape",
        "|GRP_DEPRECATED_FOLDERS|_PROPS1|barrel|barrelShape",
        "|level|rock|rockShape",
    ])
    assert planned == ["|level|rock"]
    assert sorted(queried) == ["|GRP_DEPRECATED_FOLDERS", "|level"]  # once per top group, not per object


def test_folder_that_only_shares_the_name_is_not_quarantine(plan_env, monkeypatch):
    """the stamp is the source of truth, same as purge. an artist's own
    folder that happens to have this name still gets sorted normally"""
    monkeypatch.setattr(bp.NexusBatchProcessor, "is_quarantine_root", classmethod(lambda cls, node: False))
    assert _plan(["|GRP_DEPRECATED_FOLDERS|crate|crateShape"]) == ["|GRP_DEPRECATED_FOLDERS|crate"]
