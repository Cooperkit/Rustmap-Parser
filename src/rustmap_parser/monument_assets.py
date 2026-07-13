"""Maintainer extraction of sanitized gameplay metadata from monument prefabs."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .no_build_assets import _class_name, _root_game_object
from .tunnel_assets import _component, _world_matrix, bundle_identity


SCHEMA_VERSION = 2
PREFIX = "assets/bundled/prefabs/autospawn/monument/"
LANDMARK_PREFIXES = (
    PREFIX,
    "assets/bundled/prefabs/autospawn/tunnel-entrance/",
    "assets/bundled/prefabs/autospawn/tunnel-upwards/",
)
LANDMARK_CLASSES = frozenset((
    "LandmarkInfo", "MonumentInfo", "DungeonGridInfo", "DungeonBaseLandmarkInfo",
))
NAME_FILTERS = (
    "recycler", "cardreader", "card_reader", "keycard", "puzzle", "loot",
    "crate", "safezone", "safe_zone", "vending",
)
ACCESS_LEVELS = {1: "green", 2: "blue", 3: "red"}


def _loot_tier(name: str) -> int:
    normalized = name.casefold().replace(" ", "_")
    if "elite" in normalized or "tier3" in normalized or "tier_3" in normalized:
        return 3
    if "tier2" in normalized or "tier_2" in normalized:
        return 2
    if ("tier1" in normalized or "tier_1" in normalized or "low" in normalized or
            "crate_spawner" in normalized or "spawner_normal_crates" in normalized):
        return 1
    return 0


def extract_monument_metadata(rust_install_path: str | Path) -> dict:
    """Extract compact gameplay facts; UnityPy is a maintainer-only dependency."""
    import UnityPy

    install = Path(rust_install_path)
    environment = UnityPy.load(
        str(install / "Bundles" / "shared" / "assetscenes.bundle"),
        str(install / "Bundles" / "shared" / "content.bundle"),
    )
    scenes = []
    for root in environment.files.values():
        for name, asset_file in (getattr(root, "files", None) or {}).items():
            if name.startswith("BuildPlayer-AssetScene-monument.") and not name.endswith("sharedAssets"):
                scenes.append(asset_file)

    facts: dict[str, dict] = defaultdict(lambda: {
        "recycler_count": 0,
        "keycard_reader_counts": Counter(),
        "puzzle_reset_count": 0,
        "safe_zone": False,
        "vending_machine_count": 0,
        "loot_spawner_count": 0,
        "loot_tier": 0,
    })
    landmarks: dict[str, list[dict]] = defaultdict(list)
    for scene in scenes:
        # Resolve each MonoBehaviour script type once, then inspect only the
        # LandmarkInfo-derived components instead of walking every hierarchy.
        representatives = {}
        for obj in scene.objects.values():
            if obj.type.name == "MonoBehaviour":
                representatives.setdefault(obj.serialized_type.script_type_index, obj)
        landmark_types = {}
        for type_index, obj in representatives.items():
            try:
                class_name = obj.read().m_Script.read().m_ClassName
                if class_name in LANDMARK_CLASSES:
                    landmark_types[type_index] = class_name
            except Exception:
                continue
        transform_memo: dict[int, np.ndarray] = {}
        for obj in scene.objects.values():
            if (obj.type.name != "MonoBehaviour" or
                    obj.serialized_type.script_type_index not in landmark_types):
                continue
            try:
                tree = obj.read_typetree()
                game_object = scene.objects[tree["m_GameObject"]["m_PathID"]].read()
                root = _root_game_object(game_object)
                path = root.m_Name.casefold().replace("\\", "/")
                if not path.startswith(LANDMARK_PREFIXES):
                    continue
                root_transform = _component(root, "Transform")
                child_transform = _component(game_object, "Transform")
                local_matrix = (
                    np.linalg.inv(_world_matrix(root_transform, transform_memo)) @
                    _world_matrix(child_transform, transform_memo)
                )
                phrase = tree.get("displayPhrase", {})
                token = phrase.get("token") if isinstance(phrase, dict) else None
                landmarks[path].append({
                    "component_type": landmark_types[obj.serialized_type.script_type_index],
                    "object_name": str(game_object.m_Name),
                    "display_token": str(token) if token else None,
                    "should_display_on_map": bool(tree.get("shouldDisplayOnMap", False)),
                    "local_matrix": np.round(local_matrix, 12).tolist(),
                })
                # Ensure prefabs containing only landmark data are packaged.
                facts[path]
            except Exception:
                continue

        for obj in scene.objects.values():
            if obj.type.name != "GameObject":
                continue
            try:
                game_object = obj.read()
                object_name = game_object.m_Name
                lowered = object_name.casefold()
                if not any(part in lowered for part in NAME_FILTERS):
                    continue
                root = _root_game_object(game_object)
                path = root.m_Name.casefold().replace("\\", "/")
                if not path.startswith(PREFIX):
                    continue
                item = facts[path]
                possible_tier = _loot_tier(object_name)
                if possible_tier:
                    item["loot_spawner_count"] += 1
                    item["loot_tier"] = max(item["loot_tier"], possible_tier)
                for component in game_object.m_Component:
                    class_name = _class_name(component)
                    if class_name == "Recycler":
                        item["recycler_count"] += 1
                    elif class_name == "CardReader":
                        level = int(component.component.read_typetree().get("accessLevel", 0))
                        color = ACCESS_LEVELS.get(level)
                        if color:
                            item["keycard_reader_counts"][color] += 1
                    elif class_name == "PuzzleReset":
                        item["puzzle_reset_count"] += 1
                    elif class_name == "TriggerSafeZone":
                        item["safe_zone"] = True
                    elif class_name == "NPCVendingMachine":
                        item["vending_machine_count"] += 1
            except Exception:
                continue

    prefabs = {}
    for path, item in sorted(facts.items()):
        card_counts = {name: int(item["keycard_reader_counts"].get(name, 0))
                       for name in ("green", "blue", "red")}
        cards = [name for name, count in card_counts.items() if count]
        has_cards = bool(cards)
        has_reset = item["puzzle_reset_count"] > 0
        if has_cards and has_reset:
            puzzle_type = "keycard_and_electrical"
        elif has_cards:
            puzzle_type = "keycard"
        elif has_reset:
            puzzle_type = "electrical"
        else:
            puzzle_type = "none"
        prefabs[path] = {
            "recycler_count": int(item["recycler_count"]),
            "keycard_requirements": cards,
            "keycard_reader_counts": card_counts,
            "puzzle_type": puzzle_type,
            "puzzle_reset_count": int(item["puzzle_reset_count"]),
            "loot_tier": int(item["loot_tier"]),
            "loot_spawner_count": int(item["loot_spawner_count"]),
            "safe_zone": bool(item["safe_zone"]),
            "vending_machine_count": int(item["vending_machine_count"]),
            "landmarks": sorted(landmarks.get(path, []), key=lambda value: (
                not value["should_display_on_map"],
                value["component_type"].casefold(),
                value["object_name"].casefold(),
                json.dumps(value["local_matrix"], separators=(",", ":")),
            )),
        }

    identity = bundle_identity(install)
    for bundle in identity.get("bundles", {}).values():
        bundle.pop("path", None)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": identity,
        "extraction": {
            "asset_scene_count": len(scenes),
            "method": "prefab component, named loot-spawner, and LandmarkInfo transform scan",
            "landmark_classes": sorted(LANDMARK_CLASSES),
            "landmark_transform_space": "prefab-root-relative Unity local-to-world matrix",
            "keycard_access_levels": ACCESS_LEVELS,
            "loot_tier_scale": {"0": "none detected", "1": "low/normal", "2": "tier 2", "3": "tier 3/elite"},
        },
        "prefab_count": len(prefabs),
        "prefabs": prefabs,
    }


def refresh_monument_metadata(rust_install_path: str | Path,
                              output_path: str | Path | None = None) -> Path:
    target = Path(output_path) if output_path is not None else Path(__file__).with_name("data") / "monument_metadata.json"
    payload = extract_monument_metadata(rust_install_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target
