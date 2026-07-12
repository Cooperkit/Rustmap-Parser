"""Maintainer extraction of sanitized monument no-build collider geometry."""

from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import sys
from pathlib import Path

import numpy as np

from .no_build import AUTOSPAWN_PREFIX, SURFACE_FAMILIES, _autospawn_family, _decompose
from .tunnel_assets import _component, _local_matrix, _world_matrix, bundle_identity


DATA_SCHEMA_VERSION = 4
ONLY_BLOCK_DEPLOYABLES = 512
BLOCKER_NAME_PARTS = ("prevent_build", "preventbuilding", "no_build", "nobuild")


def _include_surface_blocker(family: str | None, tagged: bool) -> bool:
    return family in SURFACE_FAMILIES and (family != "tunnel-upwards" or tagged)


def default_no_build_data() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "rustmap" / "no_build_zones.json"


def _class_name(component) -> str:
    if component.component.type.name != "MonoBehaviour":
        return component.component.type.name
    try:
        return component.component.read().m_Script.read().m_ClassName
    except Exception:
        return "MonoBehaviour"


def _root_game_object(game_object):
    current = game_object
    while True:
        transform = _component(current, "Transform")
        if transform is None or not transform.m_Father.path_id:
            return current
        current = transform.m_Father.read().m_GameObject.read()


def _has_prevent_tag(game_object) -> bool:
    return any(_class_name(item) == "PreventBuildingMonumentTag"
               for item in game_object.m_Component)


def _collider_flags(game_object) -> int:
    for item in game_object.m_Component:
        if _class_name(item) == "ColliderInfo":
            try:
                return int(item.component.read_typetree().get("flags", 0))
            except Exception:
                return 0
    return 0


def _collider_definition(component, local_matrix: np.ndarray, source: str):
    kind = component.type.name
    value = component.read()
    center = getattr(value, "m_Center", None)
    cx, cy, cz = (float(center.x), float(center.y), float(center.z)) if center else (0.0, 0.0, 0.0)
    if kind == "BoxCollider":
        size = value.m_Size
        sx, sy, sz = float(size.x), float(size.y), float(size.z)
        local_points = np.asarray([
            [cx+dx*sx/2, cy+dy*sy/2, cz+dz*sz/2]
            for dx in (-1,1) for dy in (-1,1) for dz in (-1,1)
        ], dtype=np.float64)
        homogeneous = np.column_stack((local_points, np.ones(len(local_points))))
        root_points = (local_matrix @ homogeneous.T).T[:, :3]
        planar = root_points[[0,4,5,1]][:, [0,2]]
        edge_x, edge_z = planar[1]-planar[0], planar[3]-planar[0]
        length_x, length_z = np.linalg.norm(edge_x), np.linalg.norm(edge_z)
        if length_x == 0.0 or length_z == 0.0 or abs(float(edge_x @ edge_z)) > length_x*length_z*1e-5:
            return None, "nonrepresentable"
        projected_area = float(length_x * length_z)
        definition = {"shape": "rectangle", "size": {"x": sx, "y": sy, "z": sz}}
        root_center = (local_matrix @ np.asarray([cx,cy,cz,1.0]))[:3]
        analysis = {
            "shape": "rectangle", "center": root_center[[0,2]].tolist(),
            "axis_x": (edge_x/length_x).tolist(), "axis_z": (edge_z/length_z).tolist(),
            "half_width": float(length_x/2.0), "half_height": float(length_z/2.0),
        }
    elif kind == "SphereCollider":
        radius = float(value.m_Radius)
        projected = local_matrix[[0,2], :3]
        gram = projected @ projected.T
        scale2 = float((gram[0,0] + gram[1,1]) * 0.5)
        tolerance = max(1.0, scale2) * 1e-5
        if abs(float(gram[0,0]-gram[1,1])) > tolerance or abs(float(gram[0,1])) > tolerance:
            return None, "nonrepresentable"
        projected_area = math.pi * radius * radius * scale2
        root_center = (local_matrix @ np.asarray([cx,cy,cz,1.0]))[:3]
        y_radius = radius * float(np.linalg.norm(local_matrix[1,:3]))
        root_points = np.asarray([
            [root_center[0], root_center[1]-y_radius, root_center[2]],
            [root_center[0], root_center[1]+y_radius, root_center[2]],
        ])
        definition = {"shape": "circle", "radius": radius}
        analysis = {
            "shape": "circle", "center": root_center[[0,2]].tolist(),
            "radius": float(radius * math.sqrt(scale2)),
        }
    else:
        return None, "unsupported_shape"
    definition.update({
        "source": source,
        "local_matrix": np.round(local_matrix, 12).tolist(),
        "local_transform": _decompose(local_matrix),
        "center": {"x": cx, "y": cy, "z": cz},
        "local_y_bounds": [float(root_points[:,1].min()), float(root_points[:,1].max())],
        "projected_area_m2": projected_area,
        "_analysis": analysis,
    })
    return definition, None


def _rectangle_corners(primitive: dict) -> list[np.ndarray]:
    center=np.asarray(primitive["center"],dtype=np.float64)
    axis_x=np.asarray(primitive["axis_x"],dtype=np.float64)*primitive["half_width"]
    axis_z=np.asarray(primitive["axis_z"],dtype=np.float64)*primitive["half_height"]
    return [center+sx*axis_x+sz*axis_z for sx,sz in ((-1,-1),(1,-1),(1,1),(-1,1))]


def _primitive_contains(outer: dict, inner: dict, tolerance: float = 1e-6) -> bool:
    """Return whether one prefab-local circle/rectangle fully contains another."""
    outer_center=np.asarray(outer["center"],dtype=np.float64)
    inner_center=np.asarray(inner["center"],dtype=np.float64)
    if outer["shape"] == "circle":
        radius=float(outer["radius"])+tolerance
        if inner["shape"] == "circle":
            return float(np.linalg.norm(inner_center-outer_center))+float(inner["radius"]) <= radius
        return all(float(np.linalg.norm(point-outer_center)) <= radius
                   for point in _rectangle_corners(inner))

    axis_x=np.asarray(outer["axis_x"],dtype=np.float64)
    axis_z=np.asarray(outer["axis_z"],dtype=np.float64)
    half_width=float(outer["half_width"])+tolerance
    half_height=float(outer["half_height"])+tolerance
    if inner["shape"] == "circle":
        offset=inner_center-outer_center
        radius=float(inner["radius"])
        return (abs(float(offset@axis_x))+radius <= half_width and
                abs(float(offset@axis_z))+radius <= half_height)
    for point in _rectangle_corners(inner):
        offset=point-outer_center
        if (abs(float(offset@axis_x)) > half_width or
                abs(float(offset@axis_z)) > half_height):
            return False
    return True


def _remove_contained(zones: list[dict]) -> tuple[list[dict], int]:
    ordered=sorted(zones,key=lambda item:(
        -float(item["projected_area_m2"]), item["object_name"].casefold(), item["shape"],
        json.dumps(item["local_matrix"],separators=(",",":")),
    ))
    retained=[]
    removed=0
    for zone in ordered:
        if any(_primitive_contains(outer["_analysis"],zone["_analysis"])
               for outer in retained):
            removed+=1
            continue
        retained.append(zone)
    for zone in retained:
        zone.pop("_analysis",None)
        zone.pop("object_name",None)
        zone.pop("collider_flags",None)
        zone.pop("local_transform",None)
    return retained,removed


def _extract_worker(install_text: str, target_text: str, conn) -> None:
    try:
        import UnityPy
        install = Path(install_text)
        identity = bundle_identity(install)
        environment = UnityPy.load(
            str(install / "Bundles" / "shared" / "assetscenes.bundle"),
            str(install / "Bundles" / "shared" / "content.bundle"),
        )
        scenes = []
        for root in environment.files.values():
            for name, asset_file in (getattr(root, "files", None) or {}).items():
                if name.startswith("BuildPlayer-AssetScene-monument.") and not name.endswith("sharedAssets"):
                    scenes.append((name, asset_file))
        prefab_zones: dict[str, list[dict]] = {}
        excluded = {"contained_by_larger_same_owner_zone": 0,
                    "unsupported_shape": 0, "nonrepresentable": 0,
                    "deployable_only": 0,
                    "untagged_tunnel_upwards_internal": 0}
        for scene_name, scene in sorted(scenes):
            transform_memo: dict[int, np.ndarray] = {}
            for obj in scene.objects.values():
                if obj.type.name != "GameObject":
                    continue
                try:
                    game_object = obj.read()
                    normalized_name = game_object.m_Name.casefold().replace(" ", "_")
                    named = any(part in normalized_name for part in BLOCKER_NAME_PARTS)
                    tagged = _has_prevent_tag(game_object)
                    if not named and not tagged:
                        continue
                    flags = _collider_flags(game_object)
                    if flags & ONLY_BLOCK_DEPLOYABLES:
                        excluded["deployable_only"] += 1
                        continue
                    root_go = _root_game_object(game_object)
                    path = root_go.m_Name.casefold().replace("\\", "/")
                    family=_autospawn_family(path)
                    if not path.startswith(AUTOSPAWN_PREFIX) or family not in SURFACE_FAMILIES:
                        continue
                    if not _include_surface_blocker(family,tagged):
                        excluded["untagged_tunnel_upwards_internal"] += sum(
                            item.component.type.name in {"BoxCollider","SphereCollider",
                                                        "CapsuleCollider","MeshCollider"}
                            for item in game_object.m_Component
                        )
                        continue
                    root_transform = _component(root_go, "Transform")
                    child_transform = _component(game_object, "Transform")
                    relative = np.linalg.inv(_world_matrix(root_transform, transform_memo)) @ \
                               _world_matrix(child_transform, transform_memo)
                    source = "prevent_building_monument_tag" if tagged else "block_placement"
                    for item in game_object.m_Component:
                        if item.component.type.name not in {
                            "BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider"
                        }:
                            continue
                        definition, reason = _collider_definition(item.component, relative, source)
                        if definition is not None:
                            definition["object_name"] = game_object.m_Name
                            definition["collider_flags"] = flags
                            prefab_zones.setdefault(path, []).append(definition)
                        elif reason:
                            excluded[reason] += 1
                except Exception:
                    continue
        prefabs = []
        for path, zones in sorted(prefab_zones.items()):
            zones,removed=_remove_contained(zones)
            excluded["contained_by_larger_same_owner_zone"]+=removed
            if zones:
                prefabs.append({"prefab_path": path, "zones": zones})
        content = identity["bundles"]["content"]
        payload = {
            "schema_version": DATA_SCHEMA_VERSION,
            "source": {
                "rust_build_id": identity.get("rust_build_id"),
                "content_bundle_size": content["size"],
                "content_bundle_mtime_ns": content["mtime_ns"],
                "asset_scene_count": len(scenes),
                "selection": "prevent-building named/tagged colliders affecting building blocks",
                "selection_strategy": "maximal_same_owner_containment",
                "minimum_area_m2": None,
                "allowed_shapes": ["circle", "rectangle"],
                "included_surface_families": sorted(SURFACE_FAMILIES),
            },
            "prefab_count": len(prefabs),
            "zone_definition_count": sum(len(item["zones"]) for item in prefabs),
            "excluded_definition_counts": excluded,
            "prefabs": prefabs,
        }
        target = Path(target_text)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
        conn.send({"ok": True, "path": str(target), "prefab_count": len(prefabs),
                   "zone_count": payload["zone_definition_count"]})
    except BaseException as exc:
        conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()


def refresh_no_build_zone_data(rust_install_path: str | Path,
                               output_path: str | Path | None = None) -> Path:
    """Extract sanitized building-block exclusion geometry from local Rust assets."""
    install = Path(rust_install_path).resolve()
    target = Path(output_path) if output_path is not None else default_no_build_data()
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_extract_worker, args=(str(install), str(target), child))
    process.start()
    child.close()
    result = parent.recv()
    process.join()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "No-build extraction failed"))
    return Path(result["path"])
