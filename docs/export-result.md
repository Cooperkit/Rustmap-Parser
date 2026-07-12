# ExportResult in depth

`RustMapExporter.run()` returns an immutable `ExportResult` describing the
artifacts produced by the selected export stages.

```python
from rustmap import ExportConfig, ExportOptions, RustMapExporter

result = RustMapExporter(ExportConfig(
    map_path="procedural.map",
    output_dir="output/my-map",
    exports=ExportOptions.map_only(tiles=True),
)).run()

print(result.world_size)
print(result.full_map_image)
print(result.map_tile_count)
```

The result is designed so applications do not need to reopen
`export_metadata.json` just to discover common paths, counts, or statuses.

## Mental model

- Core run fields are always populated.
- Paths belonging to disabled outputs are `None`.
- Counts belonging to disabled outputs are zero.
- Tunnel/no-build status strings are `"disabled"` when their stage is disabled.
- An enabled stage can have a missing convenience overlay when full-size terrain
  was not selected.
- `metadata` contains the complete in-memory metadata document.
- `metadata_file` points to the persisted `export_metadata.json` copy.

`ExportResult` is a frozen, slotted dataclass. Treat it as a read-only handoff
from the exporter.

## Complete type shape

```python
ExportResult(
    output_dir: Path,
    world_size: int,
    elapsed_seconds: float,
    metadata_file: Path,
    metadata: dict,
    heatmap_categories: tuple[str, ...] = (),
    heatmaps_file: Path | None = None,
    monuments_file: Path | None = None,
    monument_count: int = 0,
    map_image: Path | None = None,
    full_map_image: Path | None = None,
    map_tiles_dir: Path | None = None,
    map_tiles_metadata_file: Path | None = None,
    map_tile_count: int = 0,
    diagnostics_dir: Path | None = None,
    tunnels_image: Path | None = None,
    tunnels_overlay_image: Path | None = None,
    tunnels_metadata_file: Path | None = None,
    tunnel_render_status: str = "disabled",
    no_build_zones_image: Path | None = None,
    no_build_zones_overlay_image: Path | None = None,
    no_build_zones_file: Path | None = None,
    no_build_zone_count: int = 0,
    no_build_zone_status: str = "disabled",
)
```

## Core fields

These fields are available after every successful export.

### `output_dir`

Validated output directory used for the run.

```python
print(result.output_dir)
```

The path can contain artifacts from an earlier run if the same directory was
reused with a different output selection. Use the result's conditional paths,
not a directory listing, to determine what the current run produced.

### `world_size`

Playable Rust world size in metres.

```python
print(result.world_size)  # for example: 4500
```

The native full-size terrain, tunnel, and no-build images normally use this as
both width and height.

### `elapsed_seconds`

Total exporter wall time measured with a high-resolution monotonic clock.

```python
print(f"Completed in {result.elapsed_seconds:.2f}s")
```

Independent stages can overlap, so elapsed time is not necessarily the sum of
all stage timings.

### `metadata_file`

Path to the always-generated `export_metadata.json`.

```python
assert result.metadata_file.name == "export_metadata.json"
```

### `metadata`

The complete JSON-compatible metadata dictionary before the exporter returns.

```python
print(result.metadata["enabled_outputs"])
print(result.metadata["timings"])
```

Prefer typed result fields for common application logic. Use `metadata` for
detailed warnings, source identities, per-category statistics, artifact sizes,
and renderer timings.

## Heatmap fields

Populated when `ExportOptions.heatmaps` is a `HeatmapOptions` instance.

### `heatmaps_file`

Path to the compressed NPZ archive, or `None` when heatmaps are disabled.

```python
if result.heatmaps_file is not None:
    print(result.heatmaps_file)
```

The filename includes the configured resolution, such as
`heatmaps_2048.npz`.

### `heatmap_categories`

Sorted tuple of category names exported into the NPZ archive.

```python
for category in result.heatmap_categories:
    print(category)
```

It is empty when heatmaps are disabled.

### Loading arrays

```python
import numpy as np

if result.heatmaps_file:
    with np.load(result.heatmaps_file) as heatmaps:
        ores = heatmaps["ores"]
        print(ores.shape, ores.dtype)
```

The arrays use exported image orientation and line up with PNG previews.

## Monument fields

Populated when `ExportOptions.monuments=True`.

### `monuments_file`

Path to `monuments.json`, or `None` when disabled.

### `monument_count`

Number of placed gameplay monument instances exported. Zero when disabled or
when the map has no matching gameplay monuments.

```python
if result.monuments_file:
    print(f"Exported {result.monument_count} monuments")
```

Do not use `monument_count == 0` alone to determine whether the stage ran; use
`monuments_file` or `metadata["enabled_outputs"]["monuments"]`.

## Terrain fields

Populated from `ExportOptions.terrain`.

### `map_image`

Path to the scaled `map_render.png` when `TerrainOptions.full_size=False` and
PNG is included in `TerrainOptions.formats`. Otherwise `None`. A full-size
terrain selection never also produces this convenience render.

There is currently no separate typed field for `map_render.jpg`; its path and
size are recorded in `metadata["terrain"]["artifacts"]`.

### `full_map_image`

Path to `map_render_full.png` when `TerrainOptions.full_size=True`.

```python
if result.full_map_image:
    print("Native terrain:", result.full_map_image)
```

This is the terrain source used for tunnel/no-build composite previews.

### `map_tiles_dir`

Path to `map_render_tiles/` when `TerrainOptions.tiles` is enabled.

### `map_tiles_metadata_file`

Path to `map_render_tiles/tiles.json` when tiles are enabled.

### `map_tile_count`

Number of fixed-size PNG tiles generated. Zero when tiles are disabled.

```python
if result.map_tiles_dir:
    print(f"Generated {result.map_tile_count} tiles")
```

For 512px tiles:

- Size 4250 produces 81 tiles.
- Size 4500 produces 81 tiles.
- Size 6000 produces 144 tiles.

## Diagnostic fields

### `diagnostics_dir`

Path to `diagnostics/` when `ExportOptions.diagnostics=True`, otherwise `None`.

The directory contains decoded layer PNGs and `diagnostics.json`. Individual
diagnostic paths are intentionally not duplicated in `ExportResult`; enumerate
the directory or inspect metadata when needed.

## Tunnel fields

Populated according to `TunnelOptions` and runtime status.

### `tunnels_image`

Path to the transparent `tunnels.png` layer when:

- The tunnel stage is enabled.
- Rendering succeeds.
- `TunnelOptions.export_layer=True`.

It is `None` in overlay-only mode.

### `tunnels_overlay_image`

Path to `tunnels_on_map.png` when:

- `TunnelOptions.export_overlay=True`.
- A matching full-size terrain image exists.
- Tunnel rendering succeeds.

It is `None` when the overlay was disabled or omitted.

### `tunnels_metadata_file`

Path to `tunnels_metadata.json` whenever the tunnel stage was enabled. The file
is written for rendered and skipped/error states.

### `tunnel_render_status`

High-level tunnel stage status.

Common values:

| Status | Meaning |
|---|---|
| `"disabled"` | Tunnel stage was not selected |
| `"rendered"` | Tunnel geometry rendered successfully |
| `"skipped"` | Rendering could not proceed; inspect tunnel metadata |

Use status plus conditional paths:

```python
if result.tunnel_render_status == "rendered":
    if result.tunnels_image:
        print("Layer:", result.tunnels_image)
    if result.tunnels_overlay_image:
        print("Overlay:", result.tunnels_overlay_image)
else:
    print(result.metadata["tunnels"].get("reason"))
```

## No-build fields

Populated according to `NoBuildZoneOptions`.

### `no_build_zones_image`

Path to `no_build_zones.png` when `export_images=True`, otherwise `None`.

### `no_build_zones_overlay_image`

Path to `no_build_zones_on_map.png` when images were requested and a matching
full-size terrain render exists. It is `None` for JSON-only and terrain-free
exports.

### `no_build_zones_file`

Path to `no_build_zones.json` when `export_json=True`, otherwise `None`.

### `no_build_zone_count`

Number of selected no-build primitives. This count remains available for
images-only and JSON-only runs because geometry selection always occurs.

### `no_build_zone_status`

No-build stage status, normally `"rendered"` or `"disabled"`.

```python
if result.no_build_zones_file:
    print(f"JSON contains {result.no_build_zone_count} zones")
```

## Selection-to-result matrix

| Selected output | Primary populated result fields |
|---|---|
| Heatmaps | `heatmaps_file`, `heatmap_categories` |
| Diagnostics | `diagnostics_dir` |
| Monuments | `monuments_file`, `monument_count` |
| Terrain PNG | `map_image` |
| Full terrain | `full_map_image` |
| Tiles | `map_tiles_dir`, `map_tiles_metadata_file`, `map_tile_count` |
| Tunnel layer | `tunnels_image`, `tunnels_metadata_file`, status |
| Tunnel overlay only | `tunnels_overlay_image`, `tunnels_metadata_file`, status |
| No-build images | `no_build_zones_image`, optional overlay, count, status |
| No-build JSON only | `no_build_zones_file`, count, status |

## Safe consumption patterns

### Check paths, not assumptions

```python
if result.full_map_image is not None:
    upload_file(result.full_map_image)
```

Do not construct expected paths manually unless integrating with a fixed file
contract. The result already accounts for disabled outputs and omitted overlays.

### Use status for fallible gameplay layers

```python
if result.tunnel_render_status != "rendered":
    warning = result.metadata["tunnels"].get("reason")
```

### Serialize a compact job response

`ExportResult` contains `Path` objects, so convert them to strings for JSON APIs:

```python
def path_text(path):
    return str(path) if path is not None else None

response = {
    "world_size": result.world_size,
    "elapsed_seconds": result.elapsed_seconds,
    "metadata": path_text(result.metadata_file),
    "heatmaps": path_text(result.heatmaps_file),
    "map": path_text(result.full_map_image),
    "tiles": path_text(result.map_tiles_dir),
    "monuments": path_text(result.monuments_file),
    "tunnels": path_text(result.tunnels_image),
    "tunnel_overlay": path_text(result.tunnels_overlay_image),
    "no_build_json": path_text(result.no_build_zones_file),
}
```

### Avoid stale output confusion

If an output directory was reused, old files may still physically exist for a
stage that was disabled in the current run. Conditional result paths describe
the current run and remain `None` even if such a stale file exists.

Within an enabled stage, output switches remove stale mutually exclusive files;
for example, tunnel overlay-only removes an old `tunnels.png`, and no-build
JSON-only removes old no-build PNGs.

## export_metadata.json structure

The typed result intentionally exposes only common fields. Detailed information
lives in `result.metadata` and `export_metadata.json`:

```text
schema_version
map
orientation
enabled_outputs
heatmaps
diagnostics
monuments
terrain
tunnels
no_build_zones
timings
generation
```

### `enabled_outputs`

Boolean record of which high-level stages were selected:

```python
{
    "heatmaps": True,
    "diagnostics": False,
    "monuments": True,
    "terrain": True,
    "tunnels": False,
    "no_build_zones": True,
}
```

### `generation`

Contains final wall time and artifact sizes:

```python
elapsed = result.metadata["generation"]["elapsed_seconds"]
sizes = result.metadata["generation"]["artifact_sizes_bytes"]
```

### Per-stage metadata

- `heatmaps` includes resolution, categories, previews, and rule identity.
- `diagnostics` includes native shapes and orientation validation.
- `monuments` includes instance and unique-prefab counts.
- `terrain` mirrors `map_render_metadata.json`.
- `tunnels` mirrors `tunnels_metadata.json` and requested outputs.
- `no_build_zones` includes output selection, counts, warnings, and zones.
- `timings` contains stage and per-category durations.

## Examples by selection

### Map only

```python
result = RustMapExporter(ExportConfig(
    map_path=map_path,
    output_dir="output/map",
    exports=ExportOptions.map_only(tiles=True),
)).run()

assert result.heatmaps_file is None
assert result.monuments_file is None
assert result.full_map_image is not None
assert result.map_tiles_dir is not None
```

### Heatmaps only

```python
result = RustMapExporter(ExportConfig(
    map_path=map_path,
    output_dir="output/heatmaps",
    exports=ExportOptions.heatmaps_only(previews=False),
)).run()

assert result.heatmaps_file is not None
assert result.full_map_image is None
assert result.tunnel_render_status == "disabled"
```

### Tunnel overlay only

```python
from rustmap import TerrainOptions, TunnelOptions

result = RustMapExporter(ExportConfig(
    map_path=map_path,
    output_dir="output/tunnel-overlay",
    exports=ExportOptions(
        terrain=TerrainOptions(formats=(), full_size=True),
        tunnels=TunnelOptions(
            export_layer=False,
            export_overlay=True,
        ),
    ),
)).run()

assert result.tunnels_image is None
assert result.tunnels_overlay_image is not None
```

### No-build JSON only

```python
from rustmap import NoBuildZoneOptions

result = RustMapExporter(ExportConfig(
    map_path=map_path,
    output_dir="output/no-build-json",
    exports=ExportOptions(
        no_build_zones=NoBuildZoneOptions(
            export_images=False,
            export_json=True,
        ),
    ),
)).run()

assert result.no_build_zones_file is not None
assert result.no_build_zones_image is None
assert result.no_build_zones_overlay_image is None
```

## Relationship to ExportConfig

Output selection and tuning are documented in
[`export-config.md`](export-config.md). `ExportConfig` describes what should run;
`ExportResult` reports what the run produced.
