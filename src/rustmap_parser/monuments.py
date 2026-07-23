"""Export placed gameplay monuments from a parsed Rust world map."""

from __future__ import annotations

import json
import math
from collections import Counter
from functools import lru_cache
from importlib import resources
from pathlib import Path

import numpy as np

from .config import TransformOptions
from .no_build import _decompose
from .prefabs import PrefabManifest
from .tunnels import _instance_matrix
from .transforms import exported_position_fields, strip_disabled_position_fields


MONUMENT_PATH_PREFIX = "assets/bundled/prefabs/autospawn/monument/"
TRAIN_TUNNEL_ENTRANCE_PREFIX = "assets/bundled/prefabs/autospawn/tunnel-entrance/"
TRAIN_TUNNEL_LINK_PREFIX = "assets/bundled/prefabs/autospawn/tunnel-upwards/"
MONUMENT_PATH_PREFIXES = (
    MONUMENT_PATH_PREFIX,
    TRAIN_TUNNEL_ENTRANCE_PREFIX,
    TRAIN_TUNNEL_LINK_PREFIX,
)
LOOT_POSITION_FIELDS = [
    "local_x", "local_y", "local_z",
    "world_x", "world_y", "world_z",
    "map_x", "map_y", "radius",
]
UNIQUE_ENVIRONMENT_PREFIX = "assets/bundled/prefabs/autospawn/unique_environment/"
SIZE_CLASSES = {"tiny", "small", "medium", "large", "xlarge"}
GROUP_SIZE_CLASSES = {
    "arctic_bases": "large",
    "fishing_village": "small",
    "harbor": "large",
    "ice_lakes": "small",
    "jungle_ruins": "small",
    "lighthouse": "small",
    "military_bases": "medium",
    "roadside": "small",
    "swamp": "small",
    "underwater_lab": "large",
}
SAFE_ZONE_NAMES = {
    "bandit_town", "compound", "fishing_village_a", "fishing_village_b",
    "fishing_village_c", "stables_a", "stables_b",
}
DISPLAY_NAMES = {
    "airfield_1": "Airfield", "apartments_complex_1": "Abandoned Military Base",
    "arctic_research_base_a": "Arctic Research Base", "bandit_town": "Bandit Camp",
    "compound": "Outpost", "excavator_1": "Giant Excavator Pit",
    "ferry_terminal_1": "Ferry Terminal", "junkyard_1": "Junkyard",
    "launch_site_1": "Launch Site", "military_tunnel_1": "Military Tunnels",
    "nuclear_missile_silo": "Nuclear Missile Silo", "oilrig_1": "Small Oil Rig",
    "oilrig_2": "Large Oil Rig", "powerplant_1": "Power Plant",
    "radtown_1": "Mining Outpost", "radtown_small_3": "Sewer Branch",
    "satellite_dish": "Satellite Dish Array", "sphere_tank": "The Dome",
    "trainyard_1": "Train Yard", "underwater_lab_a": "Underwater Lab",
    "water_treatment_plant_1": "Water Treatment Plant",
}


def _humanize(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").strip().title()


def _family(name: str) -> str:
    for suffix in ("_easy", "_medium", "_hard", "_a", "_b", "_c", "_d", "_e", "_1", "_2", "_3", "_4"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def _size_class(group: str, name: str) -> str:
    if group in SIZE_CLASSES:
        return group
    if group == "cave":
        for size in ("small", "medium", "large"):
            if name.startswith(f"cave_{size}_"):
                return size
    if group == "offshore":
        return "small" if name == "oilrig_1" else "large"
    if group == "jungle_ruins" and "ziggurat" in name:
        return "medium"
    return GROUP_SIZE_CLASSES.get(group, "medium")


@lru_cache(maxsize=1)
def _gameplay_database() -> dict:
    resource = resources.files("rustmap_parser.data").joinpath("monument_metadata.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def monument_metadata(path: str) -> dict:
    """Return deterministic path-derived metadata without inventing asset facts."""
    folded_path = path.casefold()
    if folded_path.startswith(MONUMENT_PATH_PREFIX):
        path_kind = "gameplay_monument"
        relative = folded_path[len(MONUMENT_PATH_PREFIX):]
    elif folded_path.startswith(TRAIN_TUNNEL_ENTRANCE_PREFIX):
        path_kind = "train_tunnel_entrance"
        relative = folded_path[len(TRAIN_TUNNEL_ENTRANCE_PREFIX):]
    elif folded_path.startswith(TRAIN_TUNNEL_LINK_PREFIX):
        path_kind = "train_tunnel_link"
        relative = folded_path[len(TRAIN_TUNNEL_LINK_PREFIX):]
    else:
        raise ValueError(f"Unsupported monument prefab path: {path}")
    parts = relative.removesuffix(".prefab").split("/")
    group, name = parts[0], parts[-1]
    database = _gameplay_database()
    extracted = database.get("prefabs", {}).get(path.casefold())
    safe_zone = bool(extracted["safe_zone"]) if extracted is not None else name in SAFE_ZONE_NAMES
    size_class = (
        "medium" if path_kind == "train_tunnel_entrance"
        else "small" if path_kind == "train_tunnel_link"
        else _size_class(group, name)
    )
    if path_kind == "train_tunnel_entrance":
        kind, environment = "train_tunnel_entrance", "surface_to_underground"
    elif path_kind == "train_tunnel_link":
        kind, environment = "train_tunnel_link", "surface_to_underground"
    elif group == "cave":
        kind, environment = "cave", "underground"
    elif group == "underwater_lab":
        kind, environment = "underwater_monument", "underwater"
    elif group == "offshore":
        kind, environment = "offshore_monument", "offshore"
    elif group in {"ice_lakes", "swamp"}:
        kind, environment = "natural_feature", "surface"
    elif group == "tiny" and name.startswith("water_well"):
        kind, environment = "resource_location", "surface"
    elif name.startswith("mining_quarry") or name == "excavator_1":
        kind, environment = "resource_monument", "surface"
    elif safe_zone:
        kind, environment = "safe_zone", "surface"
    elif group in {"harbor", "fishing_village", "lighthouse"}:
        kind, environment = "coastal_monument", "coastal"
    elif group == "roadside":
        kind, environment = "roadside_monument", "surface"
    else:
        kind, environment = "monument", "surface"

    tags = [kind, environment]
    if safe_zone:
        tags.append("safe_zone")
    tags.append(f"size_{size_class}")
    if extracted is None:
        gameplay = {
            "safe_zone": safe_zone,
            "recycler_count": 0,
            "keycard_requirements": [],
            "puzzle_type": "none",
            "loot_tier": 0,
            "maximum_radiation": 0.0,
        }
    else:
        radiation_amounts = [
            float(zone["radiation_amount"])
            for zone in extracted.get("radiation_zones", [])
            if zone.get("radiation_amount") is not None
        ]
        gameplay = {
            "safe_zone": bool(extracted["safe_zone"]),
            "recycler_count": int(extracted["recycler_count"]),
            "keycard_requirements": list(extracted["keycard_requirements"]),
            "puzzle_type": str(extracted["puzzle_type"]),
            "loot_tier": int(extracted["loot_tier"]),
            "maximum_radiation": max(radiation_amounts, default=0.0),
        }
    if path_kind == "train_tunnel_entrance":
        display_name = f"Train Tunnel {_humanize(name)}"
        family = "train_tunnel_entrance"
    elif path_kind == "train_tunnel_link":
        display_name = f"Train Tunnel Link {_humanize(name)}"
        family = "train_tunnel_link"
    else:
        display_name = DISPLAY_NAMES.get(name, _humanize(name))
        family = _family(name)
    return {
        "display_name": display_name,
        "family": family,
        "classification": {
            "kind": kind,
            "environment": environment,
            "spawn_group": group,
            "size_class": size_class,
        },
        "gameplay": gameplay,
        "tags": sorted(set(tags)),
    }


def _custom_monument_metadata(path: str) -> dict:
    name = Path(path).stem
    return {
        "display_name": _humanize(name),
        "family": _family(name.casefold()),
        "classification": {
            "kind": "custom_monument",
            "environment": "surface",
            "spawn_group": "custom",
            "size_class": "medium",
        },
        "gameplay": {
            "safe_zone": False,
            "recycler_count": 0,
            "keycard_requirements": [],
            "puzzle_type": "none",
            "loot_tier": 0,
            "maximum_radiation": 0.0,
        },
        "tags": ["custom_monument", "size_medium", "surface"],
    }


def _landmark_metadata(path: str, landmark: dict | None) -> dict:
    try:
        metadata = monument_metadata(path)
    except ValueError:
        metadata = _custom_monument_metadata(path)
    if landmark is None:
        return metadata
    token = str(landmark.get("display_token") or "").casefold()
    component_type = str(landmark.get("component_type") or "")
    if token == "train_tunnel_link_display_name":
        kind, family, display_name = "train_tunnel_link", "train_tunnel_link", "Train Tunnel Link"
    elif token == "train_tunnel_display_name" or component_type == "DungeonGridInfo":
        kind, family, display_name = "train_tunnel_entrance", "train_tunnel_entrance", "Train Tunnel"
    else:
        return metadata
    metadata.update({
        "display_name": display_name,
        "family": family,
        "classification": {
            "kind": kind,
            "environment": "surface_to_underground",
            "spawn_group": family,
            "size_class": "small",
        },
        "gameplay": {
            "safe_zone": False,
            "recycler_count": 0,
            "keycard_requirements": [],
            "puzzle_type": "none",
            "loot_tier": 0,
        },
        "tags": ["size_small", "surface_to_underground", kind],
    })
    return metadata


def _is_monument_candidate(path: str, prefab, detailed: bool) -> bool:
    folded = path.casefold()
    if folded.startswith(MONUMENT_PATH_PREFIXES):
        return True
    return bool(
        detailed and prefab.category.casefold() == "monument" and
        not folded.startswith(UNIQUE_ENVIRONMENT_PREFIX)
    )


def _visible_landmarks(path: str) -> tuple[list[dict | None], bool]:
    definition = _gameplay_database().get("prefabs", {}).get(path.casefold())
    if path.casefold().startswith(MONUMENT_PATH_PREFIX):
        # monuments.json intentionally includes every gameplay monument, even
        # ones Rust+ hides. Preserve its root entry and append the visible
        # DungeonGridInfo child markers Rust+ uses for train-tunnel entrances.
        landmarks = definition.get("landmarks", []) if definition else []
        entrances = [
            item for item in landmarks
            if item.get("should_display_on_map") and (
                item.get("component_type") == "DungeonGridInfo" or
                str(item.get("display_token") or "").casefold() ==
                "train_tunnel_display_name"
            )
        ]
        return [None, *entrances], False
    if definition is None or "landmarks" not in definition:
        return [None], True
    visible = [item for item in definition["landmarks"]
               if item.get("should_display_on_map")]
    return (visible, False) if visible else ([None], True)


def _placed_transform(local_matrix, instance_matrix: np.ndarray,
                      world_size: int) -> dict:
    local = np.asarray(local_matrix, dtype=np.float64)
    world_matrix = instance_matrix @ local
    transform = _decompose(world_matrix)
    local_transform = _decompose(local)
    position = transform["position"]
    return {
        "local_position": local_transform["position"],
        "position": position,
        "map_position": {
            "x": round(float(position["x"]) + float(world_size) / 2.0, 9),
            "y": round(float(position["z"]) + float(world_size) / 2.0, 9),
        },
        "heading_degrees": transform["rotation_euler"]["y"],
    }


def _place_asset_interactable(definition: dict, instance_matrix: np.ndarray,
                              world_size: int, source_path: str) -> dict:
    result = {
        key: value for key, value in definition.items() if key != "local_matrix"
    }
    result.update(_placed_transform(
        definition["local_matrix"], instance_matrix, world_size
    ))
    result.update({
        "source": "prefab_asset",
        "source_prefab_path": source_path,
    })
    return result


def _packed_loot_position(local_position: dict, instance_matrix: np.ndarray,
                          world_size: int, radius: float = 0.0) -> list[float]:
    local = np.asarray([
        float(local_position["x"]), float(local_position["y"]),
        float(local_position["z"]), 1.0,
    ], dtype=np.float64)
    world = instance_matrix @ local
    return [
        round(float(local[0]), 9), round(float(local[1]), 9),
        round(float(local[2]), 9), round(float(world[0]), 9),
        round(float(world[1]), 9), round(float(world[2]), 9),
        round(float(world[0]) + float(world_size) / 2.0, 9),
        round(float(world[2]) + float(world_size) / 2.0, 9),
        round(float(radius), 9),
    ]


def _loot_summary(groups: list[dict]) -> dict:
    return {
        "barrel_spawn_point_count": sum(
            int(group["position_count"]) for group in groups
            if group["kind"] == "barrel"
        ),
        "crate_spawn_point_count": sum(
            int(group["position_count"]) for group in groups
            if group["kind"] == "crate"
        ),
        "barrel_max_population": sum(
            int(group["max_population"]) for group in groups
            if group["kind"] == "barrel"
        ),
        "crate_max_population": sum(
            int(group["max_population"]) for group in groups
            if group["kind"] == "crate"
        ),
    }


def _place_loot(definitions: list[dict], instance_matrix: np.ndarray,
                world_size: int) -> dict:
    positions = []
    groups = []
    for number, definition in enumerate(definitions, start=1):
        start = len(positions) // len(LOOT_POSITION_FIELDS)
        for point in definition.get("spawn_points", []):
            positions.extend(_packed_loot_position(
                point["local_position"], instance_matrix, world_size,
                float(point.get("radius", 0.0)),
            ))
        variants = [
            [value.get("type"), value.get("prefab_path"), int(value.get("weight", 0))]
            for value in definition.get("variants", [])
        ]
        groups.append({
            "id": f"loot-group-{number:03d}",
            "kind": definition["kind"],
            "object_name": definition["object_name"],
            "source": "prefab_asset",
            "max_population": int(definition.get("max_population", 0)),
            "spawn_per_tick": [
                int(definition.get("spawn_per_tick_min", 0)),
                int(definition.get("spawn_per_tick_max", 0)),
            ],
            "respawn_seconds": [
                float(definition.get("respawn_seconds_min", 0.0)),
                float(definition.get("respawn_seconds_max", 0.0)),
            ],
            "wants_initial_spawn": bool(
                definition.get("wants_initial_spawn", True)
            ),
            "prevent_duplicates": bool(definition.get("prevent_duplicates", False)),
            "variant_fields": ["type", "prefab_path", "weight"],
            "variants": variants,
            "position_start": start,
            "position_count": len(definition.get("spawn_points", [])),
        })
    return {
        "position_format": "flat_array",
        "position_fields": list(LOOT_POSITION_FIELDS),
        "position_stride": len(LOOT_POSITION_FIELDS),
        "positions": positions,
        "groups": groups,
        "summary": _loot_summary(groups),
        "count_semantics": (
            "spawn points are eligible locations; max_population is the configured "
            "concurrent capacity and actual live counts vary with spawning and looting"
        ),
    }


def _place_radiation_zone(definition: dict, instance_matrix: np.ndarray,
                          world_size: int, number: int) -> dict:
    local_matrix = np.asarray(definition["local_matrix"], dtype=np.float64)
    world_matrix = instance_matrix @ local_matrix
    if definition["shape"] == "box":
        center = definition["center"]
        collider_center = np.asarray([
            float(center["x"]), float(center["y"]), float(center["z"]), 1.0,
        ])
        local_center = local_matrix @ collider_center
        world_center = world_matrix @ collider_center
    else:
        local_center = local_matrix[:, 3]
        world_center = world_matrix[:, 3]
    transform = _decompose(world_matrix)
    result = {
        key: value for key, value in definition.items()
        if key not in {"local_matrix", "center", "size", "radius", "scale_radius"}
    }
    result.update({
        "id": f"radiation-zone-{number:03d}",
        "position": [
            round(float(local_center[0]), 9), round(float(local_center[1]), 9),
            round(float(local_center[2]), 9), round(float(world_center[0]), 9),
            round(float(world_center[1]), 9), round(float(world_center[2]), 9),
            round(float(world_center[0]) + float(world_size) / 2.0, 9),
            round(float(world_center[2]) + float(world_size) / 2.0, 9),
        ],
        "rotation_euler": transform["rotation_euler"],
        "source": "prefab_asset",
    })
    scale = np.linalg.norm(world_matrix[:3, :3], axis=0)
    if definition["shape"] == "sphere":
        radius = float(definition["radius"])
        if definition.get("scale_radius", True):
            radius *= float(scale.max())
        result["radius"] = round(radius, 9)
    else:
        size = definition["size"]
        result["size"] = {
            axis: round(float(size[axis]) * float(scale[index]), 9)
            for index, axis in enumerate(("x", "y", "z"))
        }
    return result


def _place_radiation(definitions: list[dict], instance_matrix: np.ndarray,
                     world_size: int) -> dict:
    zones = [
        _place_radiation_zone(value, instance_matrix, world_size, number)
        for number, value in enumerate(definitions, start=1)
    ]
    static_amounts = [
        float(value["radiation_amount"]) for value in zones
        if value.get("radiation_amount") is not None
    ]
    return {
        "position_fields": [
            "local_x", "local_y", "local_z",
            "world_x", "world_y", "world_z", "map_x", "map_y",
        ],
        "zone_count": len(zones),
        "max_static_radiation": max(static_amounts, default=0.0),
        "amount_semantics": (
            "server TriggerRadiation amount before clothing/armor protection; dynamic "
            "zones have null radiation_amount because runtime curves control them"
        ),
        "zones": zones,
    }


def _semantic_puzzle_step(node: dict) -> tuple[str, str, bool] | None:
    """Classify only actions a player needs in a concise walkthrough."""
    component = str(node.get("component_type") or "")
    name = str(node.get("object_name") or "")
    lowered = name.casefold()
    if (component in {"FuseBox", "ItemBasedFlowRestrictor"} and
            "fuse" in lowered):
        return "insert_fuse", "Insert a fuse", False
    if component == "ElectricSwitch":
        # Rust monument prefabs contain many switches belonging to lighting,
        # alarms, and visual feedback. They are circuitry, not puzzle steps.
        if "light" in lowered or "alarm" in lowered:
            return None
        return "turn_on_switch", "Turn on the switch", False
    if component in {"PressButton", "PressButton_TrainTunnel"}:
        return "press_button", "Press the button", False
    if component == "CardReader":
        keycard = str(node.get("properties", {}).get("keycard") or "").casefold()
        label = f"Swipe the {keycard} keycard" if keycard else "Swipe the keycard"
        return "swipe_keycard", label, False
    if component == "WheelSwitch":
        return "turn_wheel", "Turn the wheel", False
    if component in {"DoorManipulator", "SlidingProgressDoor"}:
        return "door_opens", "Door opens", True
    return None


def _graph_closure(start: object,
                   graph: dict[object, set[object]]) -> set[object]:
    found = {start}
    pending = [start]
    while pending:
        current = pending.pop()
        for other in graph.get(current, set()):
            if other not in found:
                found.add(other)
                pending.append(other)
    return found


def _local_position_key(node: dict) -> tuple:
    matrix = node.get("local_matrix") or np.eye(4)
    return (
        str(node.get("component_type") or ""),
        round(float(matrix[0][3]), 4),
        round(float(matrix[1][3]), 4),
        round(float(matrix[2][3]), 4),
    )


def _route_signature(route: dict) -> tuple:
    return tuple(
        (
            step["action"],
            _local_position_key(step),
            str(step.get("properties", {}).get("keycard") or ""),
        )
        for step in route["steps"]
    )


def _group_alternate_keycard_endings(routes: list[dict]) -> list[dict]:
    """Count one powered puzzle even when it has multiple access doors."""
    grouped = {}
    passthrough = []
    for route in routes:
        card_indices = [
            index for index, step in enumerate(route["steps"])
            if step["action"] == "swipe_keycard"
        ]
        if not card_indices:
            passthrough.append(route)
            continue
        terminal_card_index = card_indices[-1]
        prefix = route["steps"][:terminal_card_index]
        terminal_card = route["steps"][terminal_card_index]
        key = (
            _route_signature({"steps": prefix}),
            str(terminal_card.get("properties", {}).get("keycard") or ""),
        )
        grouped.setdefault(key, []).append((route, terminal_card_index))

    merged = list(passthrough)
    for members in grouped.values():
        members.sort(key=lambda value: (
            value[0]["outcome_key"], _route_signature(value[0])
        ))
        primary, common_step_count = members[0]
        primary = dict(primary)
        primary["common_step_count"] = common_step_count
        primary["alternate_endings"] = [
            route["steps"][card_index:]
            for route, card_index in members[1:]
        ]
        merged.append(primary)
    return merged


def _order_major_steps(steps: list[dict],
                       forward: dict[object, set[object]]) -> list[dict]:
    """Topologically order actions while finishing one causal branch at a time."""
    action_priority = {
        "insert_fuse": 0, "turn_on_switch": 1,
        "press_button": 2, "swipe_keycard": 3,
        "turn_wheel": 4, "door_opens": 5,
    }
    by_key = {step["_graph_key"]: step for step in steps}
    closures = {key: _graph_closure(key, forward) for key in by_key}
    predecessors = {
        key: {
            other for other in by_key
            if other != key and key in closures[other]
        }
        for key in by_key
    }
    remaining = set(by_key)
    ordered = []
    previous = None
    while remaining:
        available = [
            key for key in remaining
            if not (predecessors[key] & remaining)
        ]
        if not available:
            # Defensive fallback for a malformed/cyclic IO graph.
            available = list(remaining)
        continuing = (
            [key for key in available if key in closures[previous]]
            if previous is not None else []
        )
        choices = continuing or available
        selected = min(choices, key=lambda key: (
            by_key[key]["is_outcome"],
            action_priority[by_key[key]["action"]],
            _local_position_key(by_key[key]),
        ))
        ordered.append(by_key[selected])
        remaining.remove(selected)
        previous = selected
    return ordered


def _major_puzzle_routes(definitions: list[dict]) -> list[dict]:
    """Compress prefab IO graphs into causal player walkthrough routes.

    The complete graph remains in the packaged extraction database. Exported
    JSON intentionally contains only fuse/switch/button/card/wheel actions and
    the door outcome. A card route wins over an exit-button route controlling
    the same door.
    """
    # The same physical IO entity can occur in several PuzzleReset graphs.
    # Union those graphs by component and local position before tracing routes;
    # otherwise multi-station puzzles (notably Launch Site) become misleading
    # partial routes.
    nodes = {}
    forward = {}
    reverse = {}
    for definition in definitions:
        definition_nodes = {
            node["id"]: _local_position_key(node)
            for node in definition.get("nodes", [])
        }
        for node in definition.get("nodes", []):
            key = definition_nodes[node["id"]]
            previous = nodes.get(key)
            if previous is None or int(node.get("depth", 0)) < int(
                    previous.get("depth", 0)):
                nodes[key] = node
            forward.setdefault(key, set())
            reverse.setdefault(key, set())
        for edge in definition.get("edges", []):
            source = definition_nodes.get(edge.get("from"))
            target = definition_nodes.get(edge.get("to"))
            if source is not None and target is not None:
                forward[source].add(target)
                reverse[target].add(source)

    outcomes = [
        node_key for node_key, node in nodes.items()
        if (_semantic_puzzle_step(node) or (None, None, False))[2]
    ]
    routes = []
    for outcome_key in outcomes:
        ancestors = _graph_closure(outcome_key, reverse)
        cards = [
            node_key for node_key in ancestors
            if ((_semantic_puzzle_step(nodes[node_key]) or (None,))[0] ==
                "swipe_keycard")
        ]
        terminal_cards = [
            card for card in cards
            if not any(
                other != card and other in _graph_closure(card, forward)
                for other in cards
            )
        ]
        selected_cards = terminal_cards or [None]
        for selected_card in selected_cards:
            selected_ancestors = (
                _graph_closure(selected_card, reverse)
                if selected_card is not None else set()
            )
            selected_descendants = (
                _graph_closure(selected_card, forward)
                if selected_card is not None else set()
            )
            steps = []
            for node_key in ancestors:
                node = nodes[node_key]
                semantic = _semantic_puzzle_step(node)
                if semantic is None:
                    continue
                action, instruction, outcome = semantic
                if selected_card is not None:
                    if action == "swipe_keycard" and node_key not in selected_ancestors:
                        continue
                    if (action == "press_button" and
                            node_key not in selected_ancestors and
                            node_key not in selected_descendants):
                        # This is normally the inside exit button feeding the
                        # same door, not part of the card-entry route.
                        continue
                steps.append({
                    **node,
                    "action": action,
                    "instruction": instruction,
                    "is_outcome": outcome,
                    "_graph_key": node_key,
                })
            if not any(not step["is_outcome"] for step in steps):
                continue
            steps = _order_major_steps(steps, forward)
            routes.append({
                "outcome_key": outcome_key,
                "has_card": selected_card is not None,
                "steps": steps,
            })

    # A single graph may still expose duplicate card paths; remove exact route
    # duplicates after the semantic reduction.
    unique_routes = []
    seen = set()
    for route in routes:
        signature = (route["outcome_key"], _route_signature(route))
        if signature not in seen:
            seen.add(signature)
            unique_routes.append(route)
    routes = _group_alternate_keycard_endings(unique_routes)
    routes.sort(key=lambda route: (route["outcome_key"], _route_signature(route)))
    return routes


def _place_puzzle_route(route: dict, instance_matrix: np.ndarray,
                        world_size: int, number: int) -> dict:
    def place_steps(definitions: list[dict], start_order: int) -> list[dict]:
        placed = []
        for order, definition_step in enumerate(definitions, start=start_order):
            step = {
                "order": order,
                "action": definition_step["action"],
                "instruction": definition_step["instruction"],
                "component_type": definition_step["component_type"],
                "object_name": definition_step["object_name"],
                "properties": dict(definition_step.get("properties", {})),
            }
            step.update(_placed_transform(
                definition_step["local_matrix"], instance_matrix, world_size
            ))
            placed.append(step)
        return placed

    steps = place_steps(route["steps"], 1)
    alternate_endings = [
        {
            "id": f"alternate-ending-{index:03d}",
            "steps": place_steps(
                ending, int(route.get("common_step_count", 0)) + 1
            ),
        }
        for index, ending in enumerate(route.get("alternate_endings", []), start=1)
    ]
    actions = {step["action"] for step in steps}
    if "swipe_keycard" in actions:
        kind = "keycard_route"
    elif "turn_wheel" in actions:
        kind = "mechanical_route"
    elif "press_button" in actions:
        kind = "button_route"
    else:
        kind = "switch_route"
    result = {
        "id": f"puzzle-route-{number:03d}",
        "kind": kind,
        "required_keycards": sorted({
            str(step["properties"].get("keycard"))
            for step in steps
            if step["action"] == "swipe_keycard" and
            step["properties"].get("keycard")
        }),
        "steps": steps,
        "endpoint_count": 1 + len(alternate_endings),
        "source": "prefab_asset",
    }
    if alternate_endings:
        result["common_step_count"] = int(route["common_step_count"])
        result["alternate_endings"] = alternate_endings
    return result


def _map_interactable_type(path: str) -> tuple[str, dict] | None:
    lowered = path.casefold().replace("\\", "/")
    name = Path(lowered).name
    if name in {"recycler_static.prefab", "recycler.prefab"}:
        return "recycler", {}
    if name in {"researchtable_static.prefab", "researchtable_deployed.prefab",
                "io_research_table.prefab"}:
        return "research_table", {}
    if (name == "small_refinery_static.prefab" or
            name in {"refinery_small_deployed.prefab", "refinery_large_deployed.prefab"}):
        return "oil_refinery", {}
    if name in {"repairbench_static.prefab", "repairbench_deployed.prefab"}:
        return "repair_bench", {}
    if name in {"mixingtable_on.static.prefab", "mixingtable.deployed.prefab"}:
        return "mixing_table", {}
    for level in (1, 2, 3):
        if name in {f"workbench{level}.static.prefab", f"workbench{level}.deployed.prefab"}:
            return "workbench", {"level": level}
    if name in {"marketterminal.prefab", "marketplace_terminal.prefab"}:
        return "marketplace", {}
    if "npcvendingmachine" in name and name.endswith(".prefab"):
        return "vending_machine", {}
    return None


def _map_loot_type(path: str) -> str | None:
    lowered = path.casefold().replace("\\", "/")
    name = Path(lowered).name
    if "spawner" in name:
        return None
    if name == "diesel_collectable.prefab":
        return "diesel_fuel"
    if ("/radtown/" not in lowered and
            "/chinooklockedcrate/" not in lowered):
        return None
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


def _place_map_interactable(path: str, prefab, world_size: int,
                            kind: str, properties: dict) -> dict:
    matrix = _instance_matrix(prefab)
    return {
        "type": kind,
        "component_type": None,
        "object_name": Path(path).stem,
        "properties": properties,
        **_placed_transform(np.eye(4), matrix, world_size),
        "source": "map_prefab",
        "prefab_path": path,
        "map_category": prefab.category,
    }


def _place_map_loot(path: str, prefab, world_size: int, kind: str) -> dict:
    matrix = _instance_matrix(prefab)
    return {
        "type": kind,
        "object_name": Path(path).stem,
        **_placed_transform(np.eye(4), matrix, world_size),
        "source": "map_prefab",
        "prefab_path": path,
        "map_category": prefab.category,
    }


def _bounds_volume(bounds: dict | None) -> float:
    if not bounds:
        return math.inf
    extents = bounds["extents"]
    return max(0.0, 8.0 * float(extents["x"]) * float(extents["y"]) *
               float(extents["z"]))


def _contains_world_position(context: dict, position: dict) -> bool:
    bounds = context.get("bounds")
    if not bounds:
        return False
    point = np.asarray([
        float(position["x"]), float(position["y"]), float(position["z"]), 1.0
    ])
    local = np.linalg.inv(context["instance_matrix"]) @ point
    center, extents = bounds["center"], bounds["extents"]
    return all(
        abs(float(local[index]) - float(center[axis])) <= float(extents[axis]) + 1e-5
        for index, axis in enumerate(("x", "y", "z"))
    )


def _association_radius(item: dict) -> float:
    size = item["metadata"]["classification"]["size_class"]
    return {"tiny": 80.0, "small": 160.0, "medium": 280.0,
            "large": 450.0, "xlarge": 700.0}.get(size, 280.0)


def _associate_interactable(value: dict, contexts: list[dict]) -> dict | None:
    containing = [context for context in contexts
                  if _contains_world_position(context, value["position"])]
    if containing:
        return min(containing, key=lambda context: (
            _bounds_volume(context.get("bounds")),
            context["item"]["prefab_path"].casefold(),
        ))
    x, z = float(value["position"]["x"]), float(value["position"]["z"])
    nearby = []
    for context in contexts:
        root = context["item"]["position"]
        distance = math.hypot(x - float(root["x"]), z - float(root["z"]))
        if distance <= _association_radius(context["item"]):
            nearby.append((distance, context["item"]["prefab_path"].casefold(), context))
    return min(nearby, key=lambda value: (value[0], value[1]))[2] if nearby else None


def _set_assigned_local_position(value: dict, context: dict) -> None:
    """Express a directly placed map interactable in its monument's frame."""
    position = value["position"]
    world_point = np.asarray([
        float(position["x"]), float(position["y"]), float(position["z"]), 1.0,
    ], dtype=np.float64)
    local_point = np.linalg.inv(context["instance_matrix"]) @ world_point
    value["local_position"] = {
        axis: round(float(local_point[index]), 9)
        for index, axis in enumerate(("x", "y", "z"))
    }


def _append_direct_loot(item: dict, value: dict) -> None:
    loot = item["loot"]
    start = len(loot["positions"]) // loot["position_stride"]
    position = value["position"]
    local = value["local_position"]
    map_position = value["map_position"]
    loot["positions"].extend([
        float(local["x"]), float(local["y"]), float(local["z"]),
        float(position["x"]), float(position["y"]), float(position["z"]),
        float(map_position["x"]), float(map_position["y"]), 0.0,
    ])
    high_level_kind = (
        "barrel" if value["type"] == "barrel"
        else "diesel_fuel" if value["type"] == "diesel_fuel"
        else "crate"
    )
    loot["groups"].append({
        "id": f"loot-group-{len(loot['groups']) + 1:03d}",
        "kind": high_level_kind,
        "object_name": value["object_name"],
        "source": "map_prefab",
        "max_population": 1,
        "spawn_per_tick": None,
        "respawn_seconds": None,
        "wants_initial_spawn": None,
        "prevent_duplicates": None,
        "variant_fields": ["type", "prefab_path", "weight"],
        "variants": [[value["type"], value["prefab_path"], 1]],
        "position_start": start,
        "position_count": 1,
    })
    loot["summary"] = _loot_summary(loot["groups"])


def _update_detailed_counts(item: dict) -> None:
    counts = Counter(value["type"] for value in item.get("interactables", []))
    gameplay = item["metadata"]["gameplay"]
    gameplay.update({
        "recycler_count": counts["recycler"],
        "research_table_count": counts["research_table"],
        "oil_refinery_count": counts["oil_refinery"],
        "repair_bench_count": counts["repair_bench"],
        "mixing_table_count": counts["mixing_table"],
        "workbench_count": counts["workbench"],
        "vending_machine_count": counts["vending_machine"],
    })


def _interactables_sidecar(monuments: list[dict], unassigned: list[dict],
                           world) -> dict:
    documents = [{
        "monument_id": str(item["id"]),
        "prefab": str(item["prefab_path"]),
        "interactables": item["interactables"],
    } for item in monuments if item.get("interactables")]
    return {
        "schema_version": 1,
        "map": {"world_size": int(world.size), "timestamp": int(world.timestamp)},
        "coordinates": {
            "local_position": "metres in the owning monument prefab frame",
            "position": "Unity world metres",
            "map_position": "map metres from bottom-left",
            "heading_degrees": "rotation Y normalized to [0, 360)",
        },
        "monuments": documents,
        "interactable_count": sum(
            len(item["interactables"]) for item in documents
        ),
        "unassigned_interactable_count": len(unassigned),
        "unassigned_interactables": unassigned,
    }


def _puzzles_sidecar(monuments: list[dict], world) -> dict:
    documents = [{
        "monument_id": str(item["id"]),
        "prefab": str(item["prefab_path"]),
        "puzzles": item["puzzles"],
    } for item in monuments if item.get("puzzles")]
    return {
        "schema_version": 1,
        "map": {"world_size": int(world.size), "timestamp": int(world.timestamp)},
        "coordinates": {
            "local_position": "metres in the owning monument prefab frame",
            "position": "Unity world metres",
            "map_position": "map metres from bottom-left",
        },
        "monuments": documents,
        "puzzle_count": sum(len(item["puzzles"]) for item in documents),
        "route_semantics": (
            "only major causal player actions and their door outcomes; alternate "
            "doors sharing prerequisites are alternate endings"
        ),
    }


def _loot_sidecar(monuments: list[dict], world) -> dict:
    loot_items = [
        (item, item.get("loot"))
        for item in monuments if item.get("loot", {}).get("groups")
    ]
    monument_documents = {}
    marker_count = 0
    for item, loot in loot_items:
        monument = monument_documents.setdefault(
            str(item["prefab_path"]),
            {"prefab": str(item["prefab_path"]), "loot": []},
        )
        stride = int(loot["position_stride"])
        for group in loot["groups"]:
            prefabs = sorted(({
                "kind": str(variant[0] or "unknown"),
                "prefab": str(variant[1]),
            } for variant in group["variants"]), key=lambda value: (
                value["kind"], value["prefab"].casefold(),
            ))
            source_start = int(group["position_start"]) * stride
            source_end = source_start + int(group["position_count"]) * stride
            values = loot["positions"][source_start:source_end]
            positions = []
            for offset in range(0, len(values), stride):
                row = values[offset:offset + stride]
                positions.append({
                    "local_position": {
                        "x": round(float(row[0]), 3),
                        "y": round(float(row[1]), 3),
                        "z": round(float(row[2]), 3),
                    },
                    "position": {
                        "x": round(float(row[3]), 3),
                        "y": round(float(row[4]), 3),
                        "z": round(float(row[5]), 3),
                    },
                    "map_position": {
                        "x": round(float(row[6]), 3),
                        "y": round(float(row[7]), 3),
                    },
                    "radius": round(float(row[8]), 3),
                })
            marker_count += len(positions)
            monument["loot"].append({
                "kind": str(group["kind"]),
                "prefabs": prefabs,
                "positions": positions,
            })
    return {
        "schema_version": 5,
        "map": {"world_size": int(world.size), "timestamp": int(world.timestamp)},
        "coordinates": {
            "local_position": "metres in the owning monument prefab frame",
            "position": "Unity world metres",
            "map_position": "map metres from bottom-left",
            "radius": "zero for an exact point; otherwise radial spawn uncertainty",
        },
        "monuments": list(monument_documents.values()),
        "loot_count": marker_count,
    }


def _radiation_sidecar(monuments: list[dict], world) -> dict:
    radiation_items = [
        (item, item.get("radiation"))
        for item in monuments if item.get("radiation", {}).get("zones")
    ]
    monument_documents = {}
    zone_count = 0
    for item, radiation in radiation_items:
        monument = monument_documents.setdefault(
            str(item["prefab_path"]),
            {"prefab": str(item["prefab_path"]), "zones": []},
        )
        for zone in radiation["zones"]:
            position = zone["position"]
            value = {
                "local_position": {
                    "x": round(float(position[0]), 3),
                    "y": round(float(position[1]), 3),
                    "z": round(float(position[2]), 3),
                },
                "position": {
                    "x": round(float(position[3]), 3),
                    "y": round(float(position[4]), 3),
                    "z": round(float(position[5]), 3),
                },
                "map_position": {
                    "x": round(float(position[6]), 3),
                    "y": round(float(position[7]), 3),
                },
                "rotation_euler": {
                    axis: round(float(zone["rotation_euler"][axis]), 3)
                    for axis in ("x", "y", "z")
                },
                "shape": str(zone["shape"]),
                "radiation_amount": (
                    round(float(zone["radiation_amount"]), 3)
                    if zone.get("radiation_amount") is not None else None
                ),
                "tier": str(zone.get("tier") or "none"),
                "dynamic": bool(zone.get("dynamic", False)),
                "falloff": (
                    round(float(zone["falloff"]), 3)
                    if zone.get("falloff") is not None else None
                ),
                "bypass_armor": bool(zone.get("bypass_armor", False)),
                "increase_near_center": bool(
                    zone.get("increase_near_center", False)
                ),
                "use_line_of_sight": bool(
                    zone.get("use_line_of_sight", False)
                ),
                "ignore_above_ground": bool(
                    zone.get("ignore_above_ground", False)
                ),
                "minimum_local_height": (
                    round(float(zone["minimum_local_height"]), 3)
                    if zone.get("minimum_local_height") is not None else None
                ),
            }
            if zone["shape"] == "sphere":
                value["radius"] = round(float(zone["radius"]), 3)
            else:
                value["size"] = {
                    axis: round(float(zone["size"][axis]), 3)
                    for axis in ("x", "y", "z")
                }
            monument["zones"].append(value)
            zone_count += 1
    return {
        "schema_version": 2,
        "map": {"world_size": int(world.size), "timestamp": int(world.timestamp)},
        "coordinates": {
            "local_position": "metres in the owning monument prefab frame",
            "position": "Unity world metres",
            "map_position": "map metres from bottom-left",
        },
        "monuments": list(monument_documents.values()),
        "zone_count": zone_count,
        "amount_semantics": (
            "raw TriggerRadiation amount before clothing/armor protection; null means "
            "the amount is controlled dynamically at runtime"
        ),
    }


def _build_monument_documents(world, manifest: PrefabManifest, *,
                              interactable: bool = False, puzzles: bool = False,
                              loot: bool = False,
                              radiation_zones: bool = False,
                              transforms: TransformOptions | None = None,
                              ) -> tuple[
                                  dict, dict | None, dict | None,
                                  dict | None, dict | None,
                              ]:
    """Build the main monument document and optional compact sidecars."""
    if world.size <= 0:
        raise ValueError("World size must be positive to normalize monument positions")
    transforms = transforms or TransformOptions()

    expanded = bool(interactable or puzzles or loot or radiation_zones)

    candidates = []
    for prefab in world.prefabs:
        entry = manifest.get(prefab.prefab_id)
        if entry is None or not _is_monument_candidate(entry.path, prefab, expanded):
            continue
        if prefab.position is None:
            continue
        candidates.append((entry.path, prefab))

    candidates.sort(key=lambda item: (
        item[0].casefold(), float(item[1].position.x),
        float(item[1].position.y), float(item[1].position.z),
        int(item[1].prefab_id),
    ))

    monuments = []
    detail_contexts = []
    fallback_instance_count = 0
    for path, prefab in candidates:
        landmarks, used_fallback = _visible_landmarks(path)
        fallback_instance_count += int(used_fallback)
        instance_matrix = _instance_matrix(prefab)
        placed_for_instance = []
        root_item = None
        for landmark in landmarks:
            local_matrix = (
                np.eye(4, dtype=np.float64) if landmark is None
                else np.asarray(landmark["local_matrix"], dtype=np.float64)
            )
            world_matrix = instance_matrix @ local_matrix
            transform = _decompose(world_matrix)
            position = transform["position"]
            map_x = round(float(position["x"]) + float(world.size) / 2.0, 9)
            map_y = round(float(position["z"]) + float(world.size) / 2.0, 9)
            metadata = _landmark_metadata(path, landmark)
            item = {
                "name": (
                    Path(path).stem if landmark is None or
                    metadata["classification"]["kind"] not in {
                        "train_tunnel_entrance", "train_tunnel_link"
                    } else metadata["family"]
                ),
                "prefab_path": path,
                "map_category": prefab.category,
                "position": position,
                "map_position": {"x": map_x, "y": map_y},
                "heading_degrees": transform["rotation_euler"]["y"],
                "metadata": metadata,
            }
            monuments.append(item)
            placed_for_instance.append(item)
            if landmark is None:
                root_item = item
        if expanded and placed_for_instance:
            root_item = root_item or placed_for_instance[0]
            definition = _gameplay_database().get("prefabs", {}).get(path.casefold(), {})
            if interactable:
                root_item["interactables"] = [
                    _place_asset_interactable(
                        value, instance_matrix, world.size, path
                    )
                    for value in definition.get("interactables", [])
                ]
            if puzzles:
                root_item["puzzles"] = [
                    _place_puzzle_route(route, instance_matrix, world.size, number)
                    for number, route in enumerate(
                        _major_puzzle_routes(definition.get("puzzles", [])), start=1
                    )
                ]
            if loot:
                root_item["loot"] = _place_loot(
                    definition.get("loot_spawn_groups", []),
                    instance_matrix, world.size,
                )
            if radiation_zones:
                root_item["radiation"] = _place_radiation(
                    definition.get("radiation_zones", []),
                    instance_matrix, world.size,
                )
            detail_contexts.append({
                "item": root_item,
                "instance_matrix": instance_matrix,
                "bounds": definition.get("bounds"),
            })

    unassigned_interactables = []
    if expanded:
        direct = []
        direct_loot = []
        for prefab in world.prefabs:
            entry = manifest.get(prefab.prefab_id)
            if entry is None or prefab.position is None:
                continue
            classification = (
                _map_interactable_type(entry.path) if interactable else None
            )
            if classification is not None:
                kind, properties = classification
                direct.append(_place_map_interactable(
                    entry.path, prefab, world.size, kind, properties
                ))
            loot_type = _map_loot_type(entry.path) if loot else None
            if loot_type is not None:
                direct_loot.append(_place_map_loot(
                    entry.path, prefab, world.size, loot_type
                ))
        direct.sort(key=lambda value: (
            value["type"], value["prefab_path"].casefold(),
            float(value["position"]["x"]), float(value["position"]["y"]),
            float(value["position"]["z"]),
        ))
        for value in direct:
            context = _associate_interactable(value, detail_contexts)
            if context is None:
                unassigned_interactables.append(value)
            else:
                _set_assigned_local_position(value, context)
                context["item"]["interactables"].append(value)
        direct_loot.sort(key=lambda value: (
            value["type"], value["prefab_path"].casefold(),
            float(value["position"]["x"]), float(value["position"]["y"]),
            float(value["position"]["z"]),
        ))
        for value in direct_loot:
            context = _associate_interactable(value, detail_contexts)
            if context is not None:
                _set_assigned_local_position(value, context)
                _append_direct_loot(context["item"], value)
        for context in detail_contexts:
            item = context["item"]
            if interactable:
                item["interactables"].sort(key=lambda value: (
                    value["type"], value.get("prefab_path", "").casefold(),
                    float(value["position"]["x"]), float(value["position"]["y"]),
                    float(value["position"]["z"]),
                ))
                _update_detailed_counts(item)
            if puzzles:
                item["puzzles"].sort(key=lambda value: value["id"])

    monuments.sort(key=lambda item: (
        item["prefab_path"].casefold(), float(item["position"]["x"]),
        float(item["position"]["y"]), float(item["position"]["z"]),
        item["name"].casefold(),
    ))

    for number, item in enumerate(monuments, start=1):
        item["id"] = f"monument-{number:03d}"

    interactables_document = (
        _interactables_sidecar(monuments, unassigned_interactables, world)
        if interactable else None
    )
    puzzles_document = _puzzles_sidecar(monuments, world) if puzzles else None
    loot_document = _loot_sidecar(monuments, world) if loot else None
    radiation_document = (
        _radiation_sidecar(monuments, world) if radiation_zones else None
    )
    for item in monuments:
        item.pop("interactables", None)
        item.pop("puzzles", None)
        item.pop("loot", None)
        item.pop("radiation", None)

    main_document = {
        "schema_version": 13,
        "map": {
            "serialization_version": int(world.serialization_version),
            "timestamp": int(world.timestamp),
            "world_size": int(world.size),
        },
        "coordinates": {
            "position": "Unity world coordinates in metres: X east/west, Y elevation, Z north/south",
            "map_position": "map metres from bottom-left: x = world_x + world_size/2; y = world_z + world_size/2",
            "map_origin": "(0, 0) is the bottom-left of the playable map; positive X is right and positive Y is up",
            "heading_degrees": "rotation Y normalized to [0, 360)",
        },
        "selection": {
            "prefab_path_prefixes": list(MONUMENT_PATH_PREFIXES),
            "includes_train_tunnel_entrances": True,
            "includes_train_tunnel_links": True,
            "server_behavior": "gameplay roots plus visible train-tunnel LandmarkInfo child transforms",
            "excludes_unique_environments": True,
            "includes_custom_monument_category_in_expanded_mode": bool(expanded),
            "interactables_sidecar": bool(interactable),
            "puzzles_sidecar": bool(puzzles),
            "loot_sidecar": bool(loot),
            "radiation_zones_sidecar": bool(radiation_zones),
            "exported_position_fields": exported_position_fields(transforms),
        },
        "monument_count": len(monuments),
        "unique_prefab_count": len({item["prefab_path"].casefold() for item in monuments}),
        "metadata": {
            "schema_version": 1,
            "classification_source": "deterministic prefab-path rules",
            "safe_zone_source": "curated prefab identities",
            "component_fields": [
                "recycler_count", "keycard_requirements", "puzzle_type",
                "loot_tier", "maximum_radiation",
            ],
            "component_fields_status": "populated from packaged Rust prefab-component extraction",
            "component_database": {
                "schema_version": _gameplay_database().get("schema_version"),
                "source_rust_build_id": _gameplay_database().get("source", {}).get("rust_build_id"),
                "prefab_count": _gameplay_database().get("prefab_count", 0),
                "extraction": _gameplay_database().get("extraction", {}),
            },
            "position_source": "serialized gameplay roots and packaged prefab-root-relative tunnel LandmarkInfo transforms",
            "root_position_fallback_instance_count": fallback_instance_count,
            "enriched_instance_count": len(monuments),
        },
        "details": {
            "interactables_enabled": bool(interactable),
            "puzzles_enabled": bool(puzzles),
            "interactable_count": int(
                (interactables_document or {}).get("interactable_count", 0)
            ),
            "puzzle_count": int(
                (puzzles_document or {}).get("puzzle_count", 0)
            ),
            "unassigned_interactable_count": len(unassigned_interactables),
            "sources": ["packaged prefab-component transforms", "serialized map prefabs"],
            "custom_monument_behavior": (
                "nonstandard map-category Monument roots are included; directly placed "
                "recognized interactables are assigned by monument bounds, then a "
                "conservative size-class radius; unresolved items remain unassigned"
            ),
            "puzzle_order": (
                "compact causal routes derived from the internal IOEntity graph; only "
                "major player actions and their door outcome are exported; equivalent "
                "access doors sharing one prerequisite circuit are alternate endings"
            ),
        },
        "sidecars": {
            "interactables": (
                "monument_interactables.json" if interactable else None
            ),
            "puzzles": "monument_puzzles.json" if puzzles else None,
            "loot": "monument_loot.json" if loot else None,
            "radiation_zones": (
                "monument_radiation_zones.json" if radiation_zones else None
            ),
        },
        "monuments": monuments,
    }

    exported_transform_fields = exported_position_fields(transforms)
    for document in (
        main_document, interactables_document, puzzles_document,
        loot_document, radiation_document,
    ):
        if document is None:
            continue
        document["exported_position_fields"] = list(exported_transform_fields)
        strip_disabled_position_fields(document, transforms)

    return (
        main_document, interactables_document, puzzles_document,
        loot_document, radiation_document,
    )


def build_monument_export(world, manifest: PrefabManifest, *,
                          interactable: bool = False,
                          puzzles: bool = False,
                          transforms: TransformOptions | None = None) -> dict:
    """Return the main monument document without large optional sidecars."""
    document, _, _, _, _ = _build_monument_documents(
        world, manifest, interactable=interactable, puzzles=puzzles,
        transforms=transforms,
    )
    return document


def save_monuments(world, manifest_path: str | Path, output_path: str | Path, *,
                   interactable: bool = False, puzzles: bool = False,
                   loot_output_path: str | Path | None = None,
                   radiation_output_path: str | Path | None = None,
                   interactables_output_path: str | Path | None = None,
                   puzzles_output_path: str | Path | None = None,
                   transforms: TransformOptions | None = None) -> dict:
    manifest = PrefabManifest.load(manifest_path)
    target = Path(output_path)
    if interactable:
        interactables_output_path = (
            Path(interactables_output_path) if interactables_output_path is not None
            else target.with_name("monument_interactables.json")
        )
    if puzzles:
        puzzles_output_path = (
            Path(puzzles_output_path) if puzzles_output_path is not None
            else target.with_name("monument_puzzles.json")
        )
    (payload, interactables_payload, puzzles_payload,
     loot_payload, radiation_payload) = _build_monument_documents(
        world, manifest, interactable=interactable, puzzles=puzzles,
        loot=loot_output_path is not None,
        radiation_zones=radiation_output_path is not None,
        transforms=transforms,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    for path, sidecar in (
        (interactables_output_path, interactables_payload),
        (puzzles_output_path, puzzles_payload),
        (loot_output_path, loot_payload),
        (radiation_output_path, radiation_payload),
    ):
        if path is not None and sidecar is not None:
            Path(path).write_text(
                json.dumps(sidecar, indent=2) + "\n",
                encoding="utf-8", newline="\n",
            )
    payload["sidecar_counts"] = {
        "interactable_count": int(
            (interactables_payload or {}).get("interactable_count", 0)
        ),
        "unassigned_interactable_count": int(
            (interactables_payload or {}).get(
                "unassigned_interactable_count", 0
            )
        ),
        "puzzle_count": int((puzzles_payload or {}).get("puzzle_count", 0)),
        "loot_position_count": int((loot_payload or {}).get("loot_count", 0)),
        "radiation_zone_count": int((radiation_payload or {}).get("zone_count", 0)),
    }
    return payload
