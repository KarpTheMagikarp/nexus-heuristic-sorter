from __future__ import annotations

import os
import json
import datetime
from typing import Any, Callable

import maya.cmds as cmds

from .config import logger, _load_nexus_config
from .naming import NamingResolver
from .hierarchy import HierarchyManager, MayaUndoChunk
from .categorization import CategorizationEngine, Confidence, AssignmentSource
from .lifecycle import is_stray_category_folder
from .schema import PlanItem, StrayFolderCandidate, PurgeManifest


class NexusBatchProcessor:
    # shared with main_window.py's Override toolbar dropdown, so a bulk
    # reassign from there and a fresh Analyze's force_category both map
    # the same combo text to the same real folder name - one dict, not
    # two that can quietly drift apart
    FORCE_CATEGORY_MAP = {
        "01_CHARACTERS": "GRP_01_CHARACTERS",
        "02_PROPS": "GRP_02_PROPS",
        "03_ENVIRONMENT": "GRP_03_ENVIRONMENT",
        "04_VFX": "GRP_04_VFX",
        "05_LIGHTING": "GRP_05_LIGHTING",
    }

    def __init__(self) -> None:
        self.studio_hierarchy: dict[str, list] = {
            "GRP_01_CHARACTERS": [],
            "GRP_02_PROPS": [],
            "GRP_03_ENVIRONMENT": [],
            "GRP_04_VFX": [],
            "GRP_05_LIGHTING": [],
            "UNASSIGNED_ASSETS": [],
        }

    @staticmethod
    def _existing_children_names(destination: str) -> set[str]:
        """short names of whatever's already sitting in this destination folder"""
        if not cmds.objExists(f"|{destination}"):
            return set()
        kids = cmds.listRelatives(f"|{destination}", children=True, fullPath=True) or []
        return {HierarchyManager.get_safe_short_name(k) for k in kids}

    @classmethod
    def _claim_unique_name(
        cls,
        destination: str,
        raw_short_name: str,
        name_deriver: Callable[[str], str],
        claimed_names: dict[str, set[str]],
    ) -> str:
        """derives the clean name and makes sure it's unique, in one
        pass (see NamingResolver). checked against both what's already in
        the scene and whatever this same batch already claimed, so two
        new assets in the same run can't collide with each other either.

        raw_short_name needs to be the CURRENT name, not something
        already cleaned up - NamingResolver needs the raw name so it can
        strip old suffixes and exclude the asset's own name from its own
        collision check. skip either step and the stacking bug is back.

        name_deriver is str -> str, pass derive_proposed_name for a
        normal asset or sanitize_name_strict for a rig-shielded root
        (illegal characters only, never real token surgery)."""
        if destination not in claimed_names:
            claimed_names[destination] = cls._existing_children_names(destination)

        names_in_use = claimed_names[destination]
        final_name = NamingResolver.get_stable_name(
            raw_short_name, names_in_use, name_deriver=name_deriver,
        )

        names_in_use.discard(raw_short_name)  # it's about to be renamed/reparented away from this state
        names_in_use.add(final_name)
        return final_name

    def analyze_sorting_plan(self, target_shapes: list[str], force_category: str) -> dict[str, Any]:
        plan_data = {}
        seen_transforms = set()
        claimed_names = {}

        for shape_path in target_shapes:
            root_parent = HierarchyManager.get_dag_path(shape_path)
            if not root_parent: continue

            transform_node = cmds.listRelatives(shape_path, parent=True, fullPath=True)
            if transform_node:
                transform_node = transform_node[0]
            else:
                transform_node = shape_path

            # a transform can carry more than one shape (render + intermediate,
            # multiple mesh nodes, etc) - only need to run the pass stack on it once
            if transform_node in seen_transforms: continue
            seen_transforms.add(transform_node)

            short_name = HierarchyManager.get_safe_short_name(transform_node)
            is_referenced = HierarchyManager.is_node_locked_or_referenced(transform_node)

            needs_shield = False
            if is_referenced:
                primary_dest, confidence = "UNASSIGNED_ASSETS", Confidence.LOW
                source = AssignmentSource.SKIPPED_REFERENCED
                skip_reason = "Skipped: Referenced Asset"
            else:
                # pass 1 always runs even under a forced override, rig shield
                # is a safety check not a category choice, shouldn't get skipped
                auto_dest, auto_confidence, needs_shield = CategorizationEngine.analyze_asset_dna(transform_node, short_name)

                if force_category and "Auto-Detect" not in force_category:
                    primary_dest = self.FORCE_CATEGORY_MAP.get(force_category, "UNASSIGNED_ASSETS")
                    confidence = Confidence.HIGH
                    source = AssignmentSource.USER_FORCED
                else:
                    primary_dest, confidence = auto_dest, auto_confidence
                    source = AssignmentSource.AUTO_DETECTED
                skip_reason = None

            # rig shield grabs the whole asset from its top group instead of
            # renaming the joint/mesh that tripped pass 1
            is_shielded = needs_shield and not is_referenced
            move_target = (
                HierarchyManager.get_root_ancestor(transform_node, self.studio_hierarchy.keys())
                if is_shielded else transform_node
            )

            # if the root-ancestor walk couldn't get past a reserved folder at
            # all, this "asset" IS one of our own sort folders - nothing to do
            if move_target.strip('|') in self.studio_hierarchy:
                continue

            if move_target in plan_data: continue

            move_short_name = HierarchyManager.get_safe_short_name(move_target)

            if is_shielded:
                # rig's master group name, illegal chars only, no token surgery
                name_deriver = CategorizationEngine.sanitize_name_strict
            else:
                name_deriver = lambda n, _dest=primary_dest: CategorizationEngine.derive_proposed_name(n, _dest)

            # NamingResolver strips old suffixes, re-derives, only re-suffixes
            # on a real collision. this is the actual fix for the stacking bug
            clean_name = self._claim_unique_name(primary_dest, move_short_name, name_deriver, claimed_names)

            is_frozen = CategorizationEngine.check_frozen_transforms(move_target)

            plan_data[move_target] = {
                "original": move_short_name,
                "proposed": clean_name,
                "destination": primary_dest,
                "confidence": confidence,
                "source": source,
                "long_path": move_target,
                "is_frozen": is_frozen,
                "is_referenced": is_referenced,
                "is_shielded": is_shielded,
                "skip_reason": skip_reason,
            }

        return {
            "status": "success",
            "plan": list(plan_data.values()),
            # detection only, nothing touched unless the artist checks it before commit
            "stray_folders": self.scan_stray_category_folders(),
        }

    # holding group for anything quarantined instead of deleted. uses the
    # GRP_ prefix convention same as every active category folder
    DEPRECATED_HOLDING_GROUP = "GRP_DEPRECATED_FOLDERS"

    # the one place these attribute names are defined, everything else
    # references these constants instead of retyping the string
    ATTR_QUARANTINE_ROOT = "nexus_quarantine_root"
    ATTR_QUARANTINED_DATE = "nexus_quarantined_date"
    ATTR_ASSET_CATEGORY = "nexus_asset_category"
    # what _quarantine_safety_check checks a folder's attributes
    # against, see that method for why this exists at all
    _OWN_ATTRS = frozenset({ATTR_QUARANTINE_ROOT, ATTR_QUARANTINED_DATE, ATTR_ASSET_CATEGORY})

    # fallback only, used when nexus_config.json has no
    # purge_age_threshold_days key or the value there isn't usable.
    # never used in place of a config that exists and is valid
    DEFAULT_PURGE_AGE_THRESHOLD_DAYS = 14

    @classmethod
    def get_purge_age_threshold_days(cls) -> int | float:
        """studio-configurable via nexus_config.json's
        purge_age_threshold_days key, same config file and cached loader
        as CategorizationEngine.get_prefix_map(), just a different key
        out of it. falls back to DEFAULT_PURGE_AGE_THRESHOLD_DAYS if
        that key's missing or not a usable non-negative number - a
        malformed entry silently becoming "no age floor, everything's
        eligible" is exactly the wrong direction to fail in for
        something destructive, so a bad value gets logged and rejected
        instead of quietly accepted."""
        value = _load_nexus_config().get("purge_age_threshold_days")
        if value is None:
            return cls.DEFAULT_PURGE_AGE_THRESHOLD_DAYS
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return value
        logger.warning(
            f"nexus_config.json's purge_age_threshold_days ({value!r}) isn't a "
            f"usable non-negative number - falling back to "
            f"{cls.DEFAULT_PURGE_AGE_THRESHOLD_DAYS} days."
        )
        return cls.DEFAULT_PURGE_AGE_THRESHOLD_DAYS

    @classmethod
    def _ensure_deprecated_holding_group(cls) -> str:
        """idempotent, run it once or a hundred times and the end state
        is the same, one holding group, never a duplicate. the stamp below
        only runs inside the creation branch, no reason to redo it every commit"""
        group_path = f"|{cls.DEPRECATED_HOLDING_GROUP}"
        if not cmds.objExists(group_path):
            created = cls._create_root_group(cls.DEPRECATED_HOLDING_GROUP)
            cls._stamp_quarantine_root(created)
        return group_path

    @staticmethod
    def _create_root_group(name: str) -> str:
        """createNode quietly hands back name1 if something already owns
        the name (a set or display layer counts). every move after this
        targets |name by string, so a silent rename means every parent()
        aims at a folder that doesn't exist. bail loudly instead."""
        created = cmds.createNode('transform', name=name)
        if created != name:
            cmds.delete(created)
            raise RuntimeError(
                f"Couldn't create '{name}' at the scene root, Maya renamed it to "
                f"'{created}'. Something else in the scene is already named '{name}', "
                f"rename it and try again."
            )
        return f"|{name}"

    @classmethod
    def _stamp_quarantine_root(cls, group_path: str) -> None:
        """marks GRP_DEPRECATED_FOLDERS as non-production, 3 ways,
        run once when the group first gets created:

        1. a locked custom bool attribute - the real signal any exporter
           or validator should check, not the name and not a color.
           locked so it can't get cleared by accident in the channel box.
        2. a display override (Reference, NOT Template - Template makes
           it unselectable which would break dragging stuff back out of
           quarantine). only cosmetic, attr #1 is the actual source of
           truth.
        3. cmds.lockNode on the group itself so it can't get deleted or
           renamed from the outliner by accident. only the root gets
           locked, never its kids, since people still need to grab stuff
           back out of quarantine whenever they want."""
        if not cmds.attributeQuery(cls.ATTR_QUARANTINE_ROOT, node=group_path, exists=True):
            cmds.addAttr(group_path, longName=cls.ATTR_QUARANTINE_ROOT, attributeType='bool')
        cmds.setAttr(f"{group_path}.{cls.ATTR_QUARANTINE_ROOT}", lock=False)
        cmds.setAttr(f"{group_path}.{cls.ATTR_QUARANTINE_ROOT}", True)
        cmds.setAttr(f"{group_path}.{cls.ATTR_QUARANTINE_ROOT}", lock=True)

        try:
            cmds.setAttr(f"{group_path}.overrideEnabled", True)
            cmds.setAttr(f"{group_path}.overrideDisplayType", 2)  # 2 = Reference
        except RuntimeError as err:
            # cosmetic only - never worth failing group creation over
            logger.warning(f"Could not set display override on {group_path}: {err}")

        cmds.lockNode(group_path, lock=True)

    @classmethod
    def is_quarantine_root(cls, node: str) -> bool:
        """checks the locked attribute instead of the node's name, so
        other scripts don't need to hardcode DEPRECATED_HOLDING_GROUP or
        worry about an artist naming their own folder the same thing."""
        try:
            return bool(
                cmds.objExists(node)
                and cmds.attributeQuery(cls.ATTR_QUARANTINE_ROOT, node=node, exists=True)
                and cmds.getAttr(f"{node}.{cls.ATTR_QUARANTINE_ROOT}")
            )
        except RuntimeError:
            return False

    @classmethod
    def _stamp_quarantine_date(cls, node_path: str) -> None:
        """stamps when a folder got quarantined so a later purge pass
        can filter by age. not locked like the root flag since it's just
        a data record, no reason to make it hard to fix by hand."""
        if not cmds.attributeQuery(cls.ATTR_QUARANTINED_DATE, node=node_path, exists=True):
            cmds.addAttr(node_path, longName=cls.ATTR_QUARANTINED_DATE, dataType='string')
        cmds.setAttr(f"{node_path}.{cls.ATTR_QUARANTINED_DATE}",
                     datetime.date.today().isoformat(), type='string')

    @classmethod
    def _quarantine_safety_check(cls, folder_path: str) -> tuple[bool, list[str]]:
        """same safety check both quarantine paths run through, so
        they can't drift apart over time. returns (is_safe, reasons).

        referenced/locked is a hard no, maya won't let us parent it
        anyway. custom attributes or live connections mean someone did
        something here on purpose, so a human should look at it instead
        of it getting auto-decided. this doesn't delete/move/keep
        anything itself, just answers "is this safe to touch right now."

        excludes this tool's OWN attributes (_OWN_ATTRS) from the custom
        attr check, otherwise a folder that got quarantined once and
        dragged back out would forever get flagged by its own leftover
        nexus_quarantined_date attribute. people change their mind about
        folders all the time, this check shouldn't punish them for it."""
        if HierarchyManager.is_node_locked_or_referenced(folder_path):
            return False, ["referenced or locked"]

        reasons = []
        all_attrs = cmds.listAttr(folder_path, userDefined=True) or []
        custom_attrs = [a for a in all_attrs if a not in cls._OWN_ATTRS]
        if custom_attrs:
            reasons.append(f"custom attributes ({', '.join(custom_attrs)})")
        if HierarchyManager.has_external_connections(folder_path):
            reasons.append("external connections (expression/constraint/script hook)")

        return (not reasons), reasons

    def scan_stray_category_folders(self) -> list[StrayFolderCandidate]:
        """scene-wide, read-only scan for root-level folders that look
        like stray sort debris (see is_stray_category_folder in
        lifecycle.py). detection only, nothing here moves, deletes, or
        hides anything. returns candidate dicts for the review UI,
        execute_sorting_pass only quarantines one of these if the artist
        actually checked it first."""
        canonical_names = set(self.studio_hierarchy.keys())
        candidates = []
        for root_node in (cmds.ls(assemblies=True, long=True) or []):
            plain_name = root_node.strip('|')
            if not is_stray_category_folder(plain_name, canonical_names):
                continue
            child_count = len(cmds.listRelatives(root_node, children=True) or [])
            candidates.append({
                "name": plain_name,
                "long_path": root_node,
                "child_count": child_count,
            })
        return candidates

    # purge is the only place allowed to call cmds.delete() on real
    # content. never reachable from execute_commit, its own separate menu
    # action. split into two functions same as analyze/execute above,
    # build_purge_manifest only looks, purge_deprecated_folders only acts
    # on an explicit list a human already reviewed

    @staticmethod
    def _is_old_enough(date_str: str | None, days: int | float) -> bool:
        """missing/broken date = not old enough, never the other way.
        don't want to lose something because a timestamp failed to write."""
        if not date_str:
            return False
        try:
            stamped = datetime.date.fromisoformat(date_str)
        except ValueError:
            return False
        return (datetime.date.today() - stamped).days >= days

    def build_purge_manifest(self, older_than_days: int | float | None = None) -> PurgeManifest:
        """read-only, sorts quarantined folders into eligible / too-recent
        / unsafe for the purge dialog to preview before anything deletes.
        older_than_days=None means everything's eligible regardless of age."""
        manifest = {
            "generated": datetime.datetime.now().isoformat(),
            "older_than_days": older_than_days,
            "eligible": [],
            "skipped_too_recent": [],
            "skipped_unsafe": [],
        }

        root_path = f"|{self.DEPRECATED_HOLDING_GROUP}"
        # confirms whatever's at that path is actually OUR stamped root,
        # not just something else happening to share the name
        if not cmds.objExists(root_path) or not self.is_quarantine_root(root_path):
            return manifest

        for child_path in (cmds.listRelatives(root_path, children=True, fullPath=True) or []):
            quarantined_date = None
            if cmds.attributeQuery(self.ATTR_QUARANTINED_DATE, node=child_path, exists=True):
                quarantined_date = cmds.getAttr(f"{child_path}.{self.ATTR_QUARANTINED_DATE}")

            if older_than_days is not None and not self._is_old_enough(quarantined_date, older_than_days):
                manifest["skipped_too_recent"].append({
                    "path": child_path, "quarantined_date": quarantined_date,
                })
                continue

            if HierarchyManager.is_node_locked_or_referenced(child_path):
                manifest["skipped_unsafe"].append({
                    "path": child_path, "reason": "referenced or locked",
                })
                continue

            manifest["eligible"].append({
                "path": child_path,
                "quarantined_date": quarantined_date,
                "descendant_count": len(cmds.listRelatives(child_path, allDescendents=True) or []),
            })

        return manifest

    def purge_deprecated_folders(self, folder_paths: list[str]) -> dict[str, Any]:
        """DESTRUCTIVE. takes an explicit list of exact paths to delete,
        doesn't discover or decide anything fresh (that's
        build_purge_manifest's job above this). re-checks referenced/
        locked status on each path right before deleting, since a
        manifest from a minute ago isn't a promise the scene hasn't
        changed since.

        clears selection first just for general safety, doesn't change
        WHAT gets deleted since that's already locked to the explicit
        path list, just avoids deleting something mid-selected which is
        a rougher edge case in maya.

        unlocks the quarantine root once for the whole batch and relocks
        it in a finally, so a crash mid-batch can't leave it unlocked.

        return dict can legitimately differ from the manifest shown to
        the artist if the scene changed in between."""
        cmds.select(clear=True)

        result = {"purged": [], "skipped_unsafe": [], "skipped_missing": []}
        root_path = f"|{self.DEPRECATED_HOLDING_GROUP}"
        root_exists = cmds.objExists(root_path)
        if root_exists:
            cmds.lockNode(root_path, lock=False)

        try:
            for path in folder_paths:
                if not cmds.objExists(path):
                    result["skipped_missing"].append(path)
                    continue
                if HierarchyManager.is_node_locked_or_referenced(path):
                    result["skipped_unsafe"].append(path)
                    continue
                try:
                    cmds.delete(path)
                    result["purged"].append(path)
                except RuntimeError as err:
                    logger.warning(f"Failed to purge {path}: {err}")
                    result["skipped_unsafe"].append(path)
        finally:
            if root_exists:
                cmds.lockNode(root_path, lock=True)

        return result

    @staticmethod
    def write_purge_manifest_to_disk(manifest: PurgeManifest) -> str | None:
        """writes the manifest to a timestamped json file, the one thing
        that survives after maya's undo queue is gone. caller writes this
        BEFORE purge_deprecated_folders runs, off the exact manifest the
        artist reviewed, so there's a record of what was intended even if
        something goes wrong mid-purge"""
        log_dir = os.path.join(cmds.internalVar(userScriptDir=True), "nexus_purge_logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            filename = f"purge_manifest_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            filepath = os.path.join(log_dir, filename)
            with open(filepath, 'w') as f:
                json.dump(manifest, f, indent=2)
            logger.info(f"Purge manifest written to {filepath}")
            return filepath
        except OSError as err:
            logger.warning(f"Could not write purge manifest to disk: {err}")
            return None

    def execute_sorting_pass(
        self,
        plan_data: list[PlanItem],
        freeze_eligible: bool = False,
        quarantine_folder_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {"status": "pending", "count": 0, "objects": [], "message": "", "cancelled": False,
                   "frozen": 0, "freeze_failed": 0, "move_failed": []}
        quarantine_folder_paths = quarantine_folder_paths or []
        total_items = len(plan_data)
        if total_items == 0 and not quarantine_folder_paths:
            # both current callers already guard against this before calling
            # in, so this is currently dead in practice - but "pending" isn't
            # a real final status anywhere else, don't leave it hanging on
            # a caller-side guarantee that isn't actually enforced in here
            payload["status"] = "success"
            return payload

        # maya only allows one progressWindow globally, and it can fail
        # to open even on a fresh session for reasons I couldn't track down.
        # so it's optional now, not load bearing, sort still runs without it
        try:
            cmds.progressWindow(endProgress=True)
        except RuntimeError:
            pass

        progress_window_active = True
        try:
            cmds.progressWindow(title='Nexus Batch Processor', progress=0, status='Initializing...', isInterruptable=True, maxValue=total_items)
        except RuntimeError as err:
            logger.warning(f"Progress window unavailable ({err}) - continuing without one.")
            progress_window_active = False

        # constructed on its own line, guarded, so a failure right here
        # (missing import, or anything else) still closes the progress
        # window below instead of leaving it stuck open forever, exactly
        # what happened before this got added
        try:
            undo_chunk = MayaUndoChunk()
        except Exception:
            if progress_window_active:
                try:
                    cmds.progressWindow(endProgress=True)
                except RuntimeError:
                    pass
            raise

        with undo_chunk:
            try:
                pre_existing_groups = {g for g in self.studio_hierarchy if cmds.objExists(f"|{g}")}
                groups_created_this_run = set()

                # snapshot before anything moves, so cleanup later can tell
                # "we drained this" from "this was already empty when we got here"
                pre_run_occupied = {
                    g for g in pre_existing_groups
                    if cmds.listRelatives(f"|{g}", children=True)
                }

                try:
                    for master_group in self.studio_hierarchy:
                        if master_group not in pre_existing_groups:
                            self._create_root_group(master_group)
                            groups_created_this_run.add(master_group)
                except RuntimeError:
                    # nothing's moved yet, so tidy up the empty ones we
                    # made and let the outer handler report it
                    for group in groups_created_this_run:
                        cmds.delete(f"|{group}")
                    raise

                moved_objects = []
                for i, item in enumerate(plan_data):
                    if progress_window_active and cmds.progressWindow(query=True, isCancelled=True):
                        logger.warning("User interrupted the sorting pass.")
                        payload["cancelled"] = True
                        break

                    current_long_path = item["long_path"]
                    short_name = item["original"]
                    clean_name = item["proposed"]
                    target_folder = f"|{item['destination']}"

                    if progress_window_active:
                        cmds.progressWindow(edit=True, progress=i, status=f'Processing: {short_name}')

                    if HierarchyManager.is_node_locked_or_referenced(current_long_path):
                        logger.warning(f"Skipping referenced/locked asset: {current_long_path}")
                        continue

                    # freeze is opt-in, skips shielded stuff and the whole
                    # characters/vfx category since baking those breaks rigs
                    if (freeze_eligible and not item.get("is_frozen") and not item.get("is_shielded")
                            and not CategorizationEngine.is_freeze_blocked(item["destination"])):
                        if CategorizationEngine.try_freeze_transform(current_long_path):
                            payload["frozen"] += 1
                        else:
                            payload["freeze_failed"] += 1

                    if short_name != clean_name:
                        renamed_path = HierarchyManager.execute_safe_rename(current_long_path, clean_name)
                        if renamed_path: current_long_path = renamed_path

                    try:
                        if not cmds.attributeQuery(self.ATTR_ASSET_CATEGORY, node=current_long_path, exists=True):
                            cmds.addAttr(current_long_path, longName=self.ATTR_ASSET_CATEGORY, dataType='string')
                        cmds.setAttr(f"{current_long_path}.{self.ATTR_ASSET_CATEGORY}", item['destination'], type='string')
                    except RuntimeError as e:
                        logger.warning(f"Could not add metadata to {current_long_path}: {e}")

                    current_parent = cmds.listRelatives(current_long_path, parent=True, fullPath=True)
                    current_parent = current_parent[0] if current_parent else ""
                    if current_parent != target_folder:
                        try:
                            parented = cmds.parent(current_long_path, target_folder)
                            if parented: current_long_path = cmds.ls(parented[0], long=True)[0]
                        except RuntimeError as e:
                            logger.warning(f"Failed to parent {current_long_path}: {e}")
                            # used to fall through and count as sorted, so the
                            # summary claimed moves that never happened
                            payload["move_failed"].append(current_long_path)
                            continue

                    moved_objects.append(current_long_path)

                # 3 cleanup cases:
                # 1. we created it this run and it's still empty, just delete,
                #    no data loss possible
                # 2. existed before, got drained by our own moves (we can prove
                #    it via pre_run_occupied), quarantine it, don't delete a
                #    canonical folder outright, but no human gate needed either
                #    since quarantine is fully reversible
                # 3. never touched by this run at all, could be old debris or
                #    could be an artist's real folder that just matches the
                #    naming pattern, zero provenance so this needs a human
                #    (quarantine_folder_paths is exactly what got checked in
                #    the review UI, nothing auto-quarantined here like case 2)
                for group in groups_created_this_run:
                    group_path = f"|{group}"
                    if cmds.objExists(group_path) and not (cmds.listRelatives(group_path, children=True) or []):
                        cmds.delete(group_path)

                payload["drained_folders_quarantined"] = []
                payload["drained_folders_flagged"] = []
                deprecated_group_path = None  # lazy, no point creating an empty holding group

                for group in pre_run_occupied:
                    group_path = f"|{group}"
                    if not cmds.objExists(group_path):
                        continue
                    if cmds.listRelatives(group_path, children=True):
                        continue  # still has content - not our business

                    is_safe, reasons = self._quarantine_safety_check(group_path)
                    if not is_safe:
                        logger.warning(
                            f"{group_path} was emptied by this pass but {', '.join(reasons)} "
                            f"- left in place for manual review, not auto-quarantined."
                        )
                        payload["drained_folders_flagged"].append(group_path)
                        continue

                    if deprecated_group_path is None:
                        deprecated_group_path = self._ensure_deprecated_holding_group()

                    # unlock the root just long enough to accept a new child,
                    # relocked in finally so a failed parent() can't leave it open
                    cmds.lockNode(deprecated_group_path, lock=False)
                    try:
                        parented = cmds.parent(group_path, deprecated_group_path)
                        new_path = cmds.ls(parented[0], long=True)[0] if parented else group_path
                    except RuntimeError as err:
                        # one bad folder shouldn't fail the whole commit
                        logger.warning(f"Failed to quarantine {group_path}: {err}")
                        payload["drained_folders_flagged"].append(group_path)
                        continue
                    finally:
                        cmds.lockNode(deprecated_group_path, lock=True)

                    self._stamp_quarantine_date(new_path)
                    logger.info(
                        f"{group_path} was emptied by this pass - moved to "
                        f"{deprecated_group_path} (not deleted, fully recoverable)."
                    )
                    payload["drained_folders_quarantined"].append(new_path)

                payload["stray_folders_quarantined"] = []
                payload["stray_folders_skipped"] = []
                payload["stray_folders_flagged"] = []

                for folder_path in quarantine_folder_paths:
                    if not cmds.objExists(folder_path):
                        # gone by commit time, artist changed the scene since
                        # analysis, not an error, their changes win over the plan
                        payload["stray_folders_skipped"].append(folder_path)
                        continue

                    # re-check even though a human approved this row already,
                    # the scene can change between analysis and commit
                    is_safe, reasons = self._quarantine_safety_check(folder_path)
                    if not is_safe:
                        logger.warning(
                            f"{folder_path} was approved for quarantine but {', '.join(reasons)} "
                            f"- left in place for manual review, not touched."
                        )
                        payload["stray_folders_flagged"].append(folder_path)
                        continue

                    if deprecated_group_path is None:
                        deprecated_group_path = self._ensure_deprecated_holding_group()

                    cmds.lockNode(deprecated_group_path, lock=False)
                    try:
                        parented = cmds.parent(folder_path, deprecated_group_path)
                        new_path = cmds.ls(parented[0], long=True)[0] if parented else folder_path
                    except RuntimeError as err:
                        logger.warning(f"Failed to quarantine {folder_path}: {err}")
                        payload["stray_folders_flagged"].append(folder_path)
                        continue
                    finally:
                        cmds.lockNode(deprecated_group_path, lock=True)

                    self._stamp_quarantine_date(new_path)
                    logger.info(
                        f"{folder_path} approved for quarantine by artist - "
                        f"moved to {deprecated_group_path} (not deleted, "
                        f"fully recoverable)."
                    )
                    payload["stray_folders_quarantined"].append(new_path)

                payload["status"] = "success"
                payload["count"] = len(moved_objects)
                payload["objects"] = moved_objects

            except (NameError, AttributeError, ImportError):
                # these mean an actual coding bug (typo, missing import,
                # wrong attribute name), not a legitimate scene/data
                # condition. let them crash loudly here at the engine
                # boundary instead of getting silently reported as
                # "Sort failed" to whoever's running the tool - this is
                # the exact class of bug the MayaUndoChunk NameError was
                raise
            except Exception as err:
                logger.error(f"CRITICAL FAILURE: {err}")
                payload["status"] = "error"
                payload["message"] = str(err)
            finally:
                if progress_window_active:
                    try:
                        cmds.progressWindow(endProgress=True)
                    except RuntimeError:
                        pass
        return payload
