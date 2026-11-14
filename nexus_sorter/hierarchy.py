from __future__ import annotations

import types
from typing import Iterable

import maya.cmds as cmds
import maya.api.OpenMaya as om

from .config import logger


class MayaUndoChunk:
    """wraps everything in one undo chunk so ctrl+z doesn't leave the scene half-sorted."""
    def __init__(self, chunk_name: str = "NexusSortingPass") -> None:
        self.chunk_name = chunk_name

    def __enter__(self) -> MayaUndoChunk:
        cmds.undoInfo(openChunk=True, chunkName=self.chunk_name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> bool | None:
        cmds.undoInfo(closeChunk=True)
        if exc_type is not None:
            logger.error(f"Transaction aborted due to uncaught error: {exc_val}")
            return False

class RigSpansGroups(Exception):
    """the mesh's skeleton lives somewhere no single rig group covers.
    moving the mesh alone would split the rig, so the caller skips it."""


class HierarchyManager:
    """dag path lookups, safe renames, reference/lock checks - the boring stuff
    that's easy to get subtly wrong if you just use cmds directly everywhere."""
    @staticmethod
    def get_dag_path(node_name: str) -> om.MDagPath | None:
        sel = om.MSelectionList()
        try:
            sel.add(node_name)
            return sel.getDagPath(0)
        except RuntimeError:
            return None

    @staticmethod
    def get_safe_short_name(dag_path: str | om.MDagPath) -> str:
        if isinstance(dag_path, str):
            dag_path = HierarchyManager.get_dag_path(dag_path)
        if not dag_path:
            return ""
        fn_node = om.MFnDagNode(dag_path)
        return fn_node.name().split(':')[-1]

    @staticmethod
    def get_root_ancestor(node_name: str, reserved_names: Iterable[str] | None = None) -> str:
        """walks up to the top-level group under world, so the rig shield
        can move a whole rig as one unit. reserved_names stops it from
        climbing past our own sort folders (GRP_01_CHARACTERS etc), or
        an already-sorted rig gets mistaken for its own containing folder."""
        reserved_names = reserved_names or set()
        long_paths = cmds.ls(node_name, long=True)
        if not long_paths:
            return node_name

        parts = [p for p in long_paths[0].split('|') if p]
        if not parts:
            return long_paths[0]

        depth = 0
        while depth < len(parts) - 1 and parts[depth] in reserved_names:
            depth += 1
        return "|" + "|".join(parts[:depth + 1])

    @classmethod
    def find_rig_root(
        cls,
        node_name: str,
        reserved_names: Iterable[str] | None = None,
        memo: dict | None = None,
    ) -> str | None:
        """smallest group that holds this mesh's whole rig and nothing else,
        or None when there's no skeleton to anchor on (caller falls back to
        get_root_ancestor, same as before this existed).

        get_root_ancestor alone assumes every rig is its own top-level
        group. put a character under |LEVEL and one skinned mesh dragged
        the whole level into characters.

        starts at the lowest common ancestor of the mesh and its joints,
        which always holds both, then climbs only while the next group up
        holds nothing but this rig. controls, helper joints and proxy geo
        driven by our joints count as ours. any other mesh means stop.

        raises RigSpansGroups when there IS a skeleton but no group holds
        just this rig: mesh and joints in different top-level groups (a
        Blender armature next to its mesh), or their only shared group
        also holds someone else's rig. moving the mesh alone would split
        the rig, so the caller leaves it where it is."""
        reserved = set(reserved_names or ())
        node_long = (cmds.ls(node_name, long=True) or [node_name])[0]
        joints = cls._rig_joints(node_long)
        if not joints:
            return None

        start = cls._common_ancestor([node_long, *joints])
        key = (start, frozenset(joints))
        if memo is not None and key in memo:
            return memo[key]

        if start is None or (start.count('|') == 1 and start.strip('|') in reserved):
            raise RigSpansGroups(node_long)
        # loose static meshes inside a rig group are normal (blendShape
        # targets, accessories), another character's skinned mesh isn't
        if cls._holds_other_rigs(start, joints):
            raise RigSpansGroups(node_long)

        current = start
        while True:
            parent = cmds.listRelatives(current, parent=True, fullPath=True)
            if not parent:
                break
            parent = parent[0]
            if parent.count('|') == 1 and parent.strip('|') in reserved:
                break
            if cls._holds_other_assets(parent, current, joints):
                break
            current = parent

        if memo is not None:
            memo[key] = current
        return current

    @classmethod
    def _rig_joints(cls, node_long: str) -> set[str]:
        joints = set(cmds.ls(node_long, type='joint', long=True) or [])
        joints.update(cmds.listRelatives(node_long, allDescendents=True, type='joint', fullPath=True) or [])
        shapes = cmds.listRelatives(node_long, shapes=True, fullPath=True, noIntermediate=True) or []
        joints.update(cls._skin_influences(shapes))
        return joints

    @staticmethod
    def _skin_influences(shapes: list[str]) -> set[str]:
        # same single-hop upstream check the anatomy scan uses, skinCluster
        # feeds the shape's inMesh directly. no listHistory walk
        if not shapes:
            return set()
        upstream = cmds.listConnections(shapes, source=True, destination=False) or []
        influences = set()
        for skin in set(cmds.ls(upstream, type='skinCluster') or []):
            influences.update(cmds.ls(cmds.skinCluster(skin, query=True, influence=True) or [],
                                      type='joint', long=True) or [])
        return influences

    @staticmethod
    def _common_ancestor(paths: list[str]) -> str | None:
        split = [[p for p in path.split('|') if p] for path in paths]
        common = []
        for parts in zip(*split):
            if len(set(parts)) != 1:
                break
            common.append(parts[0])
        return "|" + "|".join(common) if common else None

    @classmethod
    def _holds_other_assets(cls, parent: str, current: str, joints: set[str]) -> bool:
        """true if parent holds a mesh that isn't part of this rig. stray
        joints don't count on their own, rigs carry IK chains and helper
        joints that never skin anything."""
        candidates = list(cmds.listRelatives(parent, shapes=True, fullPath=True) or [])
        for child in cmds.listRelatives(parent, children=True, fullPath=True) or []:
            if child == current or child in candidates:
                continue
            candidates.extend(cmds.listRelatives(child, allDescendents=True, type='mesh', fullPath=True) or [])

        for mesh in cmds.ls(candidates, type='mesh', noIntermediate=True, long=True) or []:
            if cls._skin_influences([mesh]) & joints:
                continue
            owner = (cmds.listRelatives(mesh, parent=True, fullPath=True) or [mesh])[0]
            if cls._driven_by_joints(owner, joints):
                continue
            return True
        return False

    @classmethod
    def _holds_other_rigs(cls, group: str, joints: set[str]) -> bool:
        meshes = cmds.ls(cmds.listRelatives(group, allDescendents=True, type='mesh', fullPath=True) or [],
                         type='mesh', noIntermediate=True, long=True) or []
        for mesh in meshes:
            influences = cls._skin_influences([mesh])
            if influences and not influences & joints:
                return True
        return False

    @staticmethod
    def _driven_by_joints(transform: str, joints: set[str]) -> bool:
        """proxy geo and mesh-shaped controls ride the rig through a
        constraint or a matrix connection instead of a skin. one or two hops
        upstream catches constraints (joint -> constraint -> transform) and
        offsetParentMatrix setups (joint -> multMatrix -> transform)."""
        sources = cmds.listConnections(transform, source=True, destination=False) or []
        if set(cmds.ls(sources, type='joint', long=True) or []) & joints:
            return True
        upstream = cmds.listConnections(sources, source=True, destination=False) or [] if sources else []
        return bool(set(cmds.ls(upstream, type='joint', long=True) or []) & joints)

    @classmethod
    def is_node_locked_or_referenced(cls, node_name: str) -> bool:
        try:
            if cmds.referenceQuery(node_name, isNodeReferenced=True): return True
            locks = cmds.lockNode(node_name, query=True, lock=True)
            if locks and locks[0]: return True
        except RuntimeError:
            return True
        return False

    # normal maya default-scene wiring, not an exhaustive safe list, just
    # a conservative allowlist. anything else connected gets flagged
    _BENIGN_DEFAULT_CONNECTIONS = {
        "defaultObjectSet", "defaultLightSet", "defaultRenderLayer",
        "defaultLayer", "time1", "lightLinker1", "renderLayerManager",
        "renderPartition", "shared",
    }

    @classmethod
    def has_external_connections(cls, node_name: str) -> bool:
        """true if node_name has any connection beyond maya's default
        wiring. catches expressions/constraints/script hooks that
        listRelatives(children=True) would miss entirely since none of
        those are DAG children."""
        try:
            connections = cmds.listConnections(node_name, source=True, destination=True) or []
        except RuntimeError:
            # can't even query it safely - treat that as "meaningful", not "clean"
            return True
        return any(c not in cls._BENIGN_DEFAULT_CONNECTIONS for c in connections)

    @classmethod
    def execute_safe_rename(cls, dag_path_str: str, new_name: str) -> str | None:
        """cmds.rename, not an API MDagModifier. API edits made outside a
        command plugin never hit Maya's undo queue, so ctrl+z after a sort
        put the hierarchy back but left every new name behind.

        a bare new name lands in whatever namespace is CURRENT, not the
        node's own, so the node's namespace gets passed back explicitly as
        an absolute path. ignoreShape keeps shapes untouched like before.
        rename can tack digits on to dodge a clash, so the real result
        comes back by uuid instead of trusting new_name."""
        if cls.is_node_locked_or_referenced(dag_path_str):
            logger.warning(f"Skipping locked/referenced node: {dag_path_str}")
            return None

        try:
            uuid = cmds.ls(dag_path_str, uuid=True)
            if not uuid:
                return None
            namespace = dag_path_str.rsplit('|', 1)[-1].rpartition(':')[0]
            cmds.rename(dag_path_str, f":{namespace}:{new_name}" if namespace else f":{new_name}", ignoreShape=True)
            renamed = cmds.ls(uuid[0], long=True)
            return renamed[0] if renamed else None
        except RuntimeError as err:
            logger.error(f"Rename failed on {dag_path_str}: {err}")
            return None
