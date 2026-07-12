"""Render Rust's exact final-LOD underground train-tunnel silhouettes."""

from __future__ import annotations

import json
import math
import time
from contextlib import nullcontext
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .prefabs import PrefabManifest
from .png import save_png
from .tunnel_assets import (
    TUNNEL_PREFIX,
    find_rust_install,
    load_or_refresh_tunnel_geometry,
)


def _euler_matrix(euler) -> np.ndarray:
    """Unity Quaternion.Euler-compatible Z-X-Y rotation matrix."""
    if euler is None:
        return np.eye(3, dtype=np.float64)
    x, y, z = (math.radians(float(euler.x)), math.radians(float(euler.y)),
               math.radians(float(euler.z)))
    sx, cx, sy, cy, sz, cz = math.sin(x), math.cos(x), math.sin(y), math.cos(y), math.sin(z), math.cos(z)
    rx = np.array(((1,0,0),(0,cx,-sx),(0,sx,cx)), dtype=np.float64)
    ry = np.array(((cy,0,sy),(0,1,0),(-sy,0,cy)), dtype=np.float64)
    rz = np.array(((cz,-sz,0),(sz,cz,0),(0,0,1)), dtype=np.float64)
    return ry @ rx @ rz


def _instance_matrix(prefab) -> np.ndarray:
    matrix = np.eye(4, dtype=np.float64)
    scale = prefab.scale
    scales = np.array((scale.x, scale.y, scale.z), dtype=np.float64) if scale else np.ones(3)
    matrix[:3, :3] = _euler_matrix(prefab.rotation) @ np.diag(scales)
    matrix[:3, 3] = (prefab.position.x, prefab.position.y, prefab.position.z)
    return matrix


def _write_metadata(output: Path, metadata: dict) -> dict:
    target = output / "tunnels_metadata.json"
    metadata["metadata_file"] = target.name
    target.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")
    return metadata


def _skip(output: Path, reason: str, started: float, details: dict | None = None) -> dict:
    for name in ("tunnels.png", "tunnels_on_map.png"):
        path = output / name
        if path.is_file():
            path.unlink()
    metadata = {
        "schema_version": 2, "layer": "TrainTunnels", "status": "skipped",
        "reason": reason, "render_seconds": time.perf_counter() - started,
        "orientation": "flip_vertical (reverse Z; preserve X left/right)",
    }
    if details:
        metadata.update(details)
    return _write_metadata(output, metadata)


def render_tunnel_map(world, manifest: PrefabManifest, geometry_cache: str | Path | None = None,
                      resolution: int | None = None, opacity: float = 1.0,
                      force_triangles: bool = False):
    """Render exact tunnel meshes, normally by placing pre-rasterized templates."""
    resolution = world.size if resolution is None else resolution
    if resolution <= 0:
        raise ValueError("Tunnel render resolution must be positive")
    if not 0.0 <= opacity <= 1.0:
        raise ValueError("Tunnel overlay opacity must be between 0 and 1")
    cache = Path(geometry_cache) if geometry_cache is not None else None
    if cache is None:
        tile_root = resources.files("rustmap_parser.data.tunnel_tiles")
        cache_metadata = json.loads(tile_root.joinpath("tiles.json").read_text(encoding="utf-8"))
        tile_source = "packaged"
        geometry_path = None
        tile_directory = "rustmap_parser.data.tunnel_tiles"
        def tile_exists(filename): return tile_root.joinpath(filename).is_file()
        def load_tile(filename):
            with tile_root.joinpath(filename).open("rb") as stream, Image.open(stream) as source:
                return np.asarray(source.convert("L"), dtype=np.uint8)
    else:
        cache_metadata = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
        tile_source = "override_cache"
        geometry_path = cache / "geometry.npz"
        tile_directory = str((cache / "templates").resolve())
        def tile_exists(filename): return (cache / filename).is_file()
        def load_tile(filename):
            with Image.open(cache / filename) as source:
                return np.asarray(source.convert("L"), dtype=np.uint8)
    started = time.perf_counter()
    related_instances, rendered_instances = 0, 0
    rendered_paths, related_paths = set(), set()
    loaded_tile_paths, missing_tiles = set(), set()
    triangle_count = fast_instances = fallback_instances = 0
    supersample = 2
    extent = float(world.size)
    intermediate_size = resolution * supersample
    intermediate_ppm = intermediate_size / extent
    mask_values = np.zeros((intermediate_size, intermediate_size), dtype=np.uint8)
    template_prepare_seconds = template_png_load_seconds = paste_seconds = fallback_seconds = 0.0
    cache_load_started = time.perf_counter()

    arrays_context = (
        np.load(geometry_path, allow_pickle=False)
        if geometry_path is not None and geometry_path.is_file()
        else nullcontext(None)
    )
    with arrays_context as arrays:
        cache_load_seconds = time.perf_counter() - cache_load_started
        templates = {item["prefab_path"]: item for item in cache_metadata["templates"]}
        prepared: dict[tuple, tuple[np.ndarray, float, float]] = {}
        fallbacks = []
        for prefab in world.prefabs:
            entry = manifest.get(prefab.prefab_id)
            path = entry.path.casefold().replace("\\", "/") if entry else ""
            if not path.startswith(TUNNEL_PREFIX) or prefab.position is None:
                continue
            related_instances += 1
            related_paths.add(path)
            template = templates.get(path)
            if template is None:
                missing_tiles.add(path)
                continue
            rotation = prefab.rotation
            scale = prefab.scale
            x_angle = float(rotation.x) if rotation else 0.0
            y_angle = float(rotation.y) if rotation else 0.0
            z_angle = float(rotation.z) if rotation else 0.0
            scales = (float(scale.x), float(scale.y), float(scale.z)) if scale else (1.0,1.0,1.0)
            quarter_value = y_angle / 90.0
            cardinal = (abs(x_angle) < 1e-5 and abs(z_angle) < 1e-5 and
                        abs(quarter_value - round(quarter_value)) < 1e-5)
            uniform = (scales[0] > 0 and abs(scales[0]-scales[1]) < 1e-5 and
                       abs(scales[0]-scales[2]) < 1e-5)
            mask_file = template.get("mask_file", "")
            has_mask = bool(template.get("mask_pixels_per_meter") and tile_exists(mask_file))
            if not has_mask:
                missing_tiles.add(path)
            if force_triangles or not (cardinal and uniform and has_mask):
                fallbacks.append((prefab, template))
                continue

            quarters = int(round(quarter_value)) % 4
            uniform_scale = scales[0]
            prepare_key = (path, quarters, round(uniform_scale, 8), round(intermediate_ppm, 10))
            prep_started = time.perf_counter()
            prepared_item = prepared.get(prepare_key)
            if prepared_item is None:
                png_load_started = time.perf_counter()
                try:
                    template_mask = load_tile(mask_file)
                except (OSError, ValueError, SyntaxError):
                    missing_tiles.add(path)
                    fallbacks.append((prefab, template))
                    template_png_load_seconds += time.perf_counter() - png_load_started
                    continue
                loaded_tile_paths.add(path)
                template_png_load_seconds += time.perf_counter() - png_load_started
                if quarters:
                    template_mask = np.rot90(template_mask, k=-quarters)
                source_ppm = float(template["mask_pixels_per_meter"])
                left = float(template["mask_left_x"]) * uniform_scale
                top = float(template["mask_top_z"]) * uniform_scale
                source_height, source_width = template["mask_shape"]
                right = left + (source_width-1) / source_ppm * uniform_scale
                bottom = top - (source_height-1) / source_ppm * uniform_scale
                corners = np.array(((left,top),(right,top),(right,bottom),(left,bottom)))
                angle = math.radians(quarters * 90)
                c, s = math.cos(angle), math.sin(angle)
                rotated_x = c*corners[:,0] + s*corners[:,1]
                rotated_z = -s*corners[:,0] + c*corners[:,1]
                rotated_left, rotated_right = float(rotated_x.min()), float(rotated_x.max())
                rotated_bottom, rotated_top = float(rotated_z.min()), float(rotated_z.max())
                target_left = math.floor(rotated_left*intermediate_ppm) / intermediate_ppm
                target_right = math.ceil(rotated_right*intermediate_ppm) / intermediate_ppm
                target_bottom = math.floor(rotated_bottom*intermediate_ppm) / intermediate_ppm
                target_top = math.ceil(rotated_top*intermediate_ppm) / intermediate_ppm
                target_size = (
                    max(1, int(round((target_right-target_left)*intermediate_ppm))+1),
                    max(1, int(round((target_top-target_bottom)*intermediate_ppm))+1),
                )
                effective_source_ppm = source_ppm / uniform_scale
                output_to_source = effective_source_ppm / intermediate_ppm
                transform = (
                    output_to_source, 0.0, (target_left-rotated_left)*effective_source_ppm,
                    0.0, output_to_source, (rotated_top-target_top)*effective_source_ppm,
                )
                if not (target_size == (template_mask.shape[1], template_mask.shape[0]) and
                        abs(output_to_source-1.0) < 1e-12 and
                        abs(transform[2]) < 1e-12 and abs(transform[5]) < 1e-12):
                    template_mask = np.asarray(Image.fromarray(template_mask).transform(
                        target_size, Image.Transform.AFFINE, transform,
                        resample=Image.Resampling.BICUBIC,
                    ), dtype=np.uint8)
                prepared_item = (template_mask, target_left, target_top)
                prepared[prepare_key] = prepared_item
            template_prepare_seconds += time.perf_counter() - prep_started
            template_mask, local_left, local_top = prepared_item
            destination_x = (prefab.position.x + local_left + extent/2) * intermediate_ppm
            destination_y = (extent/2 - prefab.position.z - local_top) * intermediate_ppm
            if (abs(destination_x-round(destination_x)) > 1e-5 or
                    abs(destination_y-round(destination_y)) > 1e-5):
                fallbacks.append((prefab, template))
                continue
            else:
                paste_started = time.perf_counter()
                x0, y0 = int(round(destination_x)), int(round(destination_y))
                h, w = template_mask.shape
                target_x0, target_y0 = max(0,x0), max(0,y0)
                target_x1, target_y1 = min(intermediate_size,x0+w), min(intermediate_size,y0+h)
                if target_x1 > target_x0 and target_y1 > target_y0:
                    source_x0, source_y0 = target_x0-x0, target_y0-y0
                    source_x1, source_y1 = source_x0+(target_x1-target_x0), source_y0+(target_y1-target_y0)
                    np.maximum(mask_values[target_y0:target_y1,target_x0:target_x1],
                               template_mask[source_y0:source_y1,source_x0:source_x1],
                               out=mask_values[target_y0:target_y1,target_x0:target_x1])
                paste_seconds += time.perf_counter() - paste_started
                fast_instances += 1
            rendered_instances += 1
            rendered_paths.add(path)

        omitted_fallbacks = []
        if fallbacks and arrays is not None:
            fallback_started = time.perf_counter()
            mask = Image.fromarray(mask_values, mode="L")
            draw = ImageDraw.Draw(mask)
            for prefab, template in fallbacks:
                vertices = arrays[template["key"] + "_vertices"]
                triangles = arrays[template["key"] + "_triangles"]
                homogeneous = np.column_stack((vertices, np.ones(len(vertices))))
                world_vertices = (_instance_matrix(prefab) @ homogeneous.T).T[:, :3]
                projected = np.round(np.column_stack((
                    (world_vertices[:,0]+extent/2)*intermediate_ppm,
                    (extent/2-world_vertices[:,2])*intermediate_ppm,
                )), 6)
                for triangle in triangles:
                    points = projected[triangle]
                    first, second = points[1]-points[0], points[2]-points[0]
                    area = abs(first[0]*second[1] - first[1]*second[0])
                    if area <= 0.05:
                        continue
                    draw.polygon(tuple(map(tuple, points)), fill=255)
                    triangle_count += 1
                fallback_instances += 1
                rendered_instances += 1
                rendered_paths.add(template["prefab_path"])
            mask_values = np.asarray(mask, dtype=np.uint8)
            fallback_seconds = time.perf_counter() - fallback_started
        elif fallbacks:
            omitted_fallbacks = sorted({
                templates_path["prefab_path"] for _, templates_path in fallbacks
            })

    source_identity = cache_metadata.get("identity", {})
    source_content = source_identity.get("bundles", {}).get("content", {})
    tile_version_mismatch = bool(
        source_content and (
            int(source_content.get("size", -1)) != manifest.source_size or
            int(source_content.get("mtime_ns", -1)) != manifest.source_mtime_ns
        )
    )

    downsample_started = time.perf_counter()
    mask = Image.fromarray(mask_values, mode="L").resize(
        (resolution, resolution), Image.Resampling.LANCZOS
    )
    downsample_seconds = time.perf_counter() - downsample_started
    if opacity != 1.0:
        mask = mask.point(lambda value: round(value * opacity))
    image = Image.new("RGBA", (resolution, resolution), (211, 214, 214, 0))
    image.putalpha(mask)
    metadata = {
        "resolution": [resolution, resolution], "world_size": world.size,
        "orientation": "flip_vertical (reverse Z; preserve X left/right)",
        "related_instance_count": related_instances,
        "train_layer_instance_count": rendered_instances,
        "related_unique_prefab_count": len(related_paths),
        "train_layer_unique_prefab_count": len(rendered_paths),
        "non_train_layer_prefabs": sorted(related_paths - rendered_paths),
        "rasterized_triangle_count": triangle_count,
        "render_mode": "triangles" if force_triangles else "cached_templates",
        "fast_path_instance_count": fast_instances,
        "fallback_instance_count": fallback_instances,
        "omitted_fallback_instance_count": len(fallbacks) - fallback_instances,
        "omitted_fallback_prefabs": omitted_fallbacks,
        "prepared_template_count": len(prepared),
        "cache_schema_version": cache_metadata.get("schema_version"),
        "cache_pixels_per_meter": cache_metadata.get("template_pixels_per_meter"),
        "cache_template_raster_seconds": cache_metadata.get("template_raster_seconds"),
        "intermediate_pixels_per_meter": intermediate_ppm,
        "cache_load_seconds": cache_load_seconds,
        "template_prepare_seconds": template_prepare_seconds,
        "template_png_load_seconds": template_png_load_seconds,
        "template_paste_seconds": paste_seconds,
        "fallback_raster_seconds": fallback_seconds,
        "downsample_seconds": downsample_seconds,
        "alpha_nonzero_pixels": int(np.count_nonzero(np.asarray(mask))),
        "render_seconds": time.perf_counter() - started,
        "cache_template_count": cache_metadata["template_count"],
        "tile_source": tile_source,
        "tile_directory": tile_directory,
        "loaded_tile_count": len(loaded_tile_paths),
        "missing_tiles": sorted(missing_tiles),
        "fallback_geometry_available": arrays is not None,
        "tile_source_build_id": source_identity.get("rust_build_id"),
        "tile_version_mismatch": tile_version_mismatch,
        "tile_warnings": (["tile_version_mismatch"] if tile_version_mismatch else []) +
                         (["instances_omitted_without_fallback_geometry"] if omitted_fallbacks else []),
    }
    return image, metadata


def save_tunnel_render(world, manifest_path: str | Path, output_dir: str | Path,
                       rust_install_path: str | Path | None = None,
                       resolution: int | None = None,
                       cache_path: str | Path | None = None,
                       overlay_opacity: float = 1.0,
                       tint_color: tuple[int, int, int, int] = (50, 45, 105, 104),
                       terrain_image: str | Path | Image.Image | None = None,
                       export_layer: bool = True,
                       export_overlay: bool = True) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    manifest = PrefabManifest.load(manifest_path)
    install = find_rust_install(rust_install_path) if rust_install_path is not None else None
    cache = None
    try:
        if cache_path is not None:
            requested_cache = Path(cache_path)
            if (requested_cache / "metadata.json").is_file():
                cache = requested_cache
            elif install is not None:
                cache = load_or_refresh_tunnel_geometry(install, requested_cache)
            else:
                return _skip(output, "override_cache_unavailable", started, {
                    "requested_cache": str(requested_cache),
                    "error": "The override is not a completed versioned cache and no Rust install was supplied",
                })
        image, render_metadata = render_tunnel_map(
            world, manifest, cache, resolution, 1.0
        )
    except Exception as exc:
        return _skip(output, "geometry_extraction_or_render_failed", started, {
            "error": f"{type(exc).__name__}: {exc}",
            "rust_install": str(install) if install else None,
        })

    image_path = output / "tunnels.png"
    if not export_layer and image_path.is_file():
        image_path.unlink()
    png_encode_seconds = 0.0
    if export_layer:
        png_started = time.perf_counter()
        save_png(image, image_path)
        png_encode_seconds = time.perf_counter() - png_started
    overlay_output_path = output / "tunnels_on_map.png"
    if overlay_output_path.is_file():
        overlay_output_path.unlink()
    overlay_path = None
    overlay_warning = None
    overlay_compose_seconds = overlay_encode_seconds = 0.0
    if not export_overlay:
        terrain = None
        overlay_warning = "overlay export disabled"
    elif isinstance(terrain_image, Image.Image):
        terrain = terrain_image
    elif terrain_image is not None and Path(terrain_image).is_file():
        with Image.open(terrain_image) as source:
            terrain = source.convert("RGBA")
    else:
        terrain = None
    if terrain is not None:
        if terrain.size == image.size:
            compose_started = time.perf_counter()
            tint = Image.new("RGBA", terrain.size, tint_color)
            tinted_terrain = Image.alpha_composite(terrain, tint)
            overlay_layer = image.copy()
            if overlay_opacity != 1.0:
                alpha = overlay_layer.getchannel("A").point(
                    lambda value: round(value * overlay_opacity)
                )
                overlay_layer.putalpha(alpha)
            overlay = Image.alpha_composite(tinted_terrain, overlay_layer)
            overlay_compose_seconds = time.perf_counter() - compose_started
            overlay_path = overlay_output_path
            overlay_encode_started = time.perf_counter()
            save_png(overlay, overlay_path)
            overlay_encode_seconds = time.perf_counter() - overlay_encode_started
        else:
            overlay_warning = f"terrain image dimensions {terrain.size} do not match {image.size}"
    elif export_overlay:
        overlay_warning = "full-size terrain image was not available"

    metadata = {
        "schema_version": 2, "layer": "TrainTunnels", "status": "rendered",
        "source": "pre-rasterized DungeonGridCell final-LOD puzzle pieces",
        "rust_install": str(install) if install else None,
        "geometry_cache": str(cache) if cache else None,
        "requested_outputs": {"layer": export_layer, "overlay": export_overlay},
        "image_file": image_path.name if export_layer else None,
        "image_size_bytes": image_path.stat().st_size if export_layer else None,
        "overlay_file": overlay_path.name if overlay_path else None,
        "overlay_size_bytes": overlay_path.stat().st_size if overlay_path else None,
        "overlay_warning": overlay_warning,
        "overlay_opacity": overlay_opacity,
        "terrain_tint_rgba": list(tint_color),
        "png_encode_seconds": png_encode_seconds,
        "overlay_compose_seconds": overlay_compose_seconds,
        "overlay_encode_seconds": overlay_encode_seconds,
        **render_metadata,
        "total_seconds": time.perf_counter() - started,
    }
    return _write_metadata(output, metadata)
