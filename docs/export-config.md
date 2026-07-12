# ExportConfig in depth

`ExportConfig` is the high-level API for selecting and configuring Rust Map
Parser outputs. It separates file locations, output selection, stage-specific
settings, and packaged-data overrides so applications do not pay for work they
did not request.

```python
from rustmap import ExportConfig, RustMapExporter

result = RustMapExporter(ExportConfig(
    map_path="procedural.map",
    output_dir="output/my-map",
)).run()
```

The default configuration exports everything. For selective exports, provide an
`ExportOptions` object.

## Mental model

```text
ExportConfig
|-- map_path: Path
|-- output_dir: Path
|-- exports: ExportOptions
|   |-- heatmaps: HeatmapOptions | None
|   |-- diagnostics: bool
|   |-- monuments: bool
|   |-- terrain: TerrainOptions | None
|   |-- tunnels: TunnelOptions | None
|   `-- no_build_zones: NoBuildZoneOptions | None
|-- data: DataOptions
`-- timing_debug: bool
```

The rules are intentionally simple:

- `None` disables an output section that has its own options.
- `False` disables a simple on/off output.
- Disabled stages do not execute.
- Disabled stages do not load their packaged resource databases.
- Result paths for disabled stages are `None`.
- Status strings for disabled stages are `"disabled"`.

`ExportOptions()` starts empty. `ExportConfig` uses `ExportOptions.all()` when
the `exports` argument is omitted.

## Imports

All configuration types are available from the public `rustmap` package:

```python
from rustmap import (
    DataOptions,
    ExportConfig,
    ExportOptions,
    HeatmapOptions,
    NoBuildZoneOptions,
    RustMapExporter,
    TerrainOptions,
    TileOptions,
    TunnelOptions,
)
```

## ExportConfig

```python
ExportConfig(
    map_path: Path,
    output_dir: Path,
    exports: ExportOptions = ExportOptions.all(),
    data: DataOptions = DataOptions(),
    timing_debug: bool = False,
)
```

### `map_path`

Path to the Rust `.map` file. Validation raises `FileNotFoundError` before any
work begins if the file does not exist.

Both strings and `Path` objects are accepted, but the validated configuration
normalizes the value to `Path`.

### `output_dir`

Directory that receives the selected artifacts and `export_metadata.json`. The
directory is created when necessary.

Use a fresh output directory when changing selections. The exporter does not
delete old artifacts merely because their stage is disabled in a later run.

### `exports`

An `ExportOptions` object selecting the stages to run. Omitting it enables the
complete default export.

### `data`

Optional `DataOptions` overrides for the bundled spawn rules and prefab
manifest. Normal users should leave this at its default.

### `timing_debug`

When `True`, print a stage timing table after the run. Timing information is
always written to `export_metadata.json`, regardless of this setting.

## Presets

### Everything

These configurations are equivalent:

```python
ExportConfig(map_path=map_path, output_dir=output_dir)
```

```python
ExportConfig(
    map_path=map_path,
    output_dir=output_dir,
    exports=ExportOptions.all(),
)
```

The complete preset enables:

- Heatmaps and raw previews
- Diagnostics
- Monuments
- Scaled and full-size terrain renders
- Train tunnels
- No-build zones

Full-size map tiles remain opt-in because they create many additional files.

### Map only

```python
exports = ExportOptions.map_only()
```

Add full-size tiles:

```python
exports = ExportOptions.map_only(tiles=True)
```

Choose a different tile size:

```python
exports = ExportOptions.map_only(tiles=True, tile_size=256)
```

Map-only runs do not load spawn rules or the prefab manifest.

### Heatmaps only

```python
exports = ExportOptions.heatmaps_only()
```

Customize resolution and previews:

```python
exports = ExportOptions.heatmaps_only(
    resolution=1024,
    previews=False,
)
```

## ExportOptions

```python
ExportOptions(
    heatmaps: HeatmapOptions | None = None,
    diagnostics: bool = False,
    monuments: bool = False,
    terrain: TerrainOptions | None = None,
    tunnels: TunnelOptions | None = None,
    no_build_zones: NoBuildZoneOptions | None = None,
)
```

Constructing `ExportOptions` directly enables only the fields you provide:

```python
exports = ExportOptions(
    monuments=True,
    tunnels=TunnelOptions(),
)
```

This produces `monuments.json`, `tunnels.png`, and tunnel metadata. It does not
render terrain, so `tunnels_on_map.png` is omitted with a metadata warning.

At least one stage must be enabled. An empty `ExportOptions()` passed to
`ExportConfig` raises `ValueError`.

## HeatmapOptions

```python
HeatmapOptions(
    resolution: int | None = 2048,
    previews: bool = True,
)
```

### `resolution`

Width and height of every exported heatmap array. A numeric value must be
positive. Set it to `None` to use the parsed map's world size, giving one
heatmap pixel per world metre and direct alignment with `map_render_full.png`:

```python
HeatmapOptions(resolution=None)
```

In `export_metadata.json`, `heatmaps.resolution` records the resolved numeric
size, while `requested_resolution` remains `null` and `resolution_mode` is
`"world_size"`.

The output archive is named from this value:

```text
heatmaps_2048.npz
heatmaps_1024.npz
heatmaps_4250.npz  # resolution=None on a size-4250 map
```

Larger values increase interpolation time, NPZ size, preview size, and memory
usage quadratically.

### `previews`

When enabled, write one exact grayscale PNG per category beneath:

```text
Heatmap-previews/
```

Preview encoding does not alter the NPZ arrays.

### Example

```python
exports = ExportOptions(
    heatmaps=HeatmapOptions(
        resolution=2048,
        previews=True,
    ),
)
```

## Diagnostics

Diagnostics are a simple boolean because they currently have no tuning options:

```python
exports = ExportOptions(diagnostics=True)
```

This writes decoded height, water, alpha, biome, splat, and topology images,
composites, orientation checks, and `diagnostics.json` beneath `diagnostics/`.

When heatmaps and diagnostics are both enabled, diagnostics run alongside
heatmap computation where possible.

## Monuments

Monuments are also a simple boolean:

```python
exports = ExportOptions(monuments=True)
```

This writes `monuments.json` with positions, bottom-left map coordinates,
headings, classifications, safe-zone status, recycler counts, keycards, puzzle
types, and loot tiers.

Monument export loads the prefab manifest. It does not require terrain,
heatmaps, or diagnostics.

## TerrainOptions

```python
TerrainOptions(
    scale: float = 0.5,
    ocean_margin: int = 0,
    formats: str | Sequence[str] = ("png", "jpg"),
    full_size: bool = True,
    tiles: TileOptions | None = None,
    debug: bool = False,
)
```

### `scale`

Scale of the convenient `map_render.png` and `map_render.jpg` outputs. Rust's
renderer clamps the effective scale to its supported range.

For a size-4500 map, `scale=0.5` produces a 2250 x 2250 scaled render before
adding an ocean margin.

### `ocean_margin`

Non-negative pixels added around every side of the scaled render. The native
full-size render always uses zero margin.

### `formats`

Accepts either one format as a string or multiple formats as a sequence:

```python
TerrainOptions(formats="png")
TerrainOptions(formats=("png", "jpg"))
```

Formats for the scaled render when `full_size=False`. Supported values are
`"png"`, `"jpg"`, and `"jpeg"`; validation is case-insensitive. When
`full_size=True`, these are intentionally ignored and no scaled render is made.

The formats do not need to be cleared when only the full-size image is wanted:

```python
TerrainOptions(
    full_size=True,
)
```

At least one scaled format or `full_size=True` is required.

### `full_size`

Write `map_render_full.png` with one pixel per world metre. This full render is
also the source for terrain composites and map tiles. Enabling it supersedes
the scaled render, so the exporter performs only one terrain-rendering pass.

If tunnels or no-build zones are selected without a full-size terrain render,
their transparent layers still export but their `*_on_map.png` composites are
omitted.

### `tiles`

Set to `TileOptions()` to split the in-memory full-size render into map tiles.
Tiles require `full_size=True`.

### `debug`

Reserved renderer debugging switch. Normal applications should leave it false.

### Terrain examples

Only a world-size PNG:

```python
TerrainOptions(
    full_size=True,
)
```

Only a half-scale JPEG, with no full-size image:

```python
TerrainOptions(
    scale=0.5,
    formats=("jpg",),
    full_size=False,
)
```

Full-size PNG and 512px tiles (scaled formats are skipped):

```python
TerrainOptions(
    full_size=True,
    tiles=TileOptions(size=512),
)
```

## TileOptions

```python
TileOptions(size: int = 512)
```

`size` must be positive. Every exported tile is a fixed-size RGBA PNG.

- Tile `(0, 0)` is the bottom-left tile.
- X increases right.
- Y increases up.
- Partial top/right tiles are transparent-padded.
- `tiles.json` records content bounds and padding offsets.
- Tile encoding uses a bounded four-thread pool.

A size-4250 map produces 81 tiles at 512px. A size-6000 map produces 144.

## TunnelOptions

```python
TunnelOptions(
    resolution: int | None = None,
    overlay_opacity: float = 1.0,
    export_layer: bool = True,
    export_overlay: bool = True,
    tint_color: Color = (50, 45, 105, 104),
)
```

### `resolution`

Output width and height. `None` uses the Rust world size. Positive integers are
accepted for custom resolutions.

### `overlay_opacity`

Opacity of `tunnels_on_map.png`, from `0.0` through `1.0`. The authoritative
transparent `tunnels.png` layer is unaffected.

### `tint_color`

RGBA color composited between the full terrain map and tunnel geometry. The
default translucent purple-blue tint `(50, 45, 105, 104)` cools and darkens the
terrain so the light tunnel pieces are easier to follow. Use an alpha channel
of `0` to disable the tint without disabling the overlay.

### `export_layer`

Write the authoritative transparent `tunnels.png` layer. Disable it when an
application needs only the terrain-composited image.

### `export_overlay`

Write `tunnels_on_map.png` when a matching full-size terrain render is selected.
Overlay-only mode requires `export_layer=False`, `export_overlay=True`, and
`TerrainOptions(full_size=True)` in the same export selection.

At least one of `export_layer` and `export_overlay` must be true.

### Example

```python
exports = ExportOptions(
    terrain=TerrainOptions(full_size=True),
    tunnels=TunnelOptions(
        overlay_opacity=0.85,
        export_layer=False,
        export_overlay=True,
    ),
)
```

The high-level configuration always uses packaged tunnel templates. Local Rust
install and geometry-cache controls remain maintainer/low-level concerns rather
than application export settings.

## NoBuildZoneOptions

```python
NoBuildZoneOptions(
    resolution: int | None = None,
    fill_color: tuple[int, int, int, int] = (255, 0, 0, 64),
    outline_color: tuple[int, int, int, int] = (255, 0, 0, 255),
    outline_width: int = 3,
    export_images: bool = True,
    export_json: bool = True,
)
```

### `resolution`

Output width and height. `None` uses the world size.

### Colors

`fill_color` and `outline_color` are RGBA tuples. Every channel must be an
integer from 0 through 255.

### `outline_width`

Non-negative pixel width of primitive boundaries.

### `export_images`

Write `no_build_zones.png` and, when full-size terrain is available,
`no_build_zones_on_map.png`. Setting this false skips primitive rasterization
and PNG encoding entirely.

### `export_json`

Write `no_build_zones.json`. Set `export_images=False, export_json=True` for
JSON-only output. Set `export_images=True, export_json=False` for images only.
At least one output must be enabled.

### Example

```python
exports = ExportOptions(
    terrain=TerrainOptions(full_size=True),
    no_build_zones=NoBuildZoneOptions(
        fill_color=(255, 80, 0, 72),
        outline_color=(255, 30, 0, 255),
        outline_width=4,
        export_images=True,
        export_json=True,
    ),
)
```

## DataOptions

```python
DataOptions(
    spawn_rules_path: Path | None = None,
    prefab_manifest_path: Path | None = None,
)
```

The package normally loads `rustmap.data` resources automatically.

### `spawn_rules_path`

Override the spawn-rule database used by heatmap export. It is ignored when
heatmaps are disabled.

### `prefab_manifest_path`

Override the prefab manifest used by monuments, tunnels, and no-build zones. It
is not loaded by terrain-only, heatmap-only, or diagnostics-only runs.

Override paths are checked during configuration validation.

## Common recipes

### Terrain plus gameplay overlays

```python
exports = ExportOptions(
    terrain=TerrainOptions(full_size=True),
    tunnels=TunnelOptions(),
    no_build_zones=NoBuildZoneOptions(),
)
```

Produces terrain, transparent layers, and both terrain composites.

### Gameplay data without expensive terrain rendering

```python
exports = ExportOptions(
    monuments=True,
    tunnels=TunnelOptions(),
    no_build_zones=NoBuildZoneOptions(),
)
```

Produces monument JSON and transparent gameplay layers. Composite overlays are
omitted because no terrain was requested.

### Tunnel overlay only

```python
exports = ExportOptions(
    terrain=TerrainOptions(formats=(), full_size=True),
    tunnels=TunnelOptions(
        export_layer=False,
        export_overlay=True,
    ),
)
```

### No-build JSON only

```python
exports = ExportOptions(
    no_build_zones=NoBuildZoneOptions(
        export_images=False,
        export_json=True,
    ),
)
```

### Analysis data only

```python
exports = ExportOptions(
    heatmaps=HeatmapOptions(previews=False),
    diagnostics=True,
    monuments=True,
)
```

### A lightweight map thumbnail

```python
exports = ExportOptions(
    terrain=TerrainOptions(
        scale=0.25,
        formats=("jpg",),
        full_size=False,
    ),
)
```

### Native map plus tiles, no duplicate scaled render

```python
exports = ExportOptions(
    terrain=TerrainOptions(
        formats=(),
        full_size=True,
        tiles=TileOptions(size=512),
    ),
)
```

## Validation and failure behavior

Validation occurs in `RustMapExporter` construction, before parsing starts.

The configuration rejects:

- Missing `.map` paths
- Empty output selections
- Non-positive heatmap, tunnel, no-build, or tile resolutions
- Unsupported terrain formats
- Negative ocean margins or no-build outline widths
- Tiles without a full-size terrain render
- Tunnel opacity outside `0.0` through `1.0`
- Invalid RGBA tuples
- Missing data override files

Asset-level omissions are handled differently:

- Missing monument/no-build definitions are skipped and reported.
- Tunnel build mismatches generate warnings.
- A missing unusual-transform fallback omits only that tunnel instance.
- Missing terrain omits only tunnel/no-build composite previews.

## ExportResult

For the complete field-by-field result manual, see
**[`export-result.md`](export-result.md)**.

```python
result = RustMapExporter(config).run()
```

Core fields always available:

```python
result.output_dir
result.world_size
result.elapsed_seconds
result.metadata_file
result.metadata
```

Conditional fields:

```python
result.heatmaps_file
result.heatmap_categories
result.diagnostics_dir
result.monuments_file
result.monument_count
result.map_image
result.full_map_image
result.map_tiles_dir
result.map_tiles_metadata_file
result.map_tile_count
result.tunnels_image
result.tunnels_overlay_image
result.tunnel_render_status
result.no_build_zones_file
result.no_build_zones_image
result.no_build_zones_overlay_image
result.no_build_zone_count
result.no_build_zone_status
```

Disabled output paths are `None`; counts are zero. An enabled tunnel/no-build
stage may still have a missing composite path when terrain is disabled.

## export_metadata.json

Every run writes a stage-neutral `export_metadata.json` containing:

- Map serialization information and world size
- Enabled output selection
- Heatmap categories and rule database identity
- Diagnostic shapes and orientation validation
- Monument counts
- Terrain render and tile metadata
- Tunnel/no-build warnings and statistics
- Stage timings
- Artifact sizes

This file is the best source for logging, job status, and downstream automation.

## Migration from the old flat configuration

Old:

```python
ExportConfig(
    map_path=map_path,
    output_dir=output_dir,
    heatmap_resolution=2048,
    export_diagnostics=False,
    export_monuments=True,
    render_map=True,
    render_full_size_png=True,
    render_tunnels=False,
)
```

New:

```python
ExportConfig(
    map_path=map_path,
    output_dir=output_dir,
    exports=ExportOptions(
        heatmaps=HeatmapOptions(resolution=2048),
        monuments=True,
        terrain=TerrainOptions(full_size=True),
    ),
)
```

Old override paths moved under `DataOptions`. Stage-specific tuning moved into
the corresponding option object. The old flat fields are intentionally not
retained as compatibility aliases.

## Threading and memory

The selection model also controls resource usage:

- Map-only runs never allocate 2048 x 2048 heatmap caches.
- Heatmap-only runs never allocate full terrain render buffers.
- Diagnostics overlap heatmaps when both are enabled.
- Half/full terrain renders share decoded layers and run concurrently.
- Tunnel/no-build exports overlap when both are enabled.
- Map tiles use four bounded workers and worker-local tile buffers.

More selected stages do not always equal the sum of their standalone timings
because compatible work overlaps. Peak memory is deliberately bounded by
avoiding multiprocessing for the large shared terrain state.

## Complete example

See [`../example.py`](../example.py) for a complete editable example after
installing the package with `python -m pip install -e .`.
