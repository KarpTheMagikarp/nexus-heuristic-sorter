# Nexus Heuristic Sorter

A Maya tool that sorts a messy scene into clean category folders (Characters, Props, Environment, VFX, Lighting). It reads what an asset actually is (joints, skinClusters, light and sim shapes, shared materials, naming prefixes), renames it to a consistent convention, and shows you the whole plan before anything moves.

## Install

1. Download the latest release zip (or **Code > Download ZIP**) and unzip it. Keep `drag_to_install_nexus.py`, the `nexus_sorter` folder and `nexus_config.json` together.
2. Drag `drag_to_install_nexus.py` into the Maya viewport.
3. Click the **NexusSorter** button on the **Custom** shelf.

No restart needed. Dragging it in again updates an existing install.

**Requirements:** Maya 2022 or newer (PySide2 or PySide6, both handled). No extra Python packages, no compiled code.

## Using it

1. Click **Analyze** to preview the plan. Nothing in the scene changes yet.
2. Review the tree. Right-click a row (or click the arrow in Destination) to reassign it.
3. Click **Commit Changes**.

## Safety

- Analyze never touches the scene.
- Referenced and locked nodes are never moved or renamed.
- Rigs and sims are moved as a whole and never renamed internally or frozen.
- A rig is never split. If a skinned mesh's skeleton sits outside its group (a Blender armature next to its mesh, say), nothing moves and it's listed under Skipped with the reason.
- Leftover folders are moved to `GRP_DEPRECATED_FOLDERS`, not deleted.
- Permanent deletion only happens from the Advanced purge drawer, behind a confirm checkbox, and writes a JSON manifest of what it removed first.

## Studio config

`nexus_config.json` maps naming prefixes to folders and sets how long quarantined folders are kept before they can be purged. Delete it to run on the built-in defaults.

To teach the sorter your studio's words, add them under `category_keywords`. These extend the built-in list, they don't replace it:

```json
"category_keywords": {
  "GRP_03_ENVIRONMENT": ["door", "gate"],
  "GRP_02_PROPS": ["lantern"]
}
```

## How it decides

1. **What the asset is.** Lights, sim shapes and skeletons are checked first. Anything with a skeleton or skinCluster is rig shielded: moved as one unit, never renamed internally, never frozen.
2. **What it's called.** Studio prefixes like `CH_` win outright. Otherwise the folder with the most matching words wins. A rigged asset only leaves Characters when its name clearly says something else, like a rigged tapestry going to Environment.
3. **When it can't tell, it asks.** A name that points two ways or nowhere goes to Manual Assignment instead of a guess.
4. **It remembers you.** A manual reassignment, or an asset you dragged into a different category folder yourself, keeps that category on every later run.

## Performance

Analyze runs one pass over the scene, so cost grows with the number of objects instead of with its square. Shared-material and sibling lookups used to be re-done for every object. Profiled on a simulated 1,000-piece kit sharing one material, that check alone went from about 1,000,000 Maya calls to about 2,000, with identical results. Lookups are cached for a single Analyze and thrown away after, so a changed scene never gets a stale answer.

## Troubleshooting

**"Access is denied" / WinError 5 during install.** Your Documents folder is probably synced by OneDrive or protected by Windows Security's *Controlled folder access*. The installer falls back to `%LOCALAPPDATA%\NexusSorter` on its own and tells you when it did. If both locations are blocked, pause OneDrive sync or allow `maya.exe` under *Controlled folder access*, then drag the installer in again.

**Still getting the same install error after updating.** Maya 2022 and newer cache a dropped script for the rest of the session, so dragging in a new installer can quietly run an older one you dropped earlier. Restart Maya once and drag the new installer in again. Current installers clear themselves from that cache, so this only bites when upgrading from an older one.

**The shelf button is missing.** Drag the installer in again. It replaces the button rather than duplicating it.

## Running the tests

```
pip install pytest
pytest
```

The tests stub out Maya and Qt, so they run on any machine without Maya installed.
