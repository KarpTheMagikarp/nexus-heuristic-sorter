"""
coverage for nexus_sorter/lifecycle.py - already maya/qt free by design
(see its own module docstring), so unlike test_naming_resolver.py this
doesn't need any stubbing at all, conftest.py's stubs are just harmless
no-ops here since this module never imports maya or PySide6 to begin with.

cases below ported straight from the old inline smoke test this module
used to run via its own __main__ block, same names, same expected
results, just runs as part of the normal suite now instead of needing
`python nexus_folder_lifecycle.py` run by hand.
"""
import pytest

from nexus_sorter.lifecycle import is_stray_category_folder, _derive_bare_category_tokens


CANONICAL_DESTINATIONS = {
    "GRP_01_CHARACTERS", "GRP_02_PROPS", "GRP_03_ENVIRONMENT",
    "GRP_04_VFX", "GRP_05_LIGHTING", "UNASSIGNED_ASSETS",
}


def test_derive_bare_category_tokens_strips_this_tools_own_prefix():
    tokens = _derive_bare_category_tokens(CANONICAL_DESTINATIONS)
    assert tokens == {
        "CHARACTERS", "PROPS", "ENVIRONMENT", "VFX",
        "LIGHTING", "UNASSIGNED_ASSETS",
    }


STRAY_FOLDER_CASES = [
    # (name, expected) - drawn directly from the reported outliner clutter
    ("_CHARACTERS", True),
    ("_CHARACTERS1", True),
    ("_CHARACTERS2", True),
    ("_CHARACTERS3", True),
    ("_PROPS", True),
    ("_PROPS1", True),
    ("_ENVIRONMENT", True),
    ("_ENVIRONMENT1", True),
    ("_VFX", True),
    ("_VFX3", True),
    ("NEXUS_REVIEW_PAD", False),              # artist staging pad - must NOT match
    ("armature", False),                      # FBX import root - unrelated
    ("rootFBXASC032ground", False),           # FBX import root - unrelated
    ("mesh", False),                          # already-sorted asset, not a root folder pattern
    ("GRP_01_CHARACTERS", False),             # this IS the active canonical folder - not stray
    ("GRP_01_CHARACTERS2", False),            # has the GRP_ prefix - different (older) pattern
    ("_CHARACTERS_OLD_DO_NOT_USE", False),    # artist-named - must NOT match
    ("_CHARACTERS_HOLD_FOR_REVIEW", False),   # artist-named - must NOT match
    ("Lighting_WIP", False),                  # artist WIP folder - must NOT match
    ("_LIGHTING_WIP", False),                 # same trap, underscore-prefixed - must NOT match
    ("_UNASSIGNED_ASSETS1", True),            # bare-token destination (no GRP_ to strip) still works
]


@pytest.mark.parametrize("name, expected", STRAY_FOLDER_CASES)
def test_is_stray_category_folder(name, expected):
    assert is_stray_category_folder(name, CANONICAL_DESTINATIONS) is expected


def test_stray_folder_detection_is_config_derived_not_hardcoded():
    """point this at a different studio's config and it should recognize
    a different set of stray folders automatically, zero code changes,
    no literal studio category name should be baked into this module"""
    other_studio_destinations = {"GRP_01_VEHICLES", "GRP_02_WEAPONS"}
    assert is_stray_category_folder("_VEHICLES1", other_studio_destinations) is True
    assert is_stray_category_folder("_CHARACTERS1", other_studio_destinations) is False
