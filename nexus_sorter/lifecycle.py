"""
detects stray category folders, root level groups that look like leftover
debris from an interrupted sort ("_CHARACTERS1", "_PROPS2"), not an
artist's own folder that happens to sit at root.

no maya import here on purpose so it's testable without Maya open. just
flags candidates, never deletes or moves anything itself. that's on the
caller, and only after a human checks a box in the review UI.
"""

from __future__ import annotations

import re
from typing import AbstractSet, Iterable

# strips our own GRP_01_ style numbering off a destination name
_GROUP_PREFIX_PATTERN = re.compile(r"^GRP_\d+_")


def _derive_bare_category_tokens(canonical_names: Iterable[str]) -> AbstractSet[str]:
    """GRP_01_CHARACTERS -> CHARACTERS. derived from canonical_names
    instead of hardcoded, so a different studio's config just works."""
    return {_GROUP_PREFIX_PATTERN.sub("", name).upper() for name in canonical_names}


def is_stray_category_folder(name: str, canonical_names: Iterable[str]) -> bool:
    """true if name looks like debris from an old sort, e.g.
    _CHARACTERS, _CHARACTERS1, _CHARACTERS2. anchored to the full
    string so "_CHARACTERS_OLD" or anything with extra text after the
    digits does NOT match, that's an artist's own name, not our pattern.
    just flags it, never touches the folder itself."""
    if not name.startswith("_"):
        return False

    remainder = name[1:]
    for token in _derive_bare_category_tokens(canonical_names):
        if not token:
            continue
        if remainder == token:
            return True
        suffix = remainder[len(token):] if remainder.startswith(token) else ""
        if suffix and suffix.isdigit():
            return True
    return False
