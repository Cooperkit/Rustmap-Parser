"""Public API for the standalone Rust map parser."""

from .config import (
    DataOptions,
    ExportConfig,
    ExportOptions,
    ExportResult,
    HeatmapOptions,
    NoBuildZoneOptions,
    TerrainOptions,
    TileOptions,
    TunnelOptions,
)
from .exporter import RustMapExporter
from .layers import generate_diagnostics
from .parser import RustMap, RustMapError, load_map
from .resources import refresh_prefab_manifest, refresh_spawn_rules
from .validation import compare_files as compare_heatmaps
from .tunnels import render_tunnel_map
from .tunnel_assets import (
    install_packaged_tunnel_templates,
    refresh_tunnel_geometry,
    refresh_tunnel_templates,
)
from .no_build import build_no_build_export, save_no_build_zones
from .no_build_assets import refresh_no_build_zone_data
from .monuments import monument_metadata
from .monument_assets import refresh_monument_metadata

__all__ = (
    "DataOptions", "ExportConfig", "ExportOptions", "ExportResult",
    "HeatmapOptions", "NoBuildZoneOptions", "TerrainOptions", "TileOptions",
    "TunnelOptions", "RustMapExporter", "RustMap", "RustMapError",
    "load_map", "generate_diagnostics", "refresh_prefab_manifest",
    "refresh_spawn_rules", "compare_heatmaps", "render_tunnel_map",
    "refresh_tunnel_geometry",
    "refresh_tunnel_templates",
    "install_packaged_tunnel_templates",
    "build_no_build_export", "save_no_build_zones", "refresh_no_build_zone_data",
    "monument_metadata",
    "refresh_monument_metadata",
)

__version__ = "0.1.2"
