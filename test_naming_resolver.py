"""
coverage for the naming pipeline: NamingResolver (canonicalize,
resolve_unique_name, get_stable_name) and
CategorizationEngine.derive_proposed_name. this is the part of the tool
most worth protecting, it's pure string/regex logic with zero maya
dependency, and it's already had one real bug in production (the
"asset_01_01_01" suffix-stacking thing, see NamingResolver's own
docstring in nexus_sorter/naming.py).

most of this ports the old inline case table that used to only run via
`python nexus_auto_sorter.py`'s __main__ block (a real maya/mayapy
environment, not plain CI) into actual pytest. the NamingResolver tests
are new, that class had zero standalone coverage before this file, only
derive_proposed_name got exercised and only indirectly.
"""
import pytest

from nexus_sorter.naming import NamingResolver
from nexus_sorter.categorization import CategorizationEngine


# derive_proposed_name cases, ported from the old __main__ smoke test
DERIVE_PROPOSED_NAME_CASES = [
    ("stylized_rock_22_stylized_rock_22_stylized_rock_22",
     "GRP_03_ENVIRONMENT", "Env_stylized_rock_22"),
    ("stylized_stylizedrock41", "GRP_03_ENVIRONMENT", "Env_stylized_rock_41"),
    ("SM_BrickV01", "GRP_02_PROPS", "Pr_brick_01"),
    ("SM_MediumRockV02", "GRP_02_PROPS", "Pr_medium_rock_02"),
    ("SM_Cart", "GRP_02_PROPS", "Pr_cart"),
    ("rock_rock", "GRP_03_ENVIRONMENT", "Env_rock"),
    ("CH_Hero_Hero_V03", "GRP_01_CHARACTERS", "Ch_hero_03"),
]


@pytest.mark.parametrize("short_name, destination, expected", DERIVE_PROPOSED_NAME_CASES)
def test_derive_proposed_name(short_name, destination, expected):
    assert CategorizationEngine.derive_proposed_name(short_name, destination) == expected


def test_stylized_is_preserved_not_stripped():
    """'stylized' is a real visual-style differentiator, not junk, has
    to survive derive_proposed_name even though it looks like the kind
    of descriptor _JUNK_DESCRIPTORS strips. pinned as its own named
    test instead of folded into the parametrized table above since this
    was an explicit design decision worth failing loudly on its own"""
    result = CategorizationEngine.derive_proposed_name(
        "stylized_rock_22", "GRP_03_ENVIRONMENT"
    )
    assert "stylized" in result.lower()


# NamingResolver.canonicalize had zero standalone coverage before this
@pytest.mark.parametrize("raw, expected", [
    ("asset_01_01_01", "asset"),          # collapses ALL stacked suffixes in one pass
    ("asset_10", "asset"),                # single 2-digit suffix
    ("asset", "asset"),                   # nothing to strip - unchanged
    ("prop_2", "prop_2"),                 # single digit spared on purpose - not this tool's own suffix shape
    ("prop_02", "prop"),                  # zfill(2)-shaped suffix IS this tool's own - stripped
])
def test_canonicalize(raw, expected):
    assert NamingResolver.canonicalize(raw) == expected


def test_canonicalize_is_idempotent():
    """canonicalize(canonicalize(x)) == canonicalize(x), always - this
    is the exact guarantee NamingResolver's own class docstring makes;
    pinning it as a real test rather than trusting the comment."""
    x = "asset_01_01_01"
    once = NamingResolver.canonicalize(x)
    twice = NamingResolver.canonicalize(once)
    assert once == twice


def test_resolve_unique_name_no_collision():
    assert NamingResolver.resolve_unique_name("asset", {"asset_02"}) == "asset"


def test_resolve_unique_name_first_collision_gets_02_suffix():
    assert NamingResolver.resolve_unique_name("asset", {"asset"}) == "asset_02"


def test_resolve_unique_name_climbs_past_taken_suffixes():
    assert NamingResolver.resolve_unique_name("asset", {"asset", "asset_02"}) == "asset_03"


def test_resolve_unique_name_excludes_self():
    """an object is never a collision with itself, current_name has to
    be removed from the sibling set before anything else is checked.
    this is the historical bug's root cause in isolation, skip this
    exclusion and every re-run treats an asset as colliding with its
    own existing name"""
    assert NamingResolver.resolve_unique_name(
        "asset", {"asset"}, current_name="asset"
    ) == "asset"


# get_stable_name: the full pipeline, plus the historical production bug as a regression test
def test_get_stable_name_does_not_stack_suffixes_on_rerun():
    """regression test for the original "asset_01_01_01" bug.

    an asset is already correctly named "asset_02" because a real
    sibling elsewhere is named "asset". on a second analyze pass, the
    naive sibling set handed to resolve_unique_name still contains the
    asset's own current name alongside the real sibling, that's just
    how a scene's existing children get listed, self included.

    before the fix, current_name was never excluded, so "asset_02"
    collided with itself too and the suffix climbed to "asset_03",
    then "asset_04" the run after that, forever. after the fix it
    should hold at "asset_02" no matter how many times this runs."""
    result = NamingResolver.get_stable_name(
        raw_current_name="asset_02",
        sibling_names={"asset", "asset_02"},
        name_deriver=lambda canonical: "asset",
    )
    assert result == "asset_02"


def test_get_stable_name_strips_stray_suffix_before_rederiving():
    """a stray dedup suffix from a previous run has to be invisible to
    name_deriver, never re-tokenized and baked back in as if it were
    meaningful asset name content (the second half of the original bug)"""
    seen_by_deriver = []

    def spy_deriver(canonical):
        seen_by_deriver.append(canonical)
        return canonical

    NamingResolver.get_stable_name(
        raw_current_name="rock_02",
        sibling_names=set(),
        name_deriver=spy_deriver,
    )
    assert seen_by_deriver == ["rock"]
