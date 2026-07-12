"""Export placed gameplay monuments from a parsed Rust world map."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path

from .prefabs import PrefabManifest


MONUMENT_PATH_PREFIX = "assets/bundled/prefabs/autospawn/monument/"
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
    "radtown_1": "Sewer Branch", "radtown_small_3": "Mining Outpost",
    "satellite_dish": "Satellite Dish Array", "sphere_tank": "The Dome",
    "trainyard_1": "Train Yard", "underwater_lab_a": "Underwater Lab",
    "water_treatment_plant_1": "Water Treatment Plant",
}


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().title()


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
    relative = path.casefold()[len(MONUMENT_PATH_PREFIX):]
    parts = relative.removesuffix(".prefab").split("/")
    group, name = parts[0], parts[-1]
    database = _gameplay_database()
    extracted = database.get("prefabs", {}).get(path.casefold())
    safe_zone = bool(extracted["safe_zone"]) if extracted is not None else name in SAFE_ZONE_NAMES
    size_class = _size_class(group, name)
    if group == "cave":
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
        }
    else:
        gameplay = {
            "safe_zone": bool(extracted["safe_zone"]),
            "recycler_count": int(extracted["recycler_count"]),
            "keycard_requirements": list(extracted["keycard_requirements"]),
            "puzzle_type": str(extracted["puzzle_type"]),
            "loot_tier": int(extracted["loot_tier"]),
        }
    return {
        "display_name": DISPLAY_NAMES.get(name, _humanize(name)),
        "family": _family(name),
        "classification": {
            "kind": kind,
            "environment": environment,
            "spawn_group": group,
            "size_class": size_class,
        },
        "gameplay": gameplay,
        "tags": sorted(set(tags)),
    }


def build_monument_export(world, manifest: PrefabManifest) -> dict:
    """Return a deterministic JSON-compatible gameplay monument document."""
    if world.size <= 0:
        raise ValueError("World size must be positive to normalize monument positions")

    candidates = []
    for prefab in world.prefabs:
        entry = manifest.get(prefab.prefab_id)
        if entry is None or not entry.path.casefold().startswith(MONUMENT_PATH_PREFIX):
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
    for path, prefab in candidates:
        position = prefab.position
        rotation = prefab.rotation
        heading = None
        if rotation is not None:
            heading = round(float(rotation.y) % 360.0, 9)
        map_x = round(float(position.x) + float(world.size) / 2.0, 9)
        map_y = round(float(position.z) + float(world.size) / 2.0, 9)
        monuments.append({
            "name": Path(path).stem,
            "prefab_path": path,
            "map_category": prefab.category,
            "position": {
                "x": float(position.x), "y": float(position.y), "z": float(position.z),
            },
            "map_position": {"x": map_x, "y": map_y},
            "heading_degrees": heading,
            "metadata": monument_metadata(path),
        })

    return {
        "schema_version": 3,
        "map": {
            "serialization_version": int(world.serialization_version),
            "timestamp": int(world.timestamp),
            "world_size": int(world.size),
        },
        "coordinates": {
            "world": "Unity world coordinates in metres: X east/west, Y elevation, Z north/south",
            "map_position": "map metres from bottom-left: x = world_x + world_size/2; y = world_z + world_size/2",
            "map_origin": "(0, 0) is the bottom-left of the playable map; positive X is right and positive Y is up",
            "heading_degrees": "rotation Y normalized to [0, 360)",
        },
        "selection": {
            "prefab_path_prefix": MONUMENT_PATH_PREFIX,
            "excludes_tunnel_entrances": True,
            "excludes_unique_environments": True,
        },
        "monument_count": len(monuments),
        "unique_prefab_count": len({item["prefab_path"].casefold() for item in monuments}),
        "metadata": {
            "schema_version": 1,
            "classification_source": "deterministic prefab-path rules",
            "safe_zone_source": "curated prefab identities",
            "component_fields": ["recycler_count", "keycard_requirements", "puzzle_type", "loot_tier"],
            "component_fields_status": "populated from packaged Rust prefab-component extraction",
            "component_database": {
                "schema_version": _gameplay_database().get("schema_version"),
                "source_rust_build_id": _gameplay_database().get("source", {}).get("rust_build_id"),
                "prefab_count": _gameplay_database().get("prefab_count", 0),
                "extraction": _gameplay_database().get("extraction", {}),
            },
            "enriched_instance_count": len(monuments),
        },
        "monuments": monuments,
    }


def save_monuments(world, manifest_path: str | Path, output_path: str | Path) -> dict:
    manifest = PrefabManifest.load(manifest_path)
    payload = build_monument_export(world, manifest)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return payload
