"""Composable Rust map export pipeline."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image

from .cargo import save_cargo_ship_path
from .config import ExportConfig, ExportResult
from .layers import generate_diagnostics
from .monuments import save_monuments
from .no_build import save_no_build_zones
from .parser import load_map
from .png import save_png
from .populations import SpawnFilterEvaluator
from .renderer import save_map_render
from .tunnels import save_tunnel_render


ORIENTATION = "flip_vertical (reverse Z rows; preserve X left/right)"
HEATMAP_PREVIEW_DIRECTORY = "Heatmap-previews"


def export_orientation(values: np.ndarray) -> np.ndarray:
    """Convert native ``[z,x]`` arrays to image rows without mirroring X."""
    return np.ascontiguousarray(values[::-1, :])


def _stats(values: np.ndarray, populations: list[str]) -> dict:
    return {
        "populations": populations,
        "nonzero_pixels": int(np.count_nonzero(values)),
        "minimum": int(values.min()), "maximum": int(values.max()),
        "mean": float(values.mean()),
    }


def _write_metadata(path: Path, metadata: dict, artifacts: dict[str, int]) -> None:
    artifacts[path.name] = 0
    for _ in range(4):
        rendered = json.dumps(metadata, indent=2) + "\n"
        size = len(rendered.encode("utf-8"))
        if artifacts[path.name] == size:
            break
        artifacts[path.name] = size
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")


def _generate(config: ExportConfig, rules_path: Path | None,
              manifest_path: Path | None) -> dict:
    started = time.perf_counter()
    output = config.output_dir
    output.mkdir(parents=True, exist_ok=True)
    exports = config.exports
    timings: dict[str, object] = {"heatmap_categories": {}}

    stage = time.perf_counter()
    world = load_map(config.map_path)
    timings["map_parse_seconds"] = time.perf_counter() - stage

    diagnostics_dir = output / "diagnostics"
    diagnostics_pool = ThreadPoolExecutor(max_workers=1) if exports.diagnostics else None
    diagnostics_started = time.perf_counter()
    diagnostics_future = (
        diagnostics_pool.submit(generate_diagnostics, world, diagnostics_dir)
        if diagnostics_pool else None
    )

    heatmap_options = exports.heatmaps
    heatmap_categories: dict[str, dict] = {}
    npz_path: Path | None = None
    preview_dir: Path | None = None
    heatmap_resolution: int | None = None
    rule_database_metadata = None
    if heatmap_options is not None:
        heatmap_resolution = heatmap_options.resolved_resolution(world.size)
        if rules_path is None:
            raise RuntimeError("Heatmap export requires a spawn-rule database")
        stage = time.perf_counter()
        database = json.loads(rules_path.read_text(encoding="utf-8"))
        timings["rule_database_load_seconds"] = time.perf_counter() - stage
        rules = {rule["asset_path"]: rule for rule in database["rules"]}
        rule_database_metadata = {
            "schema_version": database.get("schema_version"),
            "sources": database.get("sources", {}),
        }
        stage = time.perf_counter()
        evaluator = SpawnFilterEvaluator(world, heatmap_resolution)
        timings["heatmap_evaluator_setup_seconds"] = time.perf_counter() - stage
        arrays: dict[str, np.ndarray] = {}
        preview_dir = output / HEATMAP_PREVIEW_DIRECTORY
        preview_pool = ThreadPoolExecutor(max_workers=4) if heatmap_options.previews else None
        preview_futures = []
        if preview_pool:
            preview_dir.mkdir(parents=True, exist_ok=True)
        for category, asset_paths in database["heatmap_categories"].items():
            category_started = time.perf_counter()
            combined = np.zeros(
                (heatmap_resolution, heatmap_resolution), dtype=np.uint8
            )
            used = []
            for asset_path in asset_paths:
                rule = rules.get(asset_path)
                if rule is None or not rule.get("active"):
                    continue
                np.maximum(combined, evaluator.evaluate(rule), out=combined)
                used.append(rule["name"])
            exported = export_orientation(combined)
            arrays[category] = exported
            heatmap_categories[category] = _stats(exported, used)
            if preview_pool:
                preview_futures.append(preview_pool.submit(
                    save_png,
                    Image.fromarray(exported, mode="L"),
                    preview_dir / f"{category}.png",
                ))
            timings["heatmap_categories"][category] = time.perf_counter() - category_started
        timings["heatmaps_total_seconds"] = sum(timings["heatmap_categories"].values())
        npz_path = output / "heatmaps.npz"
        stage = time.perf_counter()
        np.savez_compressed(npz_path, **arrays)
        timings["heatmap_npz_encode_seconds"] = time.perf_counter() - stage
        if preview_pool:
            for future in preview_futures:
                future.result()
            preview_pool.shutdown()
    else:
        timings.update({
            "rule_database_load_seconds": 0.0,
            "heatmap_evaluator_setup_seconds": 0.0,
            "heatmaps_total_seconds": 0.0,
            "heatmap_npz_encode_seconds": 0.0,
        })

    if diagnostics_future:
        diagnostic_stats = diagnostics_future.result()
        diagnostics_pool.shutdown()
        timings["diagnostics_seconds"] = time.perf_counter() - diagnostics_started
    else:
        diagnostic_stats = {}
        timings["diagnostics_seconds"] = 0.0

    monuments_path = output / "monuments.json"
    stage = time.perf_counter()
    if exports.monuments:
        if manifest_path is None:
            raise RuntimeError("Monument export requires a prefab manifest")
        monument_data = save_monuments(world, manifest_path, monuments_path)
    else:
        monument_data = {"monument_count": 0, "unique_prefab_count": 0}
    timings["monuments_seconds"] = time.perf_counter() - stage

    terrain_options = exports.terrain
    render_metadata = None
    if terrain_options is not None:
        stage = time.perf_counter()
        render_metadata = save_map_render(
            world, output, terrain_options.scale, terrain_options.ocean_margin,
            terrain_options.formats, terrain_options.debug, terrain_options.full_size,
            terrain_options.tiles is not None,
            terrain_options.tiles.size if terrain_options.tiles else 512,
        )
        timings["map_render_seconds"] = time.perf_counter() - stage
    else:
        timings["map_render_seconds"] = 0.0

    terrain_path = (
        output / "map_render_full.png"
        if terrain_options is not None and terrain_options.full_size else None
    )
    terrain_image = None
    if terrain_path is not None and terrain_path.is_file():
        with Image.open(terrain_path) as source:
            terrain_image = source.convert("RGBA")

    no_build_options = exports.no_build_zones
    tunnel_options = exports.tunnels
    cargo_options = exports.cargo_ship_path

    def run_no_build():
        if manifest_path is None:
            raise RuntimeError("No-build export requires a prefab manifest")
        stage_started = time.perf_counter()
        value = save_no_build_zones(
            world, manifest_path, output,
            resolution=no_build_options.resolution,
            fill_color=no_build_options.fill_color,
            outline_color=no_build_options.outline_color,
            outline_width=no_build_options.outline_width,
            terrain_image=terrain_image,
            export_images=no_build_options.export_images,
            export_json=no_build_options.export_json,
        )
        return value, time.perf_counter() - stage_started

    def run_tunnels():
        if manifest_path is None:
            raise RuntimeError("Tunnel export requires a prefab manifest")
        stage_started = time.perf_counter()
        value = save_tunnel_render(
            world, manifest_path, output,
            resolution=tunnel_options.resolution,
            overlay_opacity=tunnel_options.overlay_opacity,
            tint_color=tunnel_options.tint_color,
            terrain_image=terrain_image,
            export_layer=tunnel_options.export_layer,
            export_overlay=tunnel_options.export_overlay,
        )
        return value, time.perf_counter() - stage_started

    def run_cargo():
        if manifest_path is None:
            raise RuntimeError("Cargo-ship path export requires a prefab manifest")
        stage_started = time.perf_counter()
        value = save_cargo_ship_path(
            world, manifest_path, output,
            resolution=cargo_options.resolution,
            patrol_color=cargo_options.patrol_color,
            harbor_color=cargo_options.harbor_color,
            line_width=cargo_options.line_width,
            smooth_patrol=cargo_options.smooth_patrol,
            terrain_image=terrain_image,
            export_layer=cargo_options.export_layer,
            export_overlay=cargo_options.export_overlay,
            export_json=cargo_options.export_json,
        )
        return value, time.perf_counter() - stage_started

    jobs = {}
    if no_build_options is not None:
        jobs["no_build_zones"] = run_no_build
    if tunnel_options is not None:
        jobs["tunnels"] = run_tunnels
    if cargo_options is not None:
        jobs["cargo_ship_path"] = run_cargo
    results = {}
    if len(jobs) == 1:
        name, function = next(iter(jobs.items()))
        results[name] = function()
    elif jobs:
        with ThreadPoolExecutor(max_workers=min(3, len(jobs))) as pool:
            futures = {name: pool.submit(function) for name, function in jobs.items()}
            results = {name: future.result() for name, future in futures.items()}
    no_build_metadata, timings["no_build_zones_seconds"] = results.get(
        "no_build_zones", ({"status": "disabled", "zone_count": 0}, 0.0)
    )
    tunnel_metadata, timings["tunnel_render_seconds"] = results.get(
        "tunnels", ({"status": "disabled"}, 0.0)
    )
    cargo_metadata, timings["cargo_ship_path_seconds"] = results.get(
        "cargo_ship_path", ({"status": "disabled", "patrol": {"node_count": 0}}, 0.0)
    )
    if terrain_image is not None:
        terrain_image.close()

    metadata_path = output / "export_metadata.json"
    metadata = {
        "schema_version": 2,
        "map": {
            "path": str(config.map_path),
            "world_size": int(world.size),
            "serialization_version": int(world.serialization_version),
            "timestamp": int(world.timestamp),
        },
        "orientation": ORIENTATION,
        "enabled_outputs": {
            "heatmaps": heatmap_options is not None,
            "diagnostics": exports.diagnostics,
            "monuments": exports.monuments,
            "terrain": terrain_options is not None,
            "tunnels": tunnel_options is not None,
            "no_build_zones": no_build_options is not None,
            "cargo_ship_path": cargo_options is not None,
        },
        "heatmaps": {
            "file": npz_path.name if npz_path else None,
            "resolution": heatmap_resolution,
            "requested_resolution": heatmap_options.resolution if heatmap_options else None,
            "resolution_mode": (
                "world_size" if heatmap_options and heatmap_options.resolution is None
                else "explicit" if heatmap_options else None
            ),
            "dtype": "uint8" if heatmap_options else None,
            "format": "npz_compressed" if heatmap_options else None,
            "preview_directory": preview_dir.name if preview_dir else None,
            "rule_database": rule_database_metadata,
            "categories": heatmap_categories,
        },
        "diagnostics": {
            "directory": diagnostics_dir.name if exports.diagnostics else None,
            "native_shapes": {
                name: details.get("shape")
                for name, details in diagnostic_stats.get("layers", {}).items()
                if isinstance(details, dict) and details.get("shape") is not None
            },
            "orientation_validation": diagnostic_stats.get("orientation_validation"),
        },
        "monuments": {
            "file": monuments_path.name if exports.monuments else None,
            "count": int(monument_data["monument_count"]),
            "unique_prefab_count": int(monument_data["unique_prefab_count"]),
        },
        "terrain": render_metadata,
        "tunnels": tunnel_metadata,
        "no_build_zones": no_build_metadata,
        "cargo_ship_path": cargo_metadata,
        "timings": timings,
    }
    artifacts: dict[str, int] = {}
    if npz_path:
        artifacts[npz_path.name] = npz_path.stat().st_size
    if preview_dir:
        artifacts[f"{preview_dir.name}-total"] = sum(p.stat().st_size for p in preview_dir.glob("*.png"))
    if exports.diagnostics:
        artifacts["diagnostics-total"] = sum(p.stat().st_size for p in diagnostics_dir.glob("*.*"))
    if exports.monuments:
        artifacts[monuments_path.name] = monuments_path.stat().st_size
    if render_metadata:
        artifacts.update({str(name): int(size) for name, size in render_metadata.get("artifacts", {}).items()})
    for enabled, names in (
        (tunnel_options is not None, ("tunnels.png", "tunnels_on_map.png", "tunnels_metadata.json")),
        (no_build_options is not None, ("no_build_zones.png", "no_build_zones_on_map.png", "no_build_zones.json")),
        (cargo_options is not None, ("cargo_ship_path.png", "cargo_ship_path_on_map.png", "cargo_ship_path.json")),
    ):
        if enabled:
            for name in names:
                path = output / name
                if path.is_file():
                    artifacts[name] = path.stat().st_size
    elapsed = time.perf_counter() - started
    metadata["generation"] = {"elapsed_seconds": elapsed, "artifact_sizes_bytes": artifacts}
    _write_metadata(metadata_path, metadata, artifacts)
    elapsed = time.perf_counter() - started
    metadata["generation"]["elapsed_seconds"] = elapsed
    _write_metadata(metadata_path, metadata, artifacts)

    if config.timing_debug:
        print("\nRust map export timing breakdown")
        print("-" * 48)
        labels = (
            "map_parse_seconds", "rule_database_load_seconds",
            "heatmap_evaluator_setup_seconds", "heatmaps_total_seconds",
            "heatmap_npz_encode_seconds", "diagnostics_seconds",
            "monuments_seconds", "map_render_seconds",
            "no_build_zones_seconds", "tunnel_render_seconds",
            "cargo_ship_path_seconds",
        )
        for label in labels:
            print(f"{label.removesuffix('_seconds').replace('_', ' '):32s} {float(timings[label]):8.3f}s")
        if timings["heatmap_categories"]:
            print("  Slowest heatmap categories:")
            for name, seconds in sorted(
                timings["heatmap_categories"].items(), key=lambda item: item[1], reverse=True
            )[:8]:
                print(f"    {name:28s} {seconds:8.3f}s")
        cargo_generation_timings = cargo_metadata.get("generation", {}).get("timings", {})
        if cargo_generation_timings:
            print("  Cargo path generation:")
            for name, seconds in cargo_generation_timings.items():
                print(f"    {name.removesuffix('_seconds').replace('_', ' '):28s} {float(seconds):8.3f}s")
        print("-" * 48)
        print(f"{'total':32s} {elapsed:8.3f}s\n")
    return metadata


class RustMapExporter:
    """Run selected map export stages from a validated configuration."""

    def __init__(self, config: ExportConfig):
        self.config = config.validated()

    def run(self) -> ExportResult:
        config = self.config
        exports = config.exports
        needs_rules = exports.heatmaps is not None
        needs_manifest = bool(
            exports.monuments or exports.tunnels or exports.no_build_zones or
            exports.cargo_ship_path
        )
        with ExitStack() as stack:
            rules = None
            if needs_rules:
                rules = config.data.spawn_rules_path
                if rules is None:
                    rules = stack.enter_context(resources.as_file(
                        resources.files("rustmap_parser.data").joinpath("spawn_rules.json")
                    ))
            manifest = None
            if needs_manifest:
                manifest = config.data.prefab_manifest_path
                if manifest is None:
                    manifest = stack.enter_context(resources.as_file(
                        resources.files("rustmap_parser.data").joinpath("prefab_manifest.json")
                    ))
            metadata = _generate(config, Path(rules) if rules else None,
                                 Path(manifest) if manifest else None)

        output = config.output_dir
        heatmaps = exports.heatmaps
        terrain = exports.terrain
        tiles = terrain.tiles if terrain else None
        tunnel_options = exports.tunnels
        no_build_options = exports.no_build_zones
        cargo_options = exports.cargo_ship_path
        tunnel_metadata = metadata["tunnels"]
        no_build_metadata = metadata["no_build_zones"]
        cargo_metadata = metadata["cargo_ship_path"]
        return ExportResult(
            output_dir=output,
            world_size=int(metadata["map"]["world_size"]),
            elapsed_seconds=float(metadata["generation"]["elapsed_seconds"]),
            metadata_file=output / "export_metadata.json",
            metadata=metadata,
            heatmap_categories=tuple(sorted(metadata["heatmaps"]["categories"])),
            heatmaps_file=(output / metadata["heatmaps"]["file"]) if heatmaps else None,
            monuments_file=(output / "monuments.json") if exports.monuments else None,
            monument_count=int(metadata["monuments"]["count"]),
            map_image=(output / "map_render.png")
                if terrain and not terrain.full_size and "png" in terrain.formats else None,
            full_map_image=(output / "map_render_full.png")
                if terrain and terrain.full_size else None,
            map_tiles_dir=(output / "map_render_tiles") if tiles else None,
            map_tiles_metadata_file=(output / "map_render_tiles" / "tiles.json") if tiles else None,
            map_tile_count=int(((metadata["terrain"] or {}).get("full_size_tiles") or {}).get("tile_count", 0)),
            diagnostics_dir=(output / "diagnostics") if exports.diagnostics else None,
            tunnels_image=(output / "tunnels.png")
                if (tunnel_metadata.get("status") == "rendered" and
                    tunnel_options and tunnel_options.export_layer) else None,
            tunnels_overlay_image=(output / "tunnels_on_map.png")
                if tunnel_metadata.get("overlay_file") else None,
            tunnels_metadata_file=(output / "tunnels_metadata.json") if exports.tunnels else None,
            tunnel_render_status=str(tunnel_metadata.get("status", "disabled")),
            no_build_zones_image=(output / "no_build_zones.png")
                if no_build_options and no_build_options.export_images else None,
            no_build_zones_overlay_image=(output / "no_build_zones_on_map.png")
                if no_build_metadata.get("overlay_file") else None,
            no_build_zones_file=(output / "no_build_zones.json")
                if no_build_options and no_build_options.export_json else None,
            no_build_zone_count=int(no_build_metadata.get("zone_count", 0)),
            no_build_zone_status=str(no_build_metadata.get("status", "disabled")),
            cargo_ship_path_image=(output / "cargo_ship_path.png")
                if cargo_options and cargo_options.export_layer else None,
            cargo_ship_path_overlay_image=(output / "cargo_ship_path_on_map.png")
                if cargo_metadata.get("overlay_file") else None,
            cargo_ship_path_file=(output / "cargo_ship_path.json")
                if cargo_options and cargo_options.export_json else None,
            cargo_ship_path_node_count=int(
                cargo_metadata.get("patrol", {}).get("node_count", 0)
            ),
            cargo_ship_path_status=str(cargo_metadata.get("status", "disabled")),
        )
