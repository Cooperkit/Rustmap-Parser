"""Maintainer extraction of sanitized gameplay metadata from monument prefabs."""

from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path

import numpy as np

from .no_build_assets import _class_name, _root_game_object
from .tunnel_assets import _component, _world_matrix, bundle_identity


SCHEMA_VERSION = 5
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
RADIATION_TIERS = {
    0: ("minimal", 2.0),
    1: ("low", 10.0),
    2: ("medium", 25.0),
    3: ("high", 51.0),
    4: ("none", 0.0),
}

# These are intentionally component identities rather than object-name
# guesses.  The component scan also catches nested prefab instances after
# Unity has expanded them into the BuildPlayer monument scenes.
INTERACTABLE_CLASSES = {
    "Recycler": "recycler",
    "ResearchTable": "research_table",
    "RepairBench": "repair_bench",
    "MixingTable": "mixing_table",
    "Workbench": "workbench",
    "NPCVendingMachine": "vending_machine",
    "InvisibleVendingMachine": "vending_machine",
    "Marketplace": "marketplace",
}
PUZZLE_ACTIONS = {
    "FuseBox": ("insert_fuse", True),
    "ElectricSwitch": ("activate_switch", True),
    "TimerSwitch": ("activate_timer_switch", True),
    "WheelSwitch": ("turn_wheel", True),
    "CardReader": ("swipe_keycard", True),
    "PressButton": ("press_button", True),
    "PressButton_TrainTunnel": ("press_button", True),
    "DoorManipulator": ("door_unlocks", False),
    "SlidingProgressDoor": ("door_opens", False),
}
# AlarmSound is serialized as a generic IOEntity in the oil-rig asset scenes,
# but the dedicated server does not instantiate it. Runtime validation showed
# that retaining it adds one false server node and edge to each oil-rig alarm
# graph. The visual alarm still appears through the connected server entities.
CLIENT_ONLY_IO_OBJECT_NAMES = frozenset({"alarmsound"})


def _loot_variant(path: str) -> str | None:
    """Return a useful loot subtype from an authoritative prefab path."""
    lowered = path.casefold().replace("\\", "/")
    name = Path(lowered).name
    if name == "diesel_collectable.prefab":
        return "diesel_fuel"
    if "barrel" in name:
        return "barrel"
    if "crate" not in name:
        return None
    if "elite" in name:
        return "elite_crate"
    if "military" in name:
        return "military_crate"
    if "medical" in name:
        return "medical_crate"
    if "food" in name:
        return "food_crate"
    if "tool" in name:
        return "tool_crate"
    return "crate"


def _local_position(local_matrix: np.ndarray) -> dict:
    return {
        axis: round(float(local_matrix[index, 3]), 9)
        for index, axis in enumerate(("x", "y", "z"))
    }


def _nearest_ancestor_game_object_id(game_object, candidates: set[int]) -> int | None:
    """Find the nearest candidate GameObject in this object's parent chain."""
    current = game_object
    while current is not None:
        path_id = int(current.object_reader.path_id)
        if path_id in candidates:
            return path_id
        transform = _component(current, "Transform")
        father = getattr(transform, "m_Father", None) if transform is not None else None
        if father is None or not father.path_id:
            break
        try:
            parent_transform = father.read()
            current = parent_transform.m_GameObject.read()
        except Exception:
            break
    return None


def _radiation_definition(tree: dict, game_object, local_matrix: np.ndarray,
                          dynamic: bool) -> dict | None:
    sphere = _component(game_object, "SphereCollider")
    box = _component(game_object, "BoxCollider")
    if sphere is None and box is None:
        return None
    tier_value = int(tree.get("radiationTier", 1))
    tier_name, tier_amount = RADIATION_TIERS.get(
        tier_value, (f"unknown_{tier_value}", 0.0)
    )
    override = float(tree.get("RadiationAmountOverride", 0.0))
    result = {
        "object_name": str(game_object.m_Name),
        "local_matrix": local_matrix.tolist(),
        "tier": tier_name,
        "tier_value": tier_value,
        "radiation_amount": None if dynamic else (
            override if override > 0.0 else tier_amount
        ),
        "dynamic": bool(dynamic),
        "bypass_armor": bool(tree.get("BypassArmor", False)),
        "falloff": round(float(tree.get("falloff", 0.0)), 9),
        "increase_near_center": bool(tree.get("IncreaseDamageNearCenter", True)),
        "use_line_of_sight": bool(tree.get("UseLOSCheck", False)),
        "ignore_above_ground": bool(tree.get("IgnoreAboveGroundPlayers", False)),
        "minimum_local_height": (
            round(float(tree.get("MinLocalHeight", 0.0)), 9)
            if tree.get("ApplyLocalHeightCheck") else None
        ),
    }
    if sphere is not None:
        result.update({
            "shape": "sphere",
            "radius": round(float(sphere.m_Radius), 9),
            "scale_radius": not bool(tree.get("DontScaleRadiationSize", False)),
        })
    else:
        result.update({
            "shape": "box",
            "center": {
                axis: round(float(getattr(box.m_Center, axis)), 9)
                for axis in ("x", "y", "z")
            },
            "size": {
                axis: round(float(getattr(box.m_Size, axis)), 9)
                for axis in ("x", "y", "z")
            },
            "use_per_axis_falloff": bool(tree.get("usePerAxisFalloff", False)),
            "falloff_per_axis": {
                axis: round(float(tree.get("falloffPerAxis", {}).get(axis, 0.0)), 9)
                for axis in ("x", "y", "z")
            },
        })
    return result


def _ptr_path_id(value) -> int:
    """Return a Unity PPtr path id from a typetree value."""
    if not isinstance(value, dict):
        return 0
    if "m_PathID" in value:
        return int(value.get("m_PathID") or 0)
    io_ent = value.get("ioEnt")
    return _ptr_path_id(io_ent)


def _local_component_context(scene, tree: dict, transform_memo: dict,
                             context_cache: dict[int, tuple]) -> tuple | None:
    game_object_id = _ptr_path_id(tree.get("m_GameObject"))
    if not game_object_id:
        return None
    if game_object_id in context_cache:
        return context_cache[game_object_id]
    try:
        game_object = scene.objects[game_object_id].read()
        root = _root_game_object(game_object)
        path = root.m_Name.casefold().replace("\\", "/")
        if not path.startswith(LANDMARK_PREFIXES):
            context_cache[game_object_id] = None
            return None
        root_transform = _component(root, "Transform")
        child_transform = _component(game_object, "Transform")
        if root_transform is None or child_transform is None:
            context_cache[game_object_id] = None
            return None
        local_matrix = (
            np.linalg.inv(_world_matrix(root_transform, transform_memo)) @
            _world_matrix(child_transform, transform_memo)
        )
        result = (path, game_object, np.round(local_matrix, 12))
        context_cache[game_object_id] = result
        return result
    except Exception:
        context_cache[game_object_id] = None
        return None


def _bounds_definition(tree: dict) -> dict | None:
    value = tree.get("Bounds")
    if not isinstance(value, dict):
        return None
    center, extent = value.get("m_Center"), value.get("m_Extent")
    if not isinstance(center, dict) or not isinstance(extent, dict):
        return None
    return {
        "center": {axis: round(float(center.get(axis, 0.0)), 9)
                   for axis in ("x", "y", "z")},
        "extents": {axis: round(float(extent.get(axis, 0.0)), 9)
                    for axis in ("x", "y", "z")},
    }


def _interactable_definition(class_name: str, tree: dict, game_object,
                             local_matrix: np.ndarray) -> dict | None:
    if class_name == "BaseOven":
        # BaseOven.IndustrialSlotMode.OilRefinery is enum value 2 in the
        # authoritative server assembly. Static monument refinery wrappers in
        # BuildPlayer scenes retain the Furnace default (0), so their prefab
        # object identity is also required; this avoids treating furnaces,
        # BBQs, and hobo barrels as refineries.
        object_name = str(game_object.m_Name).casefold()
        if (int(tree.get("IndustrialMode", -1)) != 2 and
                "refinery" not in object_name):
            return None
        kind = "oil_refinery"
    else:
        kind = INTERACTABLE_CLASSES.get(class_name)
    if kind is None:
        return None
    properties = {}
    if class_name == "Workbench":
        properties["level"] = int(tree.get("Workbenchlevel", 0))
    return {
        "type": kind,
        "component_type": class_name,
        "object_name": str(game_object.m_Name),
        "local_matrix": local_matrix.tolist(),
        "properties": properties,
    }


def _node_definition(obj, class_name: str, tree: dict, game_object,
                     local_matrix: np.ndarray) -> dict:
    action = PUZZLE_ACTIONS.get(class_name)
    properties = {}
    if class_name == "CardReader":
        level = int(tree.get("accessLevel", 0))
        properties["access_level"] = level
        properties["keycard"] = ACCESS_LEVELS.get(level)
    outputs = []
    for index, slot in enumerate(tree.get("outputs", [])):
        target = _ptr_path_id(slot.get("connectedTo"))
        if not target:
            continue
        outputs.append({
            "target_path_id": target,
            "output_slot": index,
            "output_name": str(slot.get("niceName") or ""),
            "input_slot": int(slot.get("connectedToSlot", 0)),
        })
    return {
        "path_id": int(obj.path_id),
        "game_object_path_id": _ptr_path_id(tree.get("m_GameObject")),
        "component_type": class_name,
        "object_name": str(game_object.m_Name),
        "local_matrix": local_matrix.tolist(),
        "action": action[0] if action else None,
        "player_action": bool(action[1]) if action else False,
        "properties": properties,
        "outputs": outputs,
        "input_names": [str(slot.get("niceName") or "")
                        for slot in tree.get("inputs", [])],
    }


def _puzzle_roots(reset: dict, nodes: dict[int, dict],
                  game_object_io: dict[int, list[int]]) -> list[int]:
    roots = list(game_object_io.get(reset["game_object_path_id"], ()))
    roots.extend(reset["reset_entity_path_ids"])
    if not roots and reset["reset_positions"]:
        reset_matrix = np.asarray(reset["local_matrix"], dtype=np.float64)
        node_positions = {
            path_id: np.asarray(node["local_matrix"], dtype=np.float64)[:3, 3]
            for path_id, node in nodes.items()
        }
        for position in reset["reset_positions"]:
            local = np.asarray([position["x"], position["y"], position["z"], 1.0])
            target = (reset_matrix @ local)[:3]
            nearby = [(float(np.linalg.norm(point - target)), path_id)
                      for path_id, point in node_positions.items()]
            if nearby:
                distance, path_id = min(nearby)
                if distance <= 0.75:
                    roots.append(path_id)
    return sorted({path_id for path_id in roots if path_id in nodes})


def _build_puzzle(reset: dict, all_nodes: dict[int, dict], index: int) -> dict:
    roots = _puzzle_roots(reset, all_nodes, reset["game_object_io"])
    depth = {path_id: 0 for path_id in roots}
    queue = deque(roots)
    while queue:
        source = queue.popleft()
        for edge in all_nodes[source]["outputs"]:
            target = edge["target_path_id"]
            if target in all_nodes and target not in depth:
                depth[target] = depth[source] + 1
                queue.append(target)

    reachable = set(depth)
    ordered_paths = sorted(reachable, key=lambda path_id: (
        depth[path_id], all_nodes[path_id]["component_type"].casefold(),
        all_nodes[path_id]["object_name"].casefold(), path_id,
    ))
    id_map = {path_id: f"node-{number:03d}"
              for number, path_id in enumerate(ordered_paths, 1)}
    nodes = []
    for path_id in ordered_paths:
        source = all_nodes[path_id]
        nodes.append({key: source[key] for key in (
            "component_type", "object_name", "local_matrix", "action",
            "player_action", "properties"
        )} | {"id": id_map[path_id], "depth": depth[path_id]})
    edges = []
    for source_id in ordered_paths:
        for edge in all_nodes[source_id]["outputs"]:
            target_id = edge["target_path_id"]
            if target_id not in reachable:
                continue
            input_names = all_nodes[target_id]["input_names"]
            input_slot = edge["input_slot"]
            edges.append({
                "from": id_map[source_id], "to": id_map[target_id],
                "output_slot": edge["output_slot"],
                "output_name": edge["output_name"],
                "input_slot": input_slot,
                "input_name": input_names[input_slot] if 0 <= input_slot < len(input_names) else "",
            })
    edges.sort(key=lambda edge: (
        edge["from"], edge["output_slot"], edge["to"], edge["input_slot"]
    ))

    action_depths = sorted({depth[path_id] for path_id in ordered_paths
                            if all_nodes[path_id]["action"]})
    order_for_depth = {value: number for number, value in enumerate(action_depths, 1)}
    ordered_steps = []
    branches = Counter()
    for path_id in ordered_paths:
        node = all_nodes[path_id]
        if not node["action"]:
            continue
        order = order_for_depth[depth[path_id]]
        branch = branches[order]
        branches[order] += 1
        ordered_steps.append({
            "order": order, "branch": branch, "node_id": id_map[path_id],
            "action": node["action"], "player_action": node["player_action"],
            "component_type": node["component_type"],
            "object_name": node["object_name"],
            "local_matrix": node["local_matrix"],
            "properties": node["properties"],
        })
    return {
        "id": f"puzzle-{index:03d}",
        "reset": {
            "component_type": "PuzzleReset",
            "object_name": reset["object_name"],
            "local_matrix": reset["local_matrix"],
        },
        "root_node_ids": [id_map[path_id] for path_id in roots if path_id in id_map],
        "nodes": nodes,
        "edges": edges,
        "ordered_steps": ordered_steps,
        "ordering": (
            "Breadth-first directed IOEntity.outputs traversal from the roots "
            "used by PuzzleReset; equal order values are parallel branches"
        ),
    }


def extract_detailed_monument_data_from_scenes(
        scenes, guid_paths: dict[str, str] | None = None) -> dict[str, dict]:
    """Extract exact child transforms and directed puzzle graphs efficiently."""
    guid_paths = guid_paths or {}
    details: dict[str, dict] = defaultdict(lambda: {
        "bounds": None, "interactables": [], "puzzles": [],
        "loot_spawn_groups": [], "radiation_zones": [],
    })
    for scene in scenes:
        representatives = {}
        for obj in scene.objects.values():
            if obj.type.name == "MonoBehaviour":
                representatives.setdefault(obj.serialized_type.script_type_index, obj)

        type_names = {}
        io_type_indices = set()
        selected_indices = set()
        for type_index, obj in representatives.items():
            try:
                class_name = obj.read().m_Script.read().m_ClassName
                type_names[type_index] = class_name
                keys = obj.read_typetree().keys()
                if "inputs" in keys and "outputs" in keys:
                    io_type_indices.add(type_index)
                if (class_name in LANDMARK_CLASSES or class_name == "PuzzleReset" or
                        class_name == "BaseOven" or
                        class_name in INTERACTABLE_CLASSES or type_index in io_type_indices):
                    selected_indices.add(type_index)
                if (class_name in {"SpawnGroup", "TriggerRadiation",
                                   "RadiationSphere"} or
                        class_name.endswith("SpawnPoint")):
                    selected_indices.add(type_index)
            except Exception:
                continue

        transform_memo: dict[int, np.ndarray] = {}
        context_cache = {}
        nodes_by_prefab: dict[str, dict[int, dict]] = defaultdict(dict)
        game_object_io_by_prefab: dict[str, dict[int, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )
        resets_by_prefab: dict[str, list[dict]] = defaultdict(list)
        spawn_groups_by_prefab: dict[str, dict[int, dict]] = defaultdict(dict)
        spawn_points_by_prefab: dict[str, list[dict]] = defaultdict(list)
        dynamic_radiation_roots: dict[str, set[int]] = defaultdict(set)
        radiation_triggers_by_prefab: dict[str, list[dict]] = defaultdict(list)
        for obj in scene.objects.values():
            if (obj.type.name != "MonoBehaviour" or
                    obj.serialized_type.script_type_index not in selected_indices):
                continue
            try:
                tree = obj.read_typetree()
                context = _local_component_context(
                    scene, tree, transform_memo, context_cache
                )
                if context is None:
                    continue
                path, game_object, local_matrix = context
                class_name = type_names[obj.serialized_type.script_type_index]
                item = details[path]
                if class_name == "MonumentInfo":
                    item["bounds"] = _bounds_definition(tree)
                game_object_path_id = _ptr_path_id(tree.get("m_GameObject"))
                active = bool(getattr(game_object, "m_IsActive", True))
                enabled = bool(tree.get("m_Enabled", True))
                if (class_name == "SpawnGroup" and active and enabled and
                        bool(tree.get("isSpawnerActive", True))):
                    spawn_groups_by_prefab[path][game_object_path_id] = {
                        "object_name": str(game_object.m_Name),
                        "tree": tree,
                        "game_object": game_object,
                        "spawn_points": [],
                    }
                elif (class_name.endswith("SpawnPoint") and active and enabled):
                    spawn_points_by_prefab[path].append({
                        "game_object": game_object,
                        "component_type": class_name,
                        "local_position": _local_position(local_matrix),
                        "radius": (
                            round(float(tree.get("radius", 0.0)), 9)
                            if class_name == "RadialSpawnPoint" else 0.0
                        ),
                    })
                elif class_name == "RadiationSphere" and active and enabled:
                    dynamic_radiation_roots[path].add(game_object_path_id)
                elif class_name == "TriggerRadiation" and active and enabled:
                    radiation_triggers_by_prefab[path].append({
                        "tree": tree,
                        "game_object": game_object,
                        "local_matrix": local_matrix,
                    })
                definition = _interactable_definition(
                    class_name, tree, game_object, local_matrix
                )
                if definition is not None:
                    item["interactables"].append(definition)
                if (obj.serialized_type.script_type_index in io_type_indices and
                        str(game_object.m_Name).casefold() not in
                        CLIENT_ONLY_IO_OBJECT_NAMES):
                    node = _node_definition(
                        obj, class_name, tree, game_object, local_matrix
                    )
                    nodes_by_prefab[path][node["path_id"]] = node
                    game_object_io_by_prefab[path][node["game_object_path_id"]].append(
                        node["path_id"]
                    )
                if class_name == "PuzzleReset":
                    positions = tree.get("resetPositions", [])
                    resets_by_prefab[path].append({
                        "object_name": str(game_object.m_Name),
                        "game_object_path_id": _ptr_path_id(tree.get("m_GameObject")),
                        "local_matrix": local_matrix.tolist(),
                        "reset_entity_path_ids": [
                            _ptr_path_id(value) for value in tree.get("resetEnts", [])
                            if _ptr_path_id(value)
                        ],
                        "reset_positions": [
                            {axis: float(value.get(axis, 0.0))
                             for axis in ("x", "y", "z")}
                            for value in positions if isinstance(value, dict)
                        ],
                    })
            except Exception:
                continue

        for path, resets in resets_by_prefab.items():
            nodes = nodes_by_prefab.get(path, {})
            game_object_io = game_object_io_by_prefab.get(path, {})
            puzzles = []
            for number, reset in enumerate(sorted(resets, key=lambda value: (
                value["object_name"].casefold(),
                json.dumps(value["local_matrix"], separators=(",", ":")),
            )), 1):
                reset["game_object_io"] = game_object_io
                puzzles.append(_build_puzzle(reset, nodes, number))
            details[path]["puzzles"] = puzzles

        for path, points in spawn_points_by_prefab.items():
            groups = spawn_groups_by_prefab.get(path, {})
            group_ids = set(groups)
            for point in points:
                group_id = _nearest_ancestor_game_object_id(
                    point["game_object"], group_ids
                )
                if group_id is not None:
                    groups[group_id]["spawn_points"].append({
                        key: point[key]
                        for key in ("component_type", "local_position", "radius")
                    })

        for path, groups in spawn_groups_by_prefab.items():
            for group in groups.values():
                tree = group["tree"]
                variants = []
                for entry in tree.get("prefabs", []):
                    reference = entry.get("prefab", {})
                    guid = str(reference.get("guid") or "")
                    prefab_path = str(guid_paths.get(guid) or "")
                    variants.append({
                        "type": _loot_variant(prefab_path),
                        "prefab_path": prefab_path or None,
                        "guid": guid or None,
                        "weight": int(entry.get("weight", 0)),
                    })
                loot_types = [value["type"] for value in variants if value["type"]]
                if not loot_types or not group["spawn_points"]:
                    continue
                crate_types = {
                    "crate", "elite_crate", "military_crate", "medical_crate",
                    "food_crate", "tool_crate",
                }
                if all(value == "barrel" for value in loot_types):
                    kind = "barrel"
                elif all(value in crate_types for value in loot_types):
                    kind = "crate"
                elif all(value == "diesel_fuel" for value in loot_types):
                    kind = "diesel_fuel"
                else:
                    kind = "mixed"
                points = sorted(group["spawn_points"], key=lambda value: (
                    value["local_position"]["x"], value["local_position"]["y"],
                    value["local_position"]["z"], value["component_type"],
                ))
                details[path]["loot_spawn_groups"].append({
                    "kind": kind,
                    "object_name": group["object_name"],
                    "max_population": int(tree.get("maxPopulation", 0)),
                    "spawn_per_tick_min": int(tree.get("numToSpawnPerTickMin", 0)),
                    "spawn_per_tick_max": int(tree.get("numToSpawnPerTickMax", 0)),
                    "respawn_seconds_min": round(
                        float(tree.get("respawnDelayMin", 0.0)), 9
                    ),
                    "respawn_seconds_max": round(
                        float(tree.get("respawnDelayMax", 0.0)), 9
                    ),
                    "wants_initial_spawn": bool(tree.get("wantsInitialSpawn", True)),
                    "prevent_duplicates": bool(tree.get("preventDuplicates", False)),
                    "variants": variants,
                    "spawn_points": points,
                })

        for path, triggers in radiation_triggers_by_prefab.items():
            dynamic_roots = dynamic_radiation_roots.get(path, set())
            for trigger in triggers:
                dynamic = _nearest_ancestor_game_object_id(
                    trigger["game_object"], dynamic_roots
                ) is not None
                definition = _radiation_definition(
                    trigger["tree"], trigger["game_object"],
                    trigger["local_matrix"], dynamic,
                )
                if definition is not None:
                    details[path]["radiation_zones"].append(definition)

    for item in details.values():
        item["interactables"].sort(key=lambda value: (
            value["type"], value["component_type"].casefold(),
            value["object_name"].casefold(),
            json.dumps(value["local_matrix"], separators=(",", ":")),
        ))
        item["loot_spawn_groups"].sort(key=lambda value: (
            value["kind"], value["object_name"].casefold(),
            json.dumps(value["spawn_points"], separators=(",", ":")),
        ))
        item["radiation_zones"].sort(key=lambda value: (
            value["shape"], value["tier"], value["object_name"].casefold(),
            json.dumps(value["local_matrix"], separators=(",", ":")),
        ))
    return dict(details)


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


def _load_monument_scenes(rust_install_path: str | Path):
    import UnityPy

    install = Path(rust_install_path)
    environment = UnityPy.load(
        str(install / "Bundles" / "shared" / "assetscenes.bundle"),
        str(install / "Bundles" / "shared" / "content.bundle"),
    )
    scenes = []
    for root in environment.files.values():
        for name, asset_file in (getattr(root, "files", None) or {}).items():
            if (name.startswith("BuildPlayer-AssetScene-monument.") and
                    not name.endswith("sharedAssets")):
                scenes.append(asset_file)
    return install, environment, scenes


def _sanitized_bundle_identity(install: Path) -> dict:
    identity = bundle_identity(install)
    for bundle in identity.get("bundles", {}).values():
        bundle.pop("path", None)
    return identity


def _installed_guid_paths(rust_install_path: str | Path) -> dict[str, str]:
    from .prefabs import extract_game_manifest, find_content_bundle
    manifest = extract_game_manifest(find_content_bundle(rust_install_path))
    return {
        entry.guid: entry.path
        for entry in manifest.entries.values()
        if entry.guid
    }


def extract_monument_metadata(rust_install_path: str | Path) -> dict:
    """Extract compact gameplay facts; UnityPy is a maintainer-only dependency."""
    guid_paths = _installed_guid_paths(rust_install_path)
    install, _environment, scenes = _load_monument_scenes(rust_install_path)
    detailed = extract_detailed_monument_data_from_scenes(
        scenes, guid_paths
    )

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
    for path in detailed:
        facts[path]
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
            "bounds": detailed.get(path, {}).get("bounds"),
            "interactables": detailed.get(path, {}).get("interactables", []),
            "puzzles": detailed.get(path, {}).get("puzzles", []),
            "loot_spawn_groups": detailed.get(path, {}).get("loot_spawn_groups", []),
            "radiation_zones": detailed.get(path, {}).get("radiation_zones", []),
        }

    identity = _sanitized_bundle_identity(install)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": identity,
        "extraction": {
            "asset_scene_count": len(scenes),
            "method": "prefab component, named loot-spawner, and LandmarkInfo transform scan",
            "landmark_classes": sorted(LANDMARK_CLASSES),
            "landmark_transform_space": "prefab-root-relative Unity local-to-world matrix",
            "detail_transform_space": "prefab-root-relative Unity local-to-world matrix",
            "puzzle_order_source": "directed IOEntity.outputs graph rooted by PuzzleReset",
            "loot_source": "SpawnGroup prefab weights and child BaseSpawnPoint transforms",
            "radiation_source": "TriggerRadiation settings and collider geometry",
            "keycard_access_levels": ACCESS_LEVELS,
            "radiation_tiers": {
                str(key): {"name": value[0], "amount": value[1]}
                for key, value in RADIATION_TIERS.items()
            },
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


def refresh_monument_details(rust_install_path: str | Path,
                             metadata_path: str | Path | None = None, *,
                             source_build_id: str | None = None) -> Path:
    """Enrich an existing aggregate database with fast exact-detail extraction.

    This is useful when aggregate facts were generated from a full client
    bundle but exact transforms need refreshing from the equivalent dedicated
    server build. Existing aggregate fields are retained.
    """
    target = (Path(metadata_path) if metadata_path is not None else
              Path(__file__).with_name("data") / "monument_metadata.json")
    payload = json.loads(target.read_text(encoding="utf-8"))
    guid_paths = _installed_guid_paths(rust_install_path)
    install, _environment, scenes = _load_monument_scenes(rust_install_path)
    detailed = extract_detailed_monument_data_from_scenes(
        scenes, guid_paths
    )
    for path, values in detailed.items():
        item = payload.setdefault("prefabs", {}).setdefault(path, {
            "recycler_count": 0, "keycard_requirements": [],
            "keycard_reader_counts": {"green": 0, "blue": 0, "red": 0},
            "puzzle_type": "none", "puzzle_reset_count": 0,
            "loot_tier": 0, "loot_spawner_count": 0, "safe_zone": False,
            "vending_machine_count": 0, "landmarks": [],
            "loot_spawn_groups": [], "radiation_zones": [],
        })
        item.update(values)
    payload["schema_version"] = SCHEMA_VERSION
    payload["prefab_count"] = len(payload.get("prefabs", {}))
    details_source = _sanitized_bundle_identity(install)
    if source_build_id is not None:
        details_source["rust_build_id"] = str(source_build_id)
    payload["details_source"] = details_source
    payload["details_extraction"] = {
        "asset_scene_count": len(scenes),
        "method": (
            "indexed interactable, SpawnGroup/BaseSpawnPoint, TriggerRadiation "
            "collider, and directed IO graph scan"
        ),
        "transform_space": "prefab-root-relative Unity local-to-world matrix",
        "interactable_component_types": sorted(
            [*INTERACTABLE_CLASSES, "BaseOven(IndustrialSlotMode.OilRefinery)"]
        ),
        "puzzle_order_source": "PuzzleReset roots followed through IOEntity.outputs",
        "loot_source": "SpawnGroup prefab weights and child BaseSpawnPoint transforms",
        "radiation_source": "TriggerRadiation settings and collider geometry",
    }
    target.write_text(json.dumps(payload, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return target
