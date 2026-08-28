from __future__ import annotations

import os
import json
import logging
from typing import Any

import maya.cmds as cmds

logger = logging.getLogger("NexusSorter")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('[Nexus %(levelname)s] %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# nested schema on purpose: {"prefixes": {...}, "purge_age_threshold_days": N}
# used to be flat and that broke the second we needed a 2nd setting.
_nexus_config_cache: dict[str, Any] | None = None


def _load_nexus_config() -> dict[str, Any]:
    """loads + caches nexus_config.json. returns {} if it's missing or
    broken, each setting getter below picks its own default for that."""
    global _nexus_config_cache
    if _nexus_config_cache is not None:
        return _nexus_config_cache

    try:
        # config.json sits next to the nexus_sorter folder, not inside it,
        # so gotta hop up one dir from this file's own location
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        base_dir = os.path.dirname(pkg_dir)
    except NameError:
        logger.warning("__file__ not defined (running from Script Editor). Defaulting to current Maya workspace.")
        base_dir = cmds.workspace(query=True, rootDirectory=True)
    config_path = os.path.join(base_dir, "nexus_config.json")

    try:
        with open(config_path, "r") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("nexus_config.json has to be a JSON object at the top level")
        _nexus_config_cache = loaded
        logger.info(f"Loaded config from {config_path}")
    except (OSError, ValueError):
        logger.warning(f"nexus_config.json missing or unreadable at {config_path}. Using built-in defaults.")
        _nexus_config_cache = {}

    return _nexus_config_cache


def reset_config_cache() -> None:
    """clears the cached config so the next _load_nexus_config() call
    re-reads from disk. mainly for tests that need to exercise more than
    one config state in the same process, production code never needs this."""
    global _nexus_config_cache
    _nexus_config_cache = None
