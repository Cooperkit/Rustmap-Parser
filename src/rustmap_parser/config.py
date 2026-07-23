"""Composable typed configuration and result objects for Rust map exports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


Color = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DataOptions:
    """Optional overrides for the versioned data bundled with ``rustmap_parser``."""

    spawn_rules_path: Path | None = None
    prefab_manifest_path: Path | None = None


@dataclass(frozen=True, slots=True)
class HeatmapOptions:
    resolution: int | None = 2048
    previews: bool = True

    def resolved_resolution(self, world_size: int) -> int:
        """Return the configured pixel size, or one pixel per world metre."""
        return int(world_size if self.resolution is None else self.resolution)


@dataclass(frozen=True, slots=True)
class DiagnosticsOptions:
    """Diagnostic PNG sizing; ``None`` means one pixel per world metre."""

    resolution: int | None = None

    def resolved_resolution(self, world_size: int) -> int:
        return int(world_size if self.resolution is None else self.resolution)


@dataclass(frozen=True, slots=True)
class TileOptions:
    size: int = 512


@dataclass(frozen=True, slots=True)
class TerrainOptions:
    scale: float = 0.5
    ocean_margin: int = 0
    formats: str | Sequence[str] = ("png", "jpg")
    full_size: bool = True
    tiles: TileOptions | None = None
    debug: bool = False


@dataclass(frozen=True, slots=True)
class TunnelOptions:
    resolution: int | None = None
    overlay_opacity: float = 1.0
    export_layer: bool = True
    export_overlay: bool = True
    tint_color: Color = (50, 45, 105, 104)


@dataclass(frozen=True, slots=True)
class NoBuildZoneOptions:
    resolution: int | None = None
    fill_color: Color = (255, 0, 0, 64)
    outline_color: Color = (255, 0, 0, 255)
    outline_width: int = 3
    export_images: bool = True
    export_json: bool = True


@dataclass(frozen=True, slots=True)
class CargoShipPathOptions:
    """Cargo patrol-loop and harbor-approach export settings."""

    resolution: int | None = None
    patrol_color: Color = (62, 203, 255, 255)
    harbor_color: Color = (255, 184, 61, 255)
    line_width: int = 4
    smooth_patrol: bool = True
    export_layer: bool = True
    export_overlay: bool = True
    export_json: bool = True


@dataclass(frozen=True, slots=True)
class TransformOptions:
    """Select coordinate representations in every exported JSON document."""

    local_position: bool = True
    position: bool = True
    map_position: bool = True


@dataclass(frozen=True, slots=True)
class MonumentOptions:
    """Optional monument detail sidecars; every field defaults to off."""

    interactable: bool = False
    puzzles: bool = False
    loot: bool = False
    radiation_zones: bool = False


# Compatibility alias for callers using the former monument-only type name.
MonumentTransformOptions = TransformOptions


@dataclass(frozen=True, slots=True)
class ExportOptions:
    """Select outputs. ``None``/``False`` means that stage does not run."""

    heatmaps: HeatmapOptions | None = None
    diagnostics: bool | DiagnosticsOptions = False
    monuments: bool | MonumentOptions = False
    terrain: TerrainOptions | None = None
    tunnels: TunnelOptions | None = None
    no_build_zones: NoBuildZoneOptions | None = None
    cargo_ship_path: CargoShipPathOptions | None = None
    transforms: TransformOptions = field(default_factory=TransformOptions)

    @classmethod
    def all(cls) -> "ExportOptions":
        return cls(
            heatmaps=HeatmapOptions(), diagnostics=True,
            monuments=MonumentOptions(
                interactable=True, puzzles=True, loot=True,
                radiation_zones=True,
            ),
            terrain=TerrainOptions(tiles=TileOptions()), tunnels=TunnelOptions(),
            no_build_zones=NoBuildZoneOptions(),
            cargo_ship_path=CargoShipPathOptions(),
        )

    @classmethod
    def map_only(cls, *, tiles: bool = False,
                 tile_size: int = 512) -> "ExportOptions":
        tile_options = TileOptions(tile_size) if tiles else None
        return cls(terrain=TerrainOptions(tiles=tile_options))

    @classmethod
    def heatmaps_only(cls, *, resolution: int | None = 2048,
                      previews: bool = True) -> "ExportOptions":
        return cls(heatmaps=HeatmapOptions(resolution, previews))


@dataclass(frozen=True, slots=True)
class ExportConfig:
    map_path: Path
    output_dir: Path
    exports: ExportOptions = field(default_factory=ExportOptions.all)
    data: DataOptions = field(default_factory=DataOptions)
    timing_debug: bool = False
    status_updates: bool = False

    def validated(self) -> "ExportConfig":
        map_path = Path(self.map_path)
        output_dir = Path(self.output_dir)
        if not map_path.is_file():
            raise FileNotFoundError(f"Rust map not found: {map_path}")
        exports = self.exports
        transforms = TransformOptions(
            local_position=bool(exports.transforms.local_position),
            position=bool(exports.transforms.position),
            map_position=bool(exports.transforms.map_position),
        )
        monuments_enabled = bool(exports.monuments)
        if isinstance(exports.monuments, MonumentOptions):
            monument_options = MonumentOptions(
                interactable=bool(exports.monuments.interactable),
                puzzles=bool(exports.monuments.puzzles),
                loot=bool(exports.monuments.loot),
                radiation_zones=bool(exports.monuments.radiation_zones),
            )
        else:
            monument_options = MonumentOptions()
        if not any((exports.heatmaps, exports.diagnostics, monuments_enabled,
                    exports.terrain, exports.tunnels, exports.no_build_zones,
                    exports.cargo_ship_path)):
            raise ValueError("At least one export output must be enabled")

        heatmaps = exports.heatmaps
        if (heatmaps is not None and heatmaps.resolution is not None and
                heatmaps.resolution <= 0):
            raise ValueError("heatmap resolution must be positive")

        diagnostics = exports.diagnostics
        if isinstance(diagnostics, DiagnosticsOptions):
            if diagnostics.resolution is not None and diagnostics.resolution <= 0:
                raise ValueError("diagnostics resolution must be positive")
            diagnostics = DiagnosticsOptions(resolution=diagnostics.resolution)
        else:
            diagnostics = bool(diagnostics)

        terrain = exports.terrain
        if terrain is not None:
            if terrain.ocean_margin < 0:
                raise ValueError("terrain ocean_margin cannot be negative")
            requested_formats = (
                (terrain.formats,) if isinstance(terrain.formats, str)
                else terrain.formats
            )
            formats = tuple(str(item).strip().casefold() for item in requested_formats)
            unsupported = set(formats) - {"png", "jpg", "jpeg"}
            if unsupported:
                raise ValueError(f"Unsupported terrain formats: {sorted(unsupported)}")
            if not formats and not terrain.full_size:
                raise ValueError("Terrain export needs a scaled format or full_size=True")
            if terrain.tiles is not None:
                if not terrain.full_size:
                    raise ValueError("Terrain tiles require full_size=True")
                if terrain.tiles.size <= 0:
                    raise ValueError("terrain tile size must be positive")
            terrain = TerrainOptions(
                scale=terrain.scale, ocean_margin=terrain.ocean_margin,
                formats=formats, full_size=terrain.full_size,
                tiles=terrain.tiles, debug=terrain.debug,
            )

        tunnels = exports.tunnels
        if tunnels is not None:
            if tunnels.resolution is not None and tunnels.resolution <= 0:
                raise ValueError("tunnel resolution must be positive")
            if not 0.0 <= tunnels.overlay_opacity <= 1.0:
                raise ValueError("tunnel overlay_opacity must be between 0 and 1")
            if (len(tunnels.tint_color) != 4 or
                    any(not 0 <= int(channel) <= 255 for channel in tunnels.tint_color)):
                raise ValueError("tunnel tint_color must contain four channels from 0 to 255")
            if not tunnels.export_layer and not tunnels.export_overlay:
                raise ValueError("Tunnel export needs export_layer or export_overlay")
            if (tunnels.export_overlay and not tunnels.export_layer and
                    (terrain is None or not terrain.full_size)):
                raise ValueError("Tunnel overlay-only export requires full-size terrain")
            tunnels = TunnelOptions(
                resolution=tunnels.resolution,
                overlay_opacity=tunnels.overlay_opacity,
                tint_color=tuple(map(int, tunnels.tint_color)),
                export_layer=bool(tunnels.export_layer),
                export_overlay=bool(tunnels.export_overlay),
            )

        no_build = exports.no_build_zones
        if no_build is not None:
            if no_build.resolution is not None and no_build.resolution <= 0:
                raise ValueError("no-build resolution must be positive")
            if no_build.outline_width < 0:
                raise ValueError("no-build outline_width cannot be negative")
            if not no_build.export_images and not no_build.export_json:
                raise ValueError("No-build export needs export_images or export_json")
            for label, color in (("fill_color", no_build.fill_color),
                                 ("outline_color", no_build.outline_color)):
                if len(color) != 4 or any(not 0 <= int(channel) <= 255 for channel in color):
                    raise ValueError(f"no-build {label} must contain four channels from 0 to 255")
            no_build = NoBuildZoneOptions(
                resolution=no_build.resolution,
                fill_color=tuple(map(int, no_build.fill_color)),
                outline_color=tuple(map(int, no_build.outline_color)),
                outline_width=no_build.outline_width,
                export_images=bool(no_build.export_images),
                export_json=bool(no_build.export_json),
            )

        cargo = exports.cargo_ship_path
        if cargo is not None:
            if cargo.resolution is not None and cargo.resolution <= 0:
                raise ValueError("cargo-ship path resolution must be positive")
            if cargo.line_width <= 0:
                raise ValueError("cargo-ship path line_width must be positive")
            if not cargo.export_layer and not cargo.export_overlay and not cargo.export_json:
                raise ValueError(
                    "Cargo-ship path export needs export_layer, export_overlay, or export_json"
                )
            if (cargo.export_overlay and not cargo.export_layer and
                    (terrain is None or not terrain.full_size)):
                raise ValueError(
                    "Cargo-ship path overlay-only export requires full-size terrain"
                )
            for label, color in (("patrol_color", cargo.patrol_color),
                                 ("harbor_color", cargo.harbor_color)):
                if len(color) != 4 or any(not 0 <= int(channel) <= 255 for channel in color):
                    raise ValueError(
                        f"cargo-ship path {label} must contain four channels from 0 to 255"
                    )
            cargo = CargoShipPathOptions(
                resolution=cargo.resolution,
                patrol_color=tuple(map(int, cargo.patrol_color)),
                harbor_color=tuple(map(int, cargo.harbor_color)),
                line_width=int(cargo.line_width),
                smooth_patrol=bool(cargo.smooth_patrol),
                export_layer=bool(cargo.export_layer),
                export_overlay=bool(cargo.export_overlay),
                export_json=bool(cargo.export_json),
            )

        data = self.data
        required_overrides = (
            ("spawn rules", data.spawn_rules_path, heatmaps is not None),
            ("prefab manifest", data.prefab_manifest_path,
             bool(monuments_enabled or tunnels or no_build or cargo)),
        )
        for label, value, required in required_overrides:
            if required and value is not None and not Path(value).is_file():
                raise FileNotFoundError(f"Override {label} file not found: {value}")
        normalized_data = DataOptions(
            spawn_rules_path=Path(data.spawn_rules_path) if data.spawn_rules_path else None,
            prefab_manifest_path=Path(data.prefab_manifest_path) if data.prefab_manifest_path else None,
        )
        normalized_exports = ExportOptions(
            heatmaps=heatmaps, diagnostics=diagnostics,
            monuments=monument_options if monuments_enabled else False,
            terrain=terrain,
            tunnels=tunnels, no_build_zones=no_build, cargo_ship_path=cargo,
            transforms=transforms,
        )
        return ExportConfig(
            map_path=map_path, output_dir=output_dir, exports=normalized_exports,
            data=normalized_data, status_updates=bool(self.status_updates),
            timing_debug=bool(self.timing_debug),
        )


@dataclass(frozen=True, slots=True)
class ExportResult:
    output_dir: Path
    world_size: int
    elapsed_seconds: float
    metadata_file: Path
    metadata: dict
    heatmap_categories: tuple[str, ...] = ()
    heatmaps_file: Path | None = None
    monuments_file: Path | None = None
    monument_count: int = 0
    monument_interactables_file: Path | None = None
    monument_interactable_count: int = 0
    monument_puzzles_file: Path | None = None
    monument_puzzle_count: int = 0
    monument_loot_file: Path | None = None
    monument_loot_position_count: int = 0
    monument_radiation_zones_file: Path | None = None
    monument_radiation_zone_count: int = 0
    map_image: Path | None = None
    full_map_image: Path | None = None
    map_tiles_dir: Path | None = None
    map_tiles_metadata_file: Path | None = None
    map_tile_count: int = 0
    diagnostics_dir: Path | None = None
    tunnels_image: Path | None = None
    tunnels_overlay_image: Path | None = None
    tunnels_metadata_file: Path | None = None
    tunnel_render_status: str = "disabled"
    no_build_zones_image: Path | None = None
    no_build_zones_overlay_image: Path | None = None
    no_build_zones_file: Path | None = None
    no_build_zone_count: int = 0
    no_build_zone_status: str = "disabled"
    cargo_ship_path_image: Path | None = None
    cargo_ship_path_overlay_image: Path | None = None
    cargo_ship_path_file: Path | None = None
    cargo_ship_path_node_count: int = 0
    cargo_ship_path_status: str = "disabled"
