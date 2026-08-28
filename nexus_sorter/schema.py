from __future__ import annotations

from typing import Optional, TypedDict

from .categorization import Confidence, AssignmentSource


class _PlanItemRequired(TypedDict):
    """the dict analyze_sorting_plan builds per-asset and everything else
    (main_window, execute_sorting_pass) reads back by these exact keys.
    still a plain dict at runtime, nothing about existing data["x"] access
    has to change, this just gives a type checker something to catch a
    typo'd key against instead of finding out at runtime."""
    original: str
    proposed: str
    destination: str
    confidence: Confidence
    source: AssignmentSource
    long_path: str
    is_frozen: bool
    is_referenced: bool
    is_shielded: bool
    skip_reason: Optional[str]


class PlanItem(_PlanItemRequired, total=False):
    # only present once main_window seeds it under Selected Assets scope,
    # so it's optional rather than folded into the required set above
    _included: bool


class StrayFolderCandidate(TypedDict):
    """one entry from scan_stray_category_folders. name is the bare root
    node name, long_path is the full DAG path, child_count is just for
    the review UI's "empty" vs "N objects inside" label."""
    name: str
    long_path: str
    child_count: int


class PurgeSkippedTooRecent(TypedDict):
    path: str
    quarantined_date: Optional[str]


class PurgeSkippedUnsafe(TypedDict):
    path: str
    reason: str


class PurgeEligible(TypedDict):
    path: str
    quarantined_date: Optional[str]
    descendant_count: int


class PurgeManifest(TypedDict):
    """what build_purge_manifest returns and what write_purge_manifest_to_disk
    dumps straight to json, so keep this in sync with both."""
    generated: str
    older_than_days: Optional[int]
    eligible: list[PurgeEligible]
    skipped_too_recent: list[PurgeSkippedTooRecent]
    skipped_unsafe: list[PurgeSkippedUnsafe]
