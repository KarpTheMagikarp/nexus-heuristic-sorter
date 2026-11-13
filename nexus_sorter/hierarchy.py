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
