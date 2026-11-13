"""
FBX writes characters it can't store in a name as FBXASC + 3 digit ascii
code. stuff coming from Blender, Max, or outsourced files shows up as
MeshFBXASC046001 or FBXASC055_shirt. these pin the decode and, more
importantly, that it never hands maya an illegal name.
"""
import pytest

from nexus_sorter.categorization import CategorizationEngine as CE
from nexus_sorter.naming import NamingResolver

CHARS, PROPS, ENV = "GRP_01_CHARACTERS", "GRP_02_PROPS", "GRP_03_ENVIRONMENT"


@pytest.mark.parametrize("raw, readable", [
    ("LeftFBXASC032Arm", "Left_Arm"),             # space
    ("RockFBXASC046001", "Rock_001"),             # Blender's .001
    ("bodyFBXASC045low", "body_low"),             # dash
    ("rigFBXASC058root", "rig_root"),             # colon would read as a namespace, never let that through
    ("FBXASC055_shirt", "7_shirt"),
    ("plain_name", "plain_name"),                 # untouched when there's nothing to decode
    ("weirdFBXASC007bell", "weirdFBXASC007bell"),  # non-printable code stays encoded
])
def test_decode(raw, readable):
    assert CE._readable_fbx_name(raw) == readable


def test_decoded_name_gets_its_prefix():
    assert CE.derive_proposed_name("FBXASC055_shirt", CHARS) == "Ch_7_shirt"


def test_blender_duplicate_suffix_reads_as_a_variant():
    assert CE.derive_proposed_name("RockFBXASC046001", ENV).startswith("Env_rock_")


@pytest.mark.parametrize("destination", ["UNASSIGNED_ASSETS", "GRP_99_NO_PREFIX"])
def test_never_hands_maya_a_name_starting_with_a_digit(destination):
    """no prefix means a decoded 7_shirt would be illegal, so it has to
    fall back to the FBX spelling instead"""
    proposed = CE.derive_proposed_name("FBXASC055_shirt", destination)
    assert not proposed[0].isdigit()


def test_rig_root_decodes_when_legal_keeps_fbx_spelling_when_not():
    assert CE.sanitize_name_strict("BodyFBXASC032Rig") == "Body_Rig"
    assert CE.sanitize_name_strict("FBXASC055_shirt_0_0_0") == "FBXASC055_shirt_0_0_0"


def test_category_scoring_reads_the_decoded_words():
    assert CE.score_name_tokens("WoodenFBXASC032Crate")[0] == PROPS


def test_rerunning_on_a_decoded_name_is_stable():
    """the stacking-suffix bug's cousin: a second Analyze must not keep
    changing a name that's already been cleaned up"""
    first = NamingResolver.get_stable_name("LeftFBXASC032Crate", [], lambda n: CE.derive_proposed_name(n, PROPS))
    second = NamingResolver.get_stable_name(first, [], lambda n: CE.derive_proposed_name(n, PROPS))
    assert first == second == "Pr_left_crate"
