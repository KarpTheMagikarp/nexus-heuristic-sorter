"""
covers the categorization rework: rigged no longer automatically means
character, name words get counted instead of first-match-wins, studio
words from config, and the tool remembering human decisions between runs.

the rig SHIELD itself isn't supposed to change anywhere in here, a few
tests pin that down explicitly since it's the part that protects rigs.
"""
from types import SimpleNamespace

import maya.cmds as cmds
import pytest

from nexus_sorter import batch_processor as bp
from nexus_sorter import categorization as cat
from nexus_sorter.categorization import AssignmentSource, CategorizationEngine, Confidence
from nexus_sorter.main_window import NexusSorterUI

CHARS, PROPS, ENV, LIGHT = "GRP_01_CHARACTERS", "GRP_02_PROPS", "GRP_03_ENVIRONMENT", "GRP_05_LIGHTING"


@pytest.fixture(autouse=True)
def clean_slate(monkeypatch):
    cmds.reset_mock(side_effect=True, return_value=True)
    monkeypatch.setattr(cat, "_load_nexus_config", lambda: {})
    CategorizationEngine.reset_prefix_map_cache()
    yield
    CategorizationEngine.reset_prefix_map_cache()


@pytest.mark.parametrize("name, expected", [
    # straight off the test scene's manual review pile
    ("Archway1", ENV), ("Base_Pillar", ENV), ("Doorway", ENV), ("Pillar_2", ENV),
    ("Cauldron", PROPS), ("Stool", PROPS), ("Pointy_Shield", PROPS), ("Round_Shield", PROPS),
    ("Torn_Tapestry", ENV), ("BaseTapestry", ENV),
    # fused words and glued-on digits read like their real words now
    ("stylizedrock41", ENV), ("barrel01", PROPS),
    # "rig" alone doesn't mean character anymore
    ("light_rig", LIGHT),
    # a generic word breaks ties, it never causes one
    ("table_set", PROPS),
    # unchanged behavior
    ("Sword", PROPS), ("Round_Table", PROPS), ("oak_tree_03", ENV), ("CH_Knight", CHARS), ("UCX_wall", ENV),
])
def test_name_scoring(name, expected):
    assert CategorizationEngine.score_name_tokens(name)[0] == expected


@pytest.mark.parametrize("name", ["camera_rig", "tree_creature", "Top", "Bottom", "pCube1"])
def test_ambiguous_or_empty_names_go_to_review(name):
    """a name pointing two ways (or nowhere) is a question for a human,
    not a coin flip on whichever category was listed first"""
    assert CategorizationEngine.score_name_tokens(name) == (None, 0)


def _rigged_scene(path, joint_count=3):
    """skinned mesh at `path` with a real joint chain under it"""
    def list_relatives(*_a, **kw):
        if kw.get("type") == "joint":
            return [f"{path}|joint{i}" for i in range(joint_count)]
        return []
    cmds.listRelatives.side_effect = list_relatives
    cmds.nodeType.return_value = "transform"
    cmds.ls.side_effect = lambda node, long=False, **_kw: [path]


def test_rigged_tapestry_goes_to_environment_and_keeps_its_shield():
    _rigged_scene("|BaseTapestry|tapestry_geo")
    dest, confidence, shielded = CategorizationEngine.analyze_asset_dna("|BaseTapestry|tapestry_geo", "tapestry_geo")
    assert dest == ENV
    assert confidence == Confidence.MEDIUM  # joints vouch for "rigged", not for the word
    assert shielded is True


def test_rig_root_name_counts_even_when_the_mesh_is_named_nothing():
    _rigged_scene("|Torn_Tapestry|pCube12")
    assert CategorizationEngine.analyze_asset_dna("|Torn_Tapestry|pCube12", "pCube12")[0] == ENV


@pytest.mark.parametrize("path", [
    "|tree_creature|body_geo",           # any character word keeps it a character
    "|rig_v2|body_geo",                   # no category word at all, old behavior
    "|tapestry_on_crate|geo",             # two non-character categories, still ambiguous
    "|GRP_03_ENVIRONMENT|rig_root|geo",   # our own folder name doesn't count as evidence
])
def test_rigged_stays_character_unless_the_name_is_unambiguous(path):
    _rigged_scene(path)
    dest, confidence, shielded = CategorizationEngine.analyze_asset_dna(path, path.rsplit("|", 1)[-1])
    assert (dest, confidence, shielded) == (CHARS, Confidence.HIGH, True)


def test_explicit_prefix_on_a_rig_is_high_confidence(monkeypatch):
    _rigged_scene("|ENV_Flag_rig|geo")
    dest, confidence, shielded = CategorizationEngine.analyze_asset_dna("|ENV_Flag_rig|geo", "geo")
    assert (dest, confidence, shielded) == (ENV, Confidence.HIGH, True)


def test_config_words_extend_defaults_without_replacing_them(monkeypatch):
    monkeypatch.setattr(cat, "_load_nexus_config", lambda: {"category_keywords": {
        ENV: ["door"],
        "GRP_99_NOT_A_FOLDER": ["thing"],  # nothing creates this folder, has to be ignored
    }})
    CategorizationEngine.reset_prefix_map_cache()

    assert CategorizationEngine.score_name_tokens("LP_Door")[0] == ENV
    assert CategorizationEngine.score_name_tokens("Pillar_2")[0] == ENV  # defaults survive
    assert "GRP_99_NOT_A_FOLDER" not in CategorizationEngine.get_category_synonyms()


def test_bad_config_words_get_ignored_not_crashed_on(monkeypatch):
    monkeypatch.setattr(cat, "_load_nexus_config", lambda: {"category_keywords": {ENV: "door"}})
    CategorizationEngine.reset_prefix_map_cache()
    assert CategorizationEngine.score_name_tokens("LP_Door") == (None, 0)


def _scene_node(parent=None, stamp=None, stamp_source=None):
    attrs = {"nexus_asset_category": stamp, "nexus_category_source": stamp_source}
    cmds.listRelatives.side_effect = lambda *_a, **kw: [parent] if (kw.get("parent") and parent) else []
    cmds.attributeQuery.side_effect = lambda attr, node=None, exists=False: attrs.get(attr) is not None
    cmds.getAttr.side_effect = lambda plug: attrs[plug.split(".", 1)[1]]


@pytest.mark.parametrize("parent, stamp, stamp_source, expected", [
    ("|GRP_02_PROPS", "GRP_03_ENVIRONMENT", "auto", PROPS),  # hand-dragged to a different folder, folder wins
    ("|GRP_02_PROPS", None, None, PROPS),                     # hand-placed, never stamped
    ("|GRP_03_ENVIRONMENT", ENV, "user", ENV),                # manual override from last commit sticks
    ("|SomeGroup", ENV, "user", ENV),                         # even if it got moved somewhere unrelated
    ("|GRP_03_ENVIRONMENT", ENV, "auto", None),               # our own guess, re-detect so fixes apply
    ("|GRP_03_ENVIRONMENT", ENV, None, None),                 # stamped by an older version, same thing
    ("|UNASSIGNED_ASSETS", None, None, None),                 # parking spot, not a decision
    (None, None, None, None),                                 # fresh asset at root
])
def test_remembered_category(parent, stamp, stamp_source, expected):
    _scene_node(parent, stamp, stamp_source)
    assert bp.NexusBatchProcessor()._remembered_category("|Crate") == expected


@pytest.fixture
def one_asset_plan(monkeypatch):
    hm = bp.HierarchyManager
    monkeypatch.setattr(hm, "get_dag_path", staticmethod(lambda node: True))
    monkeypatch.setattr(hm, "get_safe_short_name", staticmethod(lambda node: node.strip("|").split("|")[-1]))
    monkeypatch.setattr(hm, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))
    monkeypatch.setattr(CategorizationEngine, "analyze_asset_dna",
                        classmethod(lambda cls, node, name: (ENV, Confidence.MEDIUM, False)))
    monkeypatch.setattr(CategorizationEngine, "check_frozen_transforms", staticmethod(lambda node: True))
    monkeypatch.setattr(bp.NexusBatchProcessor, "_remembered_category", lambda self, node: PROPS)
    cmds.listRelatives.side_effect = lambda *_a, **kw: ["|Crate"] if kw.get("parent") else []
    cmds.objExists.return_value = False
    cmds.ls.return_value = []


def test_remembered_category_lands_in_the_plan(one_asset_plan):
    item = bp.NexusBatchProcessor().analyze_sorting_plan(["|Crate|CrateShape"], "Auto-Detect")["plan"][0]
    assert (item["destination"], item["source"], item["confidence"]) == (
        PROPS, AssignmentSource.REMEMBERED, Confidence.HIGH)


def test_toolbar_override_beats_memory(one_asset_plan):
    item = bp.NexusBatchProcessor().analyze_sorting_plan(["|Crate|CrateShape"], "03_ENVIRONMENT")["plan"][0]
    assert (item["destination"], item["source"]) == (ENV, AssignmentSource.USER_FORCED)


@pytest.mark.parametrize("source, written", [
    (AssignmentSource.USER_FORCED, "user"),
    (AssignmentSource.REMEMBERED, "user"),
    (AssignmentSource.AUTO_DETECTED, "auto"),
])
def test_commit_stamps_who_made_the_call(monkeypatch, source, written):
    monkeypatch.setattr(bp.HierarchyManager, "is_node_locked_or_referenced", classmethod(lambda cls, node: False))
    cmds.progressWindow.side_effect = lambda *a, **k: False if k.get("isCancelled") else None
    cmds.objExists.return_value = False
    cmds.createNode.side_effect = lambda _type, name=None, **_kw: name
    cmds.listRelatives.return_value = []
    cmds.attributeQuery.return_value = False
    cmds.parent.return_value = ["Pr_crate"]
    cmds.ls.return_value = ["|GRP_02_PROPS|Pr_crate"]

    item = {"long_path": "|Crate", "original": "Pr_crate", "proposed": "Pr_crate", "destination": PROPS,
            "confidence": Confidence.HIGH, "source": source, "is_frozen": True, "is_shielded": False}
    bp.NexusBatchProcessor().execute_sorting_pass([item])

    stamped = {c.args[0]: c.args[1] for c in cmds.setAttr.call_args_list if c.args[0].startswith("|Crate.")}
    assert stamped["|Crate.nexus_category_source"] == written
    assert stamped["|Crate.nexus_asset_category"] == PROPS


def _status(data, freeze_checked=False):
    fake_ui = SimpleNamespace(freeze_checkbox=SimpleNamespace(isChecked=lambda: freeze_checked))
    return NexusSorterUI._status_text_for(fake_ui, data)


def test_rig_shield_label_only_when_actually_shielded():
    assert _status({"destination": CHARS, "is_shielded": False, "is_frozen": False}) == "Ready"
    assert "Rig Shield" in _status({"destination": ENV, "is_shielded": True, "is_frozen": False})
    assert _status({"destination": ENV, "is_shielded": False, "is_frozen": False}) == "Needs Freeze"
