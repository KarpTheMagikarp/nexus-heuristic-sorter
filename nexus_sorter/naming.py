from __future__ import annotations

import re
from typing import Callable, Iterable


class NamingResolver:
    """handles the naming/dedup logic. canonicalize() strips old suffixes
    back off, resolve_unique_name() adds exactly one back if there's an
    actual collision. run get_stable_name() as many times as you want on
    the same asset and it should never change once it's stable."""

    # matches trailing _NN suffixes (2+ digits) so "asset_01_01_01" collapses
    # to "asset" in one pass. 2+ digits spares stuff like "prop_2"
    _DEDUP_SUFFIX_RE = re.compile(r'(?:_\d{2,})+$')

    @classmethod
    def canonicalize(cls, name: str) -> str:
        """canonicalize(canonicalize(x)) == canonicalize(x), always."""
        return cls._DEDUP_SUFFIX_RE.sub('', name)

    @staticmethod
    def resolve_unique_name(
        base_name: str,
        sibling_names: Iterable[str],
        current_name: str | None = None,
    ) -> str:
        """makes sure base_name doesn't collide with anything in
        sibling_names. current_name gets excluded first, since an object
        can't collide with itself - skipping that check is literally how
        the old bug happened, an asset kept comparing itself to its own
        name and the suffix climbed by one every single re-run."""
        siblings = set(sibling_names)
        if current_name is not None:
            siblings.discard(current_name)

        if base_name not in siblings:
            return base_name

        n = 2
        while True:
            candidate = f"{base_name}_{n:02d}"
            if candidate not in siblings:
                return candidate
            n += 1

    @classmethod
    def get_stable_name(
        cls,
        raw_current_name: str,
        sibling_names: Iterable[str],
        name_deriver: Callable[[str], str],
    ) -> str:
        """full pipeline: canonicalize the current name first (so an
        old suffix never gets mistaken for real content), hand it to
        name_deriver, then resolve uniqueness against everything else.
        order matters, if you skip step 1 the old stacking bug is back."""
        canonical = cls.canonicalize(raw_current_name)
        proposed = name_deriver(canonical)
        return cls.resolve_unique_name(proposed, sibling_names, current_name=raw_current_name)
