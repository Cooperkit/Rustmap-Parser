"""Render and export placed building-block exclusion zones."""

from __future__ import annotations

import json
import math
import time
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .prefabs import PrefabManifest
from .png import save_png
from .tunnels import _instance_matrix


AUTOSPAWN_PREFIX = "assets/bundled/prefabs/autospawn/"
SURFACE_FAMILIES = frozenset(("monument", "power substations", "tunnel-entrance", "tunnel-upwards"))
ORIENTATION = "flip_vertical (reverse Z; preserve X left/right)"


def _autospawn_family(path: str) -> str | None:
    if not path.startswith(AUTOSPAWN_PREFIX):
        return None
    return path[len(AUTOSPAWN_PREFIX):].split("/",1)[0]


def _round(value: float) -> float:
    return round(float(value), 9)


def _vector(values) -> dict:
    return {"x": _round(values[0]), "y": _round(values[1]), "z": _round(values[2])}


def _matrix_transform(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    homogeneous = np.column_stack((points, np.ones(len(points), dtype=np.float64)))
    return (matrix @ homogeneous.T).T[:, :3]


def _decompose(matrix: np.ndarray) -> dict:
    scale = np.linalg.norm(matrix[:3, :3], axis=0)
    rotation = matrix[:3, :3] / np.where(scale == 0.0, 1.0, scale)
    sx = max(-1.0, min(1.0, -float(rotation[1, 2])))
    x = math.asin(sx)
    if abs(math.cos(x)) > 1e-7:
        y = math.atan2(float(rotation[0, 2]), float(rotation[2, 2]))
        z = math.atan2(float(rotation[1, 0]), float(rotation[1, 1]))
    else:
        y = math.atan2(-float(rotation[2, 0]), float(rotation[0, 0]))
        z = 0.0
    return {
        "position": _vector(matrix[:3, 3]),
        "rotation_euler": {
            "x": _round(math.degrees(x)), "y": _round(math.degrees(y) % 360.0),
            "z": _round(math.degrees(z)),
        },
        "scale": _vector(scale),
    }


def _draw_zones(zones: list[dict], resolution: int, fill, outline, width: int,
                world_size: int | None = None) -> Image.Image:
    supersample = 2
    size = resolution * supersample
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    for zone in zones:
        transform, geometry = zone["transform"], zone["geometry"]
        center = transform["image_position"]
        cx, cy = center["x"] * size, center["y"] * size
        pixels_per_metre = size / float(world_size or resolution)
        if zone["shape"] == "circle":
            radius = geometry["radius_m"] * pixels_per_metre
            bounds = (cx-radius, cy-radius, cx+radius, cy+radius)
            draw.ellipse(bounds, fill=tuple(fill), outline=tuple(outline) if width else None,
                         width=width * supersample)
            continue
        heading = math.radians(transform["heading_degrees"])
        half_width = geometry["width_m"] * pixels_per_metre / 2.0
        half_height = geometry["height_m"] * pixels_per_metre / 2.0
        # Image Y reverses world Z. Unity positive yaw rotates +X toward -Z.
        x_axis = (math.cos(heading), math.sin(heading))
        z_axis = (math.sin(heading), -math.cos(heading))
        points = [
            (cx + sx*x_axis[0]*half_width + sz*z_axis[0]*half_height,
             cy + sx*x_axis[1]*half_width + sz*z_axis[1]*half_height)
            for sx, sz in ((-1,-1),(1,-1),(1,1),(-1,1))
        ]
        draw.polygon(points, fill=tuple(fill))
        if width:
            draw.line(points + [points[0]], fill=tuple(outline),
                      width=width * supersample, joint="curve")
    return image.resize((resolution, resolution), Image.Resampling.LANCZOS)


def _load_packaged_data() -> tuple[dict, str]:
    item = resources.files("rustmap_parser.data").joinpath("no_build_zones.json")
    return json.loads(item.read_text(encoding="utf-8")), "rustmap_parser.data/no_build_zones.json"


def build_no_build_export(world, manifest: PrefabManifest, data: dict,
                          resolution: int | None = None) -> tuple[dict, list[dict]]:
    resolution = world.size if resolution is None else int(resolution)
    definitions = {item["prefab_path"].casefold(): item for item in data.get("prefabs", [])}
    candidates = []
    for prefab in world.prefabs:
        entry = manifest.get(prefab.prefab_id)
        path = entry.path.casefold().replace("\\", "/") if entry else ""
        family=_autospawn_family(path)
        if family in SURFACE_FAMILIES and prefab.position is not None:
            candidates.append((path, family, prefab))
    candidates.sort(key=lambda item: (
        item[0], float(item[2].position.x), float(item[2].position.y),
        float(item[2].position.z), int(item[2].prefab_id),
    ))

    zones, skipped, resolved_instances = [], [], 0
    for owner_index, (path, family, prefab) in enumerate(candidates, start=1):
        definition = definitions.get(path)
        if definition is None or not definition.get("zones"):
            skipped.append({"prefab_path": path, "family": family,
                            "reason": "missing_packaged_blocker_geometry"})
            continue
        resolved_instances += 1
        instance = _instance_matrix(prefab)
        for blocker_index, blocker in enumerate(definition["zones"], start=1):
            local = np.asarray(blocker["local_matrix"], dtype=np.float64)
            combined = instance @ local
            center_local = blocker["center"]
            center = _matrix_transform(combined, np.asarray([[
                center_local["x"], center_local["y"], center_local["z"]
            ]], dtype=np.float64))[0]
            decomposed = _decompose(combined)
            decomposed["position"] = _vector(center)
            norm_x = _round(center[0] / world.size + 0.5)
            norm_z = _round(center[2] / world.size + 0.5)
            decomposed["normalized_position"] = {"x": norm_x, "z": norm_z}
            decomposed["image_position"] = {"x": norm_x, "y": _round(1.0 - norm_z)}
            decomposed["heading_degrees"] = decomposed["rotation_euler"]["y"]
            if blocker["shape"] == "circle":
                radial_scale = math.hypot(float(combined[0, 0]), float(combined[2, 0]))
                geometry = {"radius_m": _round(float(blocker["radius"]) * radial_scale)}
                shape = "circle"
            else:
                width_scale = math.hypot(float(combined[0, 0]), float(combined[2, 0]))
                height_scale = math.hypot(float(combined[0, 2]), float(combined[2, 2]))
                geometry = {
                    "width_m": _round(float(blocker["size"]["x"]) * width_scale),
                    "height_m": _round(float(blocker["size"]["z"]) * height_scale),
                }
                shape = "rectangle"
            local_y = blocker.get("local_y_bounds", [-0.5, 0.5])
            y_points = _matrix_transform(instance, np.asarray([
                [0.0, local_y[0], 0.0], [0.0, local_y[1], 0.0]
            ], dtype=np.float64))[:, 1]
            zones.append({
                "zone_id": "", "shape": shape,
                "owner": {
                    "instance_id": f"owner-{owner_index:04d}", "family": family,
                    "prefab_id": int(prefab.prefab_id), "prefab_path": path,
                },
                "transform": decomposed,
                "geometry": geometry,
                "vertical_bounds": {
                    "minimum_y": _round(min(y_points)), "maximum_y": _round(max(y_points)),
                },
                "projected_area_m2": _round(blocker["projected_area_m2"]),
            })
    for index, zone in enumerate(zones, start=1):
        zone["zone_id"] = f"no-build-{index:04d}"

    source = data.get("source", {})
    mismatch = bool(
        source.get("content_bundle_size") is not None and (
            int(source["content_bundle_size"]) != int(manifest.source_size or -1) or
            int(source.get("content_bundle_mtime_ns", -1)) != int(manifest.source_mtime_ns or -1)
        )
    )
    document = {
        "schema_version": 4,
        "status": "rendered",
        "source": source,
        "map": {
            "serialization_version": int(world.serialization_version),
            "timestamp": int(world.timestamp), "world_size": int(world.size),
        },
        "resolution": [resolution, resolution],
        "coordinates": {
            "world": "Unity metres: X east/west, Y elevation, Z north/south",
            "normalized": "x = world_x / world_size + 0.5; z = world_z / world_size + 0.5",
            "image": "x = normalized x; y = 1 - normalized z",
            "orientation": ORIENTATION,
        },
        "selection": {
            "restriction": "building_blocks", "deployable_only_excluded": True,
            "strategy": "maximal_same_owner_containment",
            "minimum_area_m2": None,
            "allowed_shapes": ["circle", "rectangle"],
            "included_surface_families": sorted(SURFACE_FAMILIES),
            "missing_policy": "skip_with_warning",
        },
        "placed_owner_count": len(candidates),
        "resolved_owner_count": resolved_instances,
        "skipped_owner_count": len(skipped),
        "unique_prefab_count": len({path for path, _, _ in candidates}),
        "zone_count": len(zones),
        "retained_projected_area_m2": _round(sum(zone["projected_area_m2"] for zone in zones)),
        "excluded_definition_counts": data.get("excluded_definition_counts", {}),
        "data_version_mismatch": mismatch,
        "warnings": (["no_build_data_version_mismatch"] if mismatch else []) +
                    (["owners_skipped_without_blocker_geometry"] if skipped else []),
        "skipped_owners": skipped,
        "zones": zones,
    }
    return document, zones


def save_no_build_zones(world, manifest_path: str | Path, output_dir: str | Path,
                        resolution: int | None = None,
                        fill_color=(255, 0, 0, 64), outline_color=(255, 0, 0, 255),
                        outline_width: int = 3,
                        terrain_image: str | Path | Image.Image | None = None,
                        export_images: bool = True,
                        export_json: bool = True) -> dict:
    started = time.perf_counter()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = PrefabManifest.load(manifest_path)
    data, data_path = _load_packaged_data()
    document, zones = build_no_build_export(world, manifest, data, resolution)
    image_path = output / "no_build_zones.png"
    overlay_path = output / "no_build_zones_on_map.png"
    json_path = output / "no_build_zones.json"
    document["requested_outputs"] = {"images": export_images, "json": export_json}
    document["packaged_data"] = data_path
    document["style"] = ({
        "fill_color": list(fill_color), "outline_color": list(outline_color),
        "outline_width": outline_width,
    } if export_images else None)
    warnings = document["warnings"]
    document["render_seconds"] = 0.0
    document["image_file"] = None
    document["overlay_file"] = None
    if not export_images:
        for stale in (image_path, overlay_path):
            if stale.is_file():
                stale.unlink()
    else:
        render_started = time.perf_counter()
        image = _draw_zones(zones, document["resolution"][0], fill_color,
                            outline_color, outline_width, world.size)
        document["render_seconds"] = time.perf_counter() - render_started
        save_png(image, image_path)
        document["image_file"] = image_path.name
        if overlay_path.is_file():
            overlay_path.unlink()
        if isinstance(terrain_image, Image.Image):
            base = terrain_image
        else:
            terrain = Path(terrain_image) if terrain_image is not None else None
            if terrain is not None and terrain.is_file():
                with Image.open(terrain) as source:
                    base = source.convert("RGBA")
            else:
                base = None
        if base is not None:
            if base.size == image.size:
                save_png(Image.alpha_composite(base, image), overlay_path)
                document["overlay_file"] = overlay_path.name
            else:
                warnings.append("terrain_resolution_mismatch_overlay_omitted")
        else:
            warnings.append("terrain_image_unavailable_overlay_omitted")
    document["elapsed_seconds"] = time.perf_counter() - started
    document["artifact_sizes_bytes"] = {}
    if document["image_file"]:
        document["artifact_sizes_bytes"][image_path.name] = image_path.stat().st_size
    if overlay_path.is_file() and document["overlay_file"]:
        document["artifact_sizes_bytes"][overlay_path.name] = overlay_path.stat().st_size
    if export_json:
        json_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
        document["artifact_sizes_bytes"][json_path.name] = json_path.stat().st_size
    elif json_path.is_file():
        json_path.unlink()
    return document
