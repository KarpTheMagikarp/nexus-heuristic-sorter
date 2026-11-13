from __future__ import annotations

import re
import enum
from typing import Any

import maya.cmds as cmds

from .config import logger, _load_nexus_config
from .hierarchy import HierarchyManager


class Confidence(enum.Enum):
    """how sure the heuristic is. closed set on purpose so the UI can
    branch on these safely - don't want 'Low' accidentally meaning
    something different somewhere down the line."""
    HIGH = "High Confidence (80-100%)"
    MEDIUM = "Medium Confidence (50-79%)"
    LOW = "Low Confidence (<50%)"


class AssignmentSource(enum.Enum):
    """why an asset landed where it did. used to get mashed into the
    confidence value (a fake 'High (Forced)' string) but a forced
    override isn't a confidence level, it's a totally different thing."""
    AUTO_DETECTED = "Auto-Detected"
    USER_FORCED = "User Override"
    # an earlier human decision read back off the scene, see
    # NexusBatchProcessor._remembered_category
    REMEMBERED = "Remembered"
    SKIPPED_REFERENCED = "Skipped (Referenced)"


class CategorizationEngine:
    """routes assets by name/structure only, no DG pulls, so it stays
    fast even on scenes with 10k+ objects."""

    _DEFAULT_PREFIXES = {
        "CH_": "GRP_01_CHARACTERS",
        "SK_": "GRP_01_CHARACTERS",
        "PR_": "GRP_02_PROPS",
        "SM_": "GRP_02_PROPS",
        "ENV_": "GRP_03_ENVIRONMENT",
        "FX_": "GRP_04_VFX",
        "LT_": "GRP_05_LIGHTING",
    }

    # matched against whole tokens not substrings so "LIGHT" doesn't light up on "LIGHTHOUSE".
    # no "rig" for characters on purpose: joints already prove something's rigged,
    # and the word alone was dragging camera_rig/light_rig into characters
    _CATEGORY_SYNONYMS = {
        "GRP_01_CHARACTERS": {
            "char", "character", "characters", "hero", "npc", "creature",
            "enemy", "actor", "avatar", "monster", "boss", "humanoid",
            "biped", "quadruped",
        },
        "GRP_02_PROPS": {
            "prop", "props", "item", "items", "weapon", "sword", "chair",
            "table", "barrel", "crate", "box", "tool", "gadget", "device",
            "furniture", "cauldron", "stool", "bench", "shelf", "shield",
            "shields", "axe", "spear", "bow", "bottle", "jar", "vase", "pot",
            "bucket", "basket", "book", "books", "cup", "mug", "plate",
            "bowl", "rope", "chain",
        },
        "GRP_03_ENVIRONMENT": {
            "env", "environment", "set", "terrain", "ground", "rock",
            "rocks", "boulder", "cliff", "mountain", "tree", "trees",
            "foliage", "plant", "plants", "building", "structure", "wall",
            "floor", "kit", "module", "landscape", "pillar", "pillars",
            "column", "columns", "arch", "archway", "doorway", "stairs",
            "staircase", "bridge", "fence", "roof", "ceiling", "beam",
            "beams", "ruin", "ruins", "statue", "tapestry", "tapestries",
            "banner", "banners", "flag", "flags", "curtain", "curtains",
            "drape", "drapes", "rug", "carpet",
        },
        "GRP_04_VFX": {
            "fx", "vfx", "particle", "particles", "smoke", "fire",
            "explosion", "spark", "sparks", "dust", "cloud",
        },
        "GRP_05_LIGHTING": {
            "light", "lights", "lgt", "lamp", "sun", "sky", "bulb",
        },
    }

    # catch-all words that show up across categories ("table_set", "rock_kit").
    # half weight, so they can break a tie but never create one
    _WEAK_WORDS = frozenset({"set", "kit", "module", "structure", "item", "items", "tool", "device", "gadget"})

    # descriptor words that don't say what the asset actually is, safe to
    # strip. "stylized" is NOT junk though, it's a real style tag, kept
    # separately in _PRESERVED_DESCRIPTORS below
    _JUNK_DESCRIPTORS = frozenset({
        "generic", "temp", "wip", "test", "draft", "final", "misc",
    })

    # kept but not tracked as junk, used by _segment_fused_token
    _PRESERVED_DESCRIPTORS = frozenset({"stylized"})


    # just the literal category labels, would stutter against the prefix
    # (unlike the broader synonym bank above, "rock" needs to survive)
    _STUTTER_WORDS = {
        "GRP_01_CHARACTERS": {"char", "character", "characters"},
        "GRP_02_PROPS": {"prop", "props"},
        "GRP_03_ENVIRONMENT": {"env", "environment"},
        "GRP_04_VFX": {"fx", "vfx"},
        "GRP_05_LIGHTING": {"light", "lights", "lgt"},
    }

    _LIGHT_TYPES = frozenset({"directionalLight", "pointLight", "spotLight", "areaLight"})
    # a shape node of one of these types only ever exists for one reason,
    # same certainty level as a light or a joint. bifrostGraphShape catches
    # bifrost sims specifically since this studio's already got bifrost loaded
    _VFX_SHAPE_TYPES = frozenset({
        "nParticle", "particle", "nCloth", "hairSystem", "fluidShape",
        "bifrostGraphShape", "instancer",
    })
    _COLLISION_PREFIXES = ("UCX_", "UBX_", "MCPP_")
    _DEFORMER_TYPES_HARD = ("skinCluster", "blendShape")
    _DEFORMER_TYPES_SOFT = ("cluster", "lattice", "wire", "nonLinear")
    # a hinge or a chest lid usually gets by on 1-2 joints, a real character
    # rig practically never does (a biped alone is spine + 2 arms + 2 legs
    # + neck/head, 10+ joints easy). below this, a joint count alone isn't
    # proof of a real rig - see score_node_anatomy
    _MIN_JOINTS_FOR_RIG_VETO = 3

    # weighted scoring. physical evidence (joint chain, hard deformer) vetoes
    # outright. everything else adds up so a strong signal beats a weak one
    # regardless of which pass ran first (old if/else chain let a material
    # check silently override an explicit CH_ prefix, this fixes that)
    SCORE_VETO = 10_000            # joints / hard deformers - physical fact
    SCORE_EXPLICIT_PREFIX = 500    # CH_, PR_, etc - a deliberate naming convention
    SCORE_SOFT_DEFORMER = 150      # cluster/lattice/wire - rig-adjacent, not sacred
    SCORE_SHARED_MATERIAL = 120    # reused shading network - modular kit fingerprint
    SCORE_NAME_SYNONYM = 100       # a recognized category word in the name
    SCORE_HIERARCHY_PARENT = 40    # borrowed from the parent group's name
    SCORE_HIERARCHY_SIBLING = 10   # borrowed from a sibling that already resolved

    CONFIDENCE_HIGH_FLOOR = 500
    CONFIDENCE_MEDIUM_FLOOR = 100

    # category-wide freeze block - characters and VFX are rig/sim-driven by
    # definition, not just when the anatomy scan happens to trip the shield
    NO_FREEZE_CATEGORIES = frozenset({"GRP_01_CHARACTERS", "GRP_04_VFX"})

    HIGH = Confidence.HIGH
    MEDIUM = Confidence.MEDIUM
    LOW = Confidence.LOW

    _prefix_map = None
    _synonyms = None
    _vocabulary = None

    @classmethod
    def get_category_synonyms(cls) -> dict[str, frozenset[str]]:
        """built-in words plus whatever nexus_config.json's
        category_keywords adds. extends, never replaces, so a studio
        config can't accidentally wipe out the defaults. unknown folders
        get dropped with a warning since nothing would create them."""
        if cls._synonyms is not None:
            return cls._synonyms

        merged = {dest: set(words) for dest, words in cls._CATEGORY_SYNONYMS.items()}
        extra = _load_nexus_config().get("category_keywords") or {}
        if not isinstance(extra, dict):
            logger.warning("nexus_config.json's category_keywords should map a folder to a word list, ignoring it.")
            extra = {}
        for dest, words in extra.items():
            if dest not in merged:
                logger.warning(f"category_keywords has unknown folder {dest!r}, ignoring it.")
                continue
            if isinstance(words, str) or not all(isinstance(w, str) for w in words or []):
                logger.warning(f"category_keywords[{dest!r}] should be a list of words, ignoring it.")
                continue
            merged[dest].update(w.lower() for w in words)

        cls._synonyms = {dest: frozenset(words) for dest, words in merged.items()}
        return cls._synonyms

    @classmethod
    def _known_vocabulary(cls) -> frozenset[str]:
        # derived from the merged synonyms so config words segment too
        if cls._vocabulary is None:
            cls._vocabulary = frozenset().union(
                *cls.get_category_synonyms().values(), cls._JUNK_DESCRIPTORS, cls._PRESERVED_DESCRIPTORS
            )
        return cls._vocabulary

    @classmethod
    def get_prefix_map(cls) -> dict[str, str]:
        if cls._prefix_map is not None:
            return cls._prefix_map

        prefixes_raw = _load_nexus_config().get("prefixes")
        if prefixes_raw:
            cls._prefix_map = {prefix.upper(): dest for prefix, dest in prefixes_raw.items()}
        else:
            cls._prefix_map = dict(cls._DEFAULT_PREFIXES)

        return cls._prefix_map

    @classmethod
    def reset_prefix_map_cache(cls) -> None:
        """clears the cached prefix map so the next get_prefix_map() call
        re-reads config. same reasoning as config.reset_config_cache,
        tests need this, production code doesn't."""
        cls._prefix_map = None
        cls._synonyms = None
        cls._vocabulary = None

    @classmethod
    def score_node_anatomy(cls, dag_path_str: str) -> tuple[dict[str, int], bool]:
        """checks what's actually attached to the node. returns
        (scores_dict, needs_rig_shield). a name can lie about what
        something is, a skinCluster can't.

        used to walk the whole upstream graph with listHistory which was
        what locked Maya up on heavy rigs. skinCluster/blendShape always
        sit directly upstream of the shape input, so a single-hop
        listConnections check finds them just as reliably without
        touching the rest of the graph.

        joint count matters here too - a bare "any joints at all" check
        was routing door hinges and chest lids to Characters at full
        confidence, since those get by on 1-2 joints same as a real rig's
        first bone. below _MIN_JOINTS_FOR_RIG_VETO, a joint or two only
        counts as a weak signal unless there are control curves nearby
        too (the circles/boxes animators actually select), which is
        about as strong a "this is a real rig" tell as the joint count
        itself."""
        scores = {}
        try:
            joints = cmds.listRelatives(dag_path_str, allDescendents=True, type='joint') or []
            if cmds.nodeType(dag_path_str) == 'joint' or len(joints) >= cls._MIN_JOINTS_FOR_RIG_VETO:
                scores["GRP_01_CHARACTERS"] = cls.SCORE_VETO
                return scores, True

            if joints:
                control_curves = cmds.listRelatives(dag_path_str, allDescendents=True, type='nurbsCurve') or []
                if control_curves:
                    # joints plus control curves together is as strong a
                    # tell as a full joint chain on its own
                    scores["GRP_01_CHARACTERS"] = cls.SCORE_VETO
                    return scores, True
                scores["GRP_01_CHARACTERS"] = scores.get("GRP_01_CHARACTERS", 0) + cls.SCORE_SOFT_DEFORMER

            shapes = cmds.listRelatives(dag_path_str, shapes=True, fullPath=True, noIntermediate=True) or []
            if shapes:
                # listConnections only takes one type at a time, so grab everything
                # upstream once and filter with cmds.ls instead (takes a list)
                upstream = cmds.listConnections(shapes, source=True, destination=False) or []

                # skinCluster or blendShape directly upstream means this mesh
                # is being deformed by a rig - doesn't matter what it's called
                hard_hits = cmds.ls(upstream, type=list(cls._DEFORMER_TYPES_HARD)) if upstream else []
                if hard_hits:
                    scores["GRP_01_CHARACTERS"] = cls.SCORE_VETO
                    return scores, True

                # softer deformers still smell like rigging, just not sacred
                # enough on their own to trip the shield - lattices and
                # clusters turn up on simple prop rigs too
                soft_hits = cmds.ls(upstream, type=list(cls._DEFORMER_TYPES_SOFT)) if upstream else []
                if soft_hits:
                    scores["GRP_01_CHARACTERS"] = scores.get("GRP_01_CHARACTERS", 0) + cls.SCORE_SOFT_DEFORMER

            # plain geo, no deformers - check if the material's shared across
            # a bunch of other objects. reused materials are the classic
            # fingerprint of a modular environment kit piece
            if cls.has_shared_material(dag_path_str):
                scores["GRP_03_ENVIRONMENT"] = scores.get("GRP_03_ENVIRONMENT", 0) + cls.SCORE_SHARED_MATERIAL

        except RuntimeError:
            pass

        return scores, False

    @classmethod
    def has_shared_material(cls, dag_path_str: str) -> bool:
        try:
            shapes = cmds.listRelatives(dag_path_str, shapes=True, fullPath=True, noIntermediate=True) or []
            if not shapes:
                return False

            shading_engines = set(cmds.listConnections(shapes, type='shadingEngine') or [])
            for sg in shading_engines:
                if sg in ('initialShadingGroup', 'initialParticleSE'):
                    continue  # default maya groups, not a real signal either way

                members = cmds.sets(sg, query=True) or []
                owners = set()
                for member in members:
                    owner = cmds.listRelatives(member.split('.')[0], parent=True, fullPath=True)
                    if owner:
                        owners.add(owner[0])

                if len(owners) > 2:
                    return True
            return False
        except RuntimeError:
            return False

    @classmethod
    def tokenize_asset_name(cls, short_name: str) -> list[str]:
        """splits on underscores and camelCase, peels trailing digits off
        (Archway1 -> archway), splits fused known words (stylizedrock ->
        stylized, rock), then drops the numbers and v01 tags so they don't
        pollute the category match. same splitting derive_proposed_name
        uses, so what the name reads as and what it gets renamed to agree."""
        short_name = cls._readable_fbx_name(short_name)
        spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', short_name)
        tokens = []
        for raw in (t for t in spaced.split('_') if t):
            for piece in cls._isolate_trailing_digits(raw):
                if cls._PURE_VARIANT_RE.match(piece):
                    continue
                tokens.extend(cls._segment_fused_token(piece))
        return tokens

    @classmethod
    def _name_hits(cls, short_name: str) -> tuple[dict[str, float], bool]:
        """({destination: weighted hits}, came_from_explicit_prefix). split
        out so the rig check can pool hits across a whole path instead of
        trusting one node's name."""
        upper_name = short_name.upper()

        # an explicit studio prefix (CH_, PR_, ENV_...) still wins outright,
        # that's a deliberate naming convention, not a guess
        prefix_hit = next((dest for prefix, dest in cls.get_prefix_map().items() if upper_name.startswith(prefix)), None)
        if prefix_hit:
            return {prefix_hit: 1}, True
        if upper_name.startswith(cls._COLLISION_PREFIXES):
            return {"GRP_03_ENVIRONMENT": 1}, True

        tokens = {t.lower() for t in cls.tokenize_asset_name(short_name)}
        hits = {}
        for dest, words in cls.get_category_synonyms().items():
            matched = tokens & words
            if matched:
                hits[dest] = sum(0.5 if w in cls._WEAK_WORDS else 1.0 for w in matched)
        return hits, False

    @classmethod
    def score_name_tokens(cls, short_name: str) -> tuple[str | None, int]:
        """(destination, score) off the name's real words. a studio prefix
        clears CONFIDENCE_HIGH_FLOOR outright, a synonym match only gets
        medium territory.

        most matching words wins. a tie means the name genuinely points two
        ways (light_rig, table_set), so it returns nothing and lets
        hierarchy context or manual review decide. the old version took
        whichever category happened to be listed first, which is a coin
        flip that looks confident."""
        hits, explicit = cls._name_hits(short_name)
        if not hits:
            return None, 0
        if explicit:
            return next(iter(hits)), cls.SCORE_EXPLICIT_PREFIX

        best = max(hits.values())
        leaders = [dest for dest, count in hits.items() if count == best]
        if len(leaders) > 1:
            return None, 0
        return leaders[0], cls.SCORE_NAME_SYNONYM

    @classmethod
    def score_hierarchy_context(cls, dag_path_str: str) -> tuple[str | None, int]:
        """last resort once the name gave us nothing - borrows a score
        from a parent group's name or a sibling that already resolved,
        scored low since it's a guess about this asset, not a read of it."""
        parent = cmds.listRelatives(dag_path_str, parent=True, fullPath=True)
        if not parent:
            return None, 0

        parent_short = HierarchyManager.get_safe_short_name(parent[0])
        dest, _ = cls.score_name_tokens(parent_short)
        if dest:
            return dest, cls.SCORE_HIERARCHY_PARENT

        # still nothing - see if a sibling already picked a lane. weak signal,
        # but on a real production scene it beats a coin flip
        siblings = cmds.listRelatives(parent[0], children=True, fullPath=True) or []
        for sib in siblings:
            if sib == dag_path_str:
                continue
            sib_dest, _ = cls.score_name_tokens(HierarchyManager.get_safe_short_name(sib))
            if sib_dest:
                return sib_dest, cls.SCORE_HIERARCHY_SIBLING

        return None, 0

    @classmethod
    def _path_names(cls, dag_path_str: str) -> list[str]:
        """short names from the scene root down to this node, minus our own
        sort folders. the rig root usually carries the real name
        (BaseTapestry) while the skinned mesh under it is body_geo or
        pCube12, so one node's name isn't enough to go on."""
        long_paths = cmds.ls(dag_path_str, long=True) or [dag_path_str]
        names = []
        for part in long_paths[0].split('|'):
            short = part.split(':')[-1]
            if not short or short.startswith("GRP_") or short == "UNASSIGNED_ASSETS":
                continue
            names.append(short)
        return names

    @classmethod
    def _category_for_rigged(cls, dag_path_str: str) -> tuple[str, Confidence]:
        """a skeleton proves something's rigged, not that it's a character.
        tapestries, flags, doors and chains all get joints too.

        only leaves characters when the names along its path point at
        exactly ONE other category and nothing in them says character.
        tree_creature stays a character, a name pointing two ways stays a
        character, no name signal at all stays a character. that's the old
        behavior for every case except a clean, unambiguous name. the rig
        shield stays on no matter where it lands, caller handles that."""
        pooled: dict[str, float] = {}
        explicit = set()
        for name in cls._path_names(dag_path_str):
            hits, from_prefix = cls._name_hits(name)
            for dest, count in hits.items():
                pooled[dest] = pooled.get(dest, 0) + count
            if from_prefix:
                explicit.update(hits)

        if "GRP_01_CHARACTERS" in pooled or len(pooled) != 1:
            return "GRP_01_CHARACTERS", cls.HIGH

        dest = next(iter(pooled))
        # a loose word match is medium even with joints, the joints vouch
        # for "rigged", not for the word
        return dest, (cls.HIGH if dest in explicit else cls.MEDIUM)

    @classmethod
    def _confidence_from_score(cls, score: int) -> Confidence:
        if score >= cls.CONFIDENCE_HIGH_FLOOR:
            return cls.HIGH
        if score >= cls.CONFIDENCE_MEDIUM_FLOOR:
            return cls.MEDIUM
        return cls.LOW

    @classmethod
    def is_freeze_blocked(cls, destination: str) -> bool:
        """characters and vfx are rig/simulation territory by category,
        not just when the anatomy scan happens to catch a specific
        object. baking their transform is destructive, so freezing is
        blocked for the whole category, not just flagged objects"""
        return destination in cls.NO_FREEZE_CATEGORIES

    @classmethod
    def analyze_asset_dna(cls, dag_path_str: str, short_name: str) -> tuple[str, Confidence, bool]:
        """runs the full scoring stack, returns
        (destination, confidence, needs_rig_shield).

        anatomy gets checked first and short circuits on a veto (joint
        chain, hard deformer, a real VFX shape node, nothing outranks
        those). everything else adds into one shared scoreboard instead
        of returning early, so a strong signal can beat a weak conflicting
        one no matter which pass ran first. fixes stuff like "LT_LightPost"
        sharing a material with a bunch of environment kit pieces, old code
        let the material check win before the LT_ prefix ever got a look,
        even though the explicit prefix should've won."""
        shapes = cmds.listRelatives(dag_path_str, shapes=True, fullPath=True) or []
        if any(cmds.nodeType(shape) in cls._LIGHT_TYPES for shape in shapes):
            return "GRP_05_LIGHTING", cls.HIGH, False
        if any(cmds.nodeType(shape) in cls._VFX_SHAPE_TYPES for shape in shapes):
            # shielded same as a rig - a live sim setup has caches/expressions
            # that can depend on stable names just as much as a rig does
            return "GRP_04_VFX", cls.HIGH, True

        scores, needs_shield = cls.score_node_anatomy(dag_path_str)
        if scores.get("GRP_01_CHARACTERS", 0) >= cls.SCORE_VETO:
            dest, confidence = cls._category_for_rigged(dag_path_str)
            return dest, confidence, True

        token_dest, token_score = cls.score_name_tokens(short_name)
        if token_dest:
            scores[token_dest] = scores.get(token_dest, 0) + token_score

        # hierarchy context is a last resort - only worth consulting when
        # nothing more direct (anatomy, name) produced any signal at all
        if not scores:
            context_dest, context_score = cls.score_hierarchy_context(dag_path_str)
            if context_dest:
                scores[context_dest] = scores.get(context_dest, 0) + context_score

        if not scores:
            return "UNASSIGNED_ASSETS", cls.LOW, False

        best_dest = max(scores, key=scores.get)
        return best_dest, cls._confidence_from_score(scores[best_dest]), needs_shield

    # FBX can't store some characters in names, so it writes them as
    # FBXASC + the 3 digit ascii code. Blender's "Mesh.001" comes through as
    # MeshFBXASC046001, a name starting with 7 as FBXASC055...
    _FBX_ESCAPE_RE = re.compile(r'FBXASC(\d{3})')

    @classmethod
    def _readable_fbx_name(cls, name: str) -> str:
        """decodes FBXASC escapes, then turns whatever maya won't take in a
        name (spaces, dots, colons) into underscores. printable ascii only,
        anything else stays encoded rather than guessing."""
        def decode(match: re.Match) -> str:
            code = int(match.group(1))
            return chr(code) if 32 <= code < 127 else match.group(0)

        decoded = cls._FBX_ESCAPE_RE.sub(decode, name)
        return re.sub(r'[^a-zA-Z0-9_]', '_', decoded) if decoded != name else name

    @staticmethod
    def _strip_illegal(short_name: str) -> str:
        clean_name = re.sub(r'[^a-zA-Z0-9_]', '_', short_name)
        clean_name = re.sub(r'_+', '_', clean_name).strip('_')
        # if the name was nothing but junk characters we'd hand maya an empty
        # string here, and rename() just errors out on that - keep the original
        # instead of pretending we cleaned it
        return clean_name or short_name

    @classmethod
    def sanitize_name_strict(cls, short_name: str) -> str:
        # rig roots get no prefix, so a decoded "7_shirt" would be an illegal
        # maya name on its own. keep the FBX spelling over handing maya junk
        readable = cls._strip_illegal(cls._readable_fbx_name(short_name))
        if readable and not readable[0].isdigit():
            return readable
        return cls._strip_illegal(short_name)

    @classmethod
    def _destination_to_prefix(cls, destination: str) -> str:
        # a destination can have more than one prefix pointing at it (CH_ and
        # SK_ both land in characters) - sort so we get the same answer every
        # run instead of whatever order the config file happened to load in
        prefix_map = cls.get_prefix_map()
        matches = sorted(prefix for prefix, dest in prefix_map.items() if dest == destination)
        return matches[0] if matches else ""

    @staticmethod
    def _format_prefix_casing(prefix: str) -> str:
        """casing for a newly generated prefix: one capital, then
        lowercase, then the underscore - "ENV_" becomes "Env_". just a
        display transform on the string this tool writes, doesn't touch
        get_prefix_map's own matching which stays case-insensitive
        against the config keys, so an existing ALL-CAPS convention in
        nexus_config.json still matches fine.
        """
        if not prefix:
            return prefix
        letters = prefix.rstrip('_')
        trailing = prefix[len(letters):]
        return letters.capitalize() + trailing

    @classmethod
    def _is_stutter_token(cls, token: str, destination: str) -> bool:
        # catches spelled out labels and short tags matching a configured prefix
        low = token.lower()
        if low in cls._STUTTER_WORDS.get(destination, set()):
            return True
        return cls.get_prefix_map().get(f"{token.upper()}_") == destination

    # fused/duplicated token repair, 3 separate passes:
    # isolate trailing digits (rock41 -> rock, 41), segment fused dictionary
    # words (stylizedrock -> stylized, rock), collapse repeated runs

    # bare variant tag like "01"/"v02"/"3", left alone by isolate_trailing_digits
    # so the leading "v" doesn't get peeled off into its own orphan token
    _PURE_VARIANT_RE = re.compile(r'^v?\d+$', re.IGNORECASE)
    _TRAILING_DIGITS_RE = re.compile(r'^([a-zA-Z]+)(\d+)$')  # rock41 -> (rock, 41)

    @classmethod
    def _isolate_trailing_digits(cls, token: str) -> list[str]:
        if cls._PURE_VARIANT_RE.match(token):
            return [token]
        match = cls._TRAILING_DIGITS_RE.match(token)
        if match:
            return [match.group(1), match.group(2)]
        return [token]

    @classmethod
    def _segment_fused_token(cls, token: str) -> list[str]:
        """tries to split one fused word into pieces from
        the known vocabulary, longest match first. only returns a split if it
        found a FULL match all the way through, otherwise just gives back
        [token] unchanged - never guesses at a boundary the dictionary
        can't back up, so a real one-word name that's just not in the
        vocab (made up prop name, foreign word) never gets mangled. can't
        solve general word segmentation, just handles words it knows."""
        low = token.lower()
        length = len(low)
        reachable = [False] * (length + 1)
        reachable[0] = True
        back = [None] * (length + 1)

        for end in range(1, length + 1):
            # start ascends from 0, so the first hit tried is the longest
            # possible word ending at `end` - "environment" wins over
            # "en" + "vironment" whenever both would technically work
            for start in range(0, end):
                if reachable[start] and low[start:end] in cls._known_vocabulary():
                    reachable[end] = True
                    back[end] = start
                    break

        if not reachable[length]:
            return [token]

        words, pos = [], length
        while pos > 0:
            start = back[pos]
            words.append(token[start:pos])
            pos = start
        words.reverse()
        return words

    @staticmethod
    def _collapse_repeated_runs(tokens: list[str]) -> list[str]:
        """collapses a token list that repeats the same block more than
        once, smallest block first. handles plain dupes (rock,rock),
        longer repeating blocks (rock,22,rock,22,rock), and a ragged
        trailing cycle that doesn't fully repeat (gets absorbed too since
        it's not adding any info). keeps re-running until nothing changes
        anymore, since collapsing one thing can expose a smaller repeat
        underneath it."""
        tokens = list(tokens)
        changed = True
        while changed:
            changed = False
            for width in range(1, len(tokens) // 2 + 1):
                out = []
                i = 0
                while i < len(tokens):
                    block = tokens[i:i + width]
                    if len(block) < width:
                        out.extend(tokens[i:])
                        break
                    j = i + width
                    repeated = False
                    while tokens[j:j + width] == block:
                        j += width
                        repeated = True
                    if repeated:
                        tail = tokens[j:j + width]
                        if tail and block[:len(tail)] == tail:
                            j += len(tail)  # ragged partial final cycle - absorb it too
                        out.extend(block)
                        i = j
                        changed = True
                    else:
                        out.append(tokens[i])
                        i += 1
                tokens = out
                if changed:
                    break  # token list just mutated - restart from width=1
        return tokens

    @classmethod
    def derive_proposed_name(cls, short_name: str, destination: str) -> str:
        """cleans a name up for its new home: fixes fused/duplicated
        tokens, strips junk words, drops anything that'd be redundant with
        the destination prefix (no Pr_prop_barrel), keeps the variant
        number. no scoring here, just "does this word actually mean
        anything."

        order matters: camelCase split -> underscore split -> pull
        trailing digits off each token -> dictionary segmentation on
        fused tokens -> strip junk/stutter words -> collapse repeats ->
        THEN reformat the variant tag. collapsing has to happen before
        the tag reformat or a duplicated digit gets counted twice."""
        readable = cls._readable_fbx_name(short_name)
        if readable != short_name:
            # same digit-first problem as sanitize_name_strict, matters for
            # unassigned stuff that never gets a prefix in front of it
            proposed = cls.derive_proposed_name(readable, destination)
            if not proposed[:1].isdigit():
                return proposed

        spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', short_name)
        raw_tokens = [t for t in spaced.split('_') if t]

        tokens = []
        for tok in raw_tokens:
            tokens.extend(cls._isolate_trailing_digits(tok))

        expanded = []
        for tok in tokens:
            if cls._PURE_VARIANT_RE.match(tok):
                expanded.append(tok)  # pure digits/variant tag - nothing to segment
            else:
                expanded.extend(cls._segment_fused_token(tok))
        tokens = expanded

        core_tokens = []
        for tok in tokens:
            if tok.lower() in cls._JUNK_DESCRIPTORS:
                continue
            if cls._is_stutter_token(tok, destination):
                continue  # about to get a prefix that already says this
            core_tokens.append(tok.lower())

        if not core_tokens:
            # stripped everything - better to fall back to the raw tokens
            # than hand back a blank name
            core_tokens = [t.lower() for t in tokens] or [short_name.lower()]

        core_tokens = cls._collapse_repeated_runs(core_tokens)

        if core_tokens and cls._PURE_VARIANT_RE.match(core_tokens[-1]):
            digits = re.sub(r'\D', '', core_tokens[-1])
            core_tokens[-1] = digits.zfill(2)

        prefix = cls._format_prefix_casing(cls._destination_to_prefix(destination))
        body = "_".join(core_tokens)
        proposed = f"{prefix}{body}" if prefix else body

        return cls.sanitize_name_strict(proposed)

    @staticmethod
    def check_frozen_transforms(dag_path_str: str) -> bool:
        defaults = {'tx': 0, 'ty': 0, 'tz': 0, 'rx': 0, 'ry': 0, 'rz': 0, 'sx': 1, 'sy': 1, 'sz': 1}
        try:
            return all(abs(cmds.getAttr(f"{dag_path_str}.{a}") - d) <= 0.001 for a, d in defaults.items())
        except RuntimeError:
            return True

    @staticmethod
    def try_freeze_transform(dag_path_str: str) -> bool:
        """bakes translate/rotate/scale to identity, opt-in only. maya
        refuses outright on anything driving the transform (constraint,
        expression, anim curve) or on instanced geometry. returns
        True/False so the caller can tell the artist which ones it
        couldn't touch instead of pretending it succeeded"""
        try:
            cmds.makeIdentity(dag_path_str, apply=True, translate=True, rotate=True, scale=True, normal=False)
            return True
        except RuntimeError as err:
            logger.warning(f"Could not freeze {dag_path_str}: {err}")
            return False
