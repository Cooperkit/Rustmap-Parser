# ExportConfig in depth

`ExportConfig` is the high-level API for selecting and configuring Rust Map
Parser outputs. It separates file locations, output selection, stage-specific
settings, and packaged-data overrides so applications do not pay for work they
did not request.

```python
from rustmap_parser import ExportConfig, RustMapExporter

result = RustMapExporter(ExportConfig(
    map_path="procedural.map",
    output_dir="output/my-map",
)).run()
```

The default configuration uses `ExportOptions.all()` and exports every output,
including all four monument sidecars. For selective exports, provide an
`ExportOptions` object.

## Mental model

```text
ExportConfig
|-- map_path: Path
|-- output_dir: Path
|-- exports: ExportOptions
|   |-- heatmaps: HeatmapOptions | None
|   |-- diagnostics: bool | DiagnosticsOptions
|   |-- monuments: bool | MonumentOptions
|   |   |-- interactable: bool -> monuments/monument_interactables.json
|   |   |-- puzzles: bool -> monuments/monument_puzzles.json
|   |   |-- loot: bool -> monuments/monument_loot.json
|   |   |-- radiation_zones: bool -> monuments/monument_radiation_zones.json
|   |-- terrain: TerrainOptions | None
|   |-- tunnels: TunnelOptions | None
|   |-- no_build_zones: NoBuildZoneOptions | None
|   |-- cargo_ship_path: CargoShipPathOptions | None
|   `-- transforms: TransformOptions
|-- data: DataOptions
|-- status_updates: bool
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

All configuration types are available from the public `rustmap_parser` package:

```python
from rustmap_parser import (
    CargoShipPathOptions,
    DataOptions,
    DiagnosticsOptions,
    ExportConfig,
    ExportOptions,
    HeatmapOptions,
    MonumentOptions,
    NoBuildZoneOptions,
    RustMapExporter,
    TerrainOptions,
    TileOptions,
    TransformOptions,
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
    status_updates: bool = False,
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

### `status_updates`

When `True`, print concise live pipeline milestones to standard output. Every
line begins with `[rust-map-parser]` and is flushed immediately, making this
suitable for terminals, service logs, and desktop application consoles.

The updates cover:

- Export start, map loading, world size, and placed-prefab count
- Enabled heatmap, diagnostics, monument, and terrain stages
- Supporting no-build, tunnel, and cargo stages, including useful result counts
- Metadata writing and final elapsed time/output directory

The exporter does not print per-prefab, per-loot-marker, or per-puzzle-step
messages. When supporting stages run concurrently, their completion messages
appear in actual completion order. The default is `False`, and this setting is
independent of `timing_debug`, which prints the detailed timing table only after
the run.

```python
config = ExportConfig(
    map_path=map_path,
    output_dir=output_dir,
    status_updates=True,
)
```

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
- Monuments, interactables, puzzles, loot, and radiation zones
- Full-size terrain render and 512-pixel map tiles
- Train-tunnel layer and overlay
- No-build-zone layer, overlay, and JSON
- Cargo patrol layer, terrain overlay, and JSON

The scaled terrain render is intentionally omitted when `full_size=True`; this
avoids rendering the same terrain twice. All distinct normal output artifacts,
including full-size map tiles and every monument JSON, are enabled by this
preset.

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
    diagnostics: bool | DiagnosticsOptions = False,
    monuments: bool | MonumentOptions = False,
    terrain: TerrainOptions | None = None,
    tunnels: TunnelOptions | None = None,
    no_build_zones: NoBuildZoneOptions | None = None,
    cargo_ship_path: CargoShipPathOptions | None = None,
    transforms: TransformOptions = TransformOptions(),
)
```

Constructing `ExportOptions` directly enables only the fields you provide:

```python
exports = ExportOptions(
    monuments=True,
    tunnels=TunnelOptions(),
)
```

This produces `monuments/monuments.json`, `tunnels.png`, and tunnel metadata. It does not
render terrain, so `tunnels_on_map.png` is omitted with a metadata warning.

At least one stage must be enabled. An empty `ExportOptions()` passed to
`ExportConfig` raises `ValueError`.

## MonumentOptions

```python
MonumentOptions(
    interactable: bool = False,
    puzzles: bool = False,
    loot: bool = False,
    radiation_zones: bool = False,
)
```

All four expansions default to off. `interactable` and `puzzles` write separate
indented JSON files; `loot` and
`radiation_zones` create their own sidecars. None of these arrays are embedded
in `monuments/monuments.json`. Passing plain `monuments=True` retains the small
basic export.

The switches are independent. For example, this writes the puzzle file without
performing interactable placement:

```python
exports = ExportOptions(
    monuments=MonumentOptions(puzzles=True),
)
```

## TransformOptions

```python
TransformOptions(
    local_position: bool = True,
    position: bool = True,
    map_position: bool = True,
)
```

These global switches project coordinate fields consistently across every
transform-bearing JSON export. Defaults preserve the complete output:

- `local_position` is XYZ in the owning monument prefab frame.
- `position` is the true Unity world XYZ position.
- `map_position` is bottom-left map XY for placing a marker on a map.

For a compact map-marker-only export:

```python
exports = ExportOptions(
    monuments=MonumentOptions(
        interactable=True,
        puzzles=True,
        loot=True,
        radiation_zones=True,
    ),
    no_build_zones=NoBuildZoneOptions(export_json=True),
    cargo_ship_path=CargoShipPathOptions(export_json=True),
    transforms=TransformOptions(
        local_position=False,
        position=False,
        map_position=True,
    ),
)
```

This filters monument roots and sidecars, cargo patrol/harbor nodes, and
no-build-zone centres. It does not remove `heading_degrees`, `rotation_euler`,
loot `radius`, or collider geometry because those describe orientation or shape,
not alternate position representations. Image renderers still use their full
internal geometry. All three switches may be false for an identity/metadata-only
JSON export. Each affected document includes `exported_position_fields` so
consumers can discover its coordinate contract.

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
heatmaps.npz  # the configured resolution is stored in export metadata
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

Use `DiagnosticsOptions` to give every diagnostic PNG one square output size:

```python
from rustmap_parser import DiagnosticsOptions

exports = ExportOptions(
    diagnostics=DiagnosticsOptions(resolution=None),
)
```

`resolution=None` resolves to the map's world size, producing one pixel per
world metre. A positive integer such as `1024` selects an explicit
`1024 × 1024` output. Plain `diagnostics=True` remains a compatibility mode
that preserves the different native resolutions of the decoded layers.

This writes decoded height, water, alpha, biome, splat, and topology images,
composites, orientation checks, and `diagnostics.json` beneath `diagnostics/`.
Continuous height, water, alpha, biome, and splat images use bilinear scaling.
Topology masks use nearest-neighbor scaling so their values remain binary.
`export_metadata.json` records the requested resolution, resolved resolution,
resolution mode, and original native layer shapes.

When heatmaps and diagnostics are both enabled, diagnostics run alongside
heatmap computation where possible.

## Monuments

Basic monument export is a simple boolean:

```python
exports = ExportOptions(monuments=True)
```

This writes `monuments/monuments.json` with positions, bottom-left map
coordinates, headings, classifications, safe-zone status, recycler counts,
keycards, puzzle types, and loot tiers. Existing gameplay-monument roots are preserved, while
train-tunnel links and monument-owned entrances are positioned from packaged
prefab child `LandmarkInfo` transforms to match the Rust+ server behavior.

Every expanded monument export is independently opt-in and defaults to off:

```python
exports = ExportOptions(
    monuments=MonumentOptions(
        interactable=True,
        puzzles=True,
        loot=False,
        radiation_zones=False,
    ),
)
```

`interactable=True` writes per-monument interactables to
`monuments/monument_interactables.json`. Independently, `puzzles=True` writes
major puzzle routes to `monuments/monument_puzzles.json`. Recognized
interactables include recyclers, research tables, oil refineries, repair
benches, mixing tables, workbenches, vending machines, and marketplaces.

Vanilla child positions come from packaged prefab-root-relative component
transforms. Recognized prefabs placed directly in the `.map` are merged as
`source="map_prefab"`, which captures RustEdit/custom-monument additions such
as extra recyclers. After assignment, their `local_position` is expressed in
the owning monument root's coordinate frame, including its rotation and scale.
When any monument expansion is enabled, nonstandard prefab roots explicitly in
the map's `Monument` category are also exported as `custom_monument` records;
Rust's unique-environment roots remain excluded. Assignment uses the
monument's oriented bounds first and a conservative size-based fallback. Items
that cannot be assigned confidently are kept in the interactables sidecar's
top-level `unassigned_interactables` array.

`loot=True` writes a separate, indented `monuments/monument_loot.json`; no loot
arrays are placed in `monuments/monuments.json`. Records are grouped beneath
their owning monument prefab, then by loot kind and possible prefab paths. Every position has
`local_position`, Unity world `position`, bottom-left `map_position`, and
radial uncertainty. Each `prefabs` entry has explicit `kind` and `prefab`
fields. Coordinates are rounded to millimetres. Radius is zero
for an exact point and is the random placement radius for a radial point.
Population limits, respawn timers, weights, group settings, summaries, and
runtime occupancy are deliberately omitted. Direct barrel/crate/diesel prefabs added
to a custom monument are included after they are assigned to an owner. Diesel
Fuel collectables use kind `diesel_fuel` and prefab
`assets/content/structures/excavator/prefabs/diesel_collectable.prefab`; both
vanilla spawn choices and directly placed custom copies are recognized.

Every monument always keeps just one `metadata.gameplay.maximum_radiation`
number. `radiation_zones=True` additionally writes the separate, indented
`monuments/monument_radiation_zones.json`, containing static
`TriggerRadiation` sphere or box geometry, tier, raw amount before player
protection, falloff, relevant
flags, and readable local/world/map positions. Zones are grouped beneath their
owning monument prefab. A dynamic zone has
`radiation_amount=null` because server events can
change it at runtime. Radiation outside a monument trigger,
plugin-created runtime zones, and a custom prefab's unbundled internal
components cannot be inferred reliably from the `.map` alone.

The packaged data retains Rust's complete directed `IOEntity.outputs` graph,
but that implementation detail is not dumped into either output. Each puzzle
in `monuments/monument_puzzles.json` is a compact causal route containing only
major player actions
(`insert_fuse`, `turn_on_switch`, `press_button`, `swipe_keycard`, or
`turn_wheel`) followed by the `door_opens` outcome. Lighting/alarm switches,
timers, logic components, and inside exit-button alternatives are omitted.
Every retained step includes exact local, world, and map positions. Dynamic
modular puzzles with no baked graph are omitted rather than receiving guessed
steps.

One `puzzles` entry represents one powered player puzzle, not one physical
door. When several keycard readers open equivalent entrances powered by the
same fuse/switch sequence, the canonical walkthrough remains in `steps` and
the other valid reader/door suffixes are retained in `alternate_endings`.
`common_step_count` says how many leading canonical steps precede each ending,
and `endpoint_count` records the total number of equivalent access points.

For server-side validation, `tools/MonumentDetailsProbe.cs` is an optional
Oxide plugin. It writes `oxide/data/MonumentDetailsProbe.json` after server
initialization and on the `monumentdetails.export` console command. The probe
captures runtime entities and IO links, including plugin-spawned entities and
dynamically assembled puzzles, plus live spawn-group population and radiation
trigger values. It is deliberately diagnostic-only: the parser
does not require the file and has no server-snapshot path setting.

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

Each zone exposes a compact top-level coordinate set:

```json
{
  "position": {"x": -1530.0, "y": 9.0, "z": -1038.0},
  "map_position": {"x": 595.0, "y": 1087.0},
  "heading_degrees": 316.143671635
}
```

`map_position` is measured in metres from the bottom-left of the playable map.
Verbose Euler, scale, normalized, and image-position transforms are omitted.

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

## CargoShipPathOptions

```python
CargoShipPathOptions(
    resolution: int | None = None,
    patrol_color: tuple[int, int, int, int] = (62, 203, 255, 255),
    harbor_color: tuple[int, int, int, int] = (255, 184, 61, 255),
    line_width: int = 4,
    smooth_patrol: bool = True,
    export_layer: bool = True,
    export_overlay: bool = True,
    export_json: bool = True,
)
```

### `resolution`

Output width and height. `None` uses the world size and aligns directly with
`map_render_full.png`.

### Colors and width

`patrol_color` draws the closed generated ocean loop. `harbor_color` draws the
packaged Harbor 1/2 approach and departure nodes. Colors are RGBA channels from
0 through 255; `line_width` must be positive.

### `smooth_patrol`

When `True`, the exporter simulates `CargoShip.UpdateMovement`: decreasing node
order, an 80-metre arrival radius, eased steering/throttle, a
2.5-degree-per-second turn limit, and an 8-metre-per-second maximum speed. It
warms the controls for one loop, records the next loop, then compacts the track
with a 0.5-metre simplification. The result is the smooth, corner-cutting ship
centreline seen in game. PNG, terrain composite, and JSON use the same track;
`source_node_count` records the reconstructed waypoint count. Harbor paths are
not simulated and reconnect to the nearest exported patrol point. The default
is `True`; set it to `False` to preserve the generated angular waypoints.

### Output selection

- `export_layer=True` writes `cargo_ship_path.png`.
- `export_overlay=True` writes `cargo_ship_path_on_map.png` when a matching
  full-size terrain image is available.
- `export_json=True` writes `cargo_ship_path.json`.

At least one must be enabled. JSON-only cargo export does not allocate or encode
a world-size image:

```python
exports = ExportOptions(
    cargo_ship_path=CargoShipPathOptions(
        export_layer=False,
        export_overlay=False,
        export_json=True,
    ),
)
```

The server generates the ocean loop during `WorldSetup` rather than serializing
it in the `.map`. The offline Python export reconstructs its ordered relaxation
from the serialized TerrainCollider heightfield and packaged collision
footprints for placed world prefabs such as icebergs. It reports
`accuracy: world_setup_collision_reconstructed`. Shallow submerged terrain down
to three metres below sea level is included because Rust uses radius-3 sphere
casts at world Y=0; measuring from only the visible shoreline is insufficient.

`WorldSetup` creates the route before `ServerMgr.Initialize`, save loading, and
`SpawnHandler.InitialSpawn`. Later runtime populations such as floating
junkpiles are therefore deliberately excluded. Exact packaged harbor `BasePath`
nodes are applied through each placed harbor transform. Collision resource
version and missing-template details are available under
`generation.prefab_collision`.

## DataOptions

```python
DataOptions(
    spawn_rules_path: Path | None = None,
    prefab_manifest_path: Path | None = None,
)
```

The package normally loads `rustmap_parser.data` resources automatically.

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
- Non-positive heatmap, tunnel, no-build, cargo-path, or tile resolutions
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
- Missing terrain omits only the cargo-path composite preview.

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
result.monument_interactables_file
result.monument_interactable_count
result.monument_puzzles_file
result.monument_puzzle_count
result.monument_loot_file
result.monument_loot_position_count
result.monument_radiation_zones_file
result.monument_radiation_zone_count
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
result.cargo_ship_path_file
result.cargo_ship_path_image
result.cargo_ship_path_overlay_image
result.cargo_ship_path_node_count
result.cargo_ship_path_status
```

Disabled output paths are `None`; counts are zero. An enabled tunnel/no-build/cargo
stage may still have a missing composite path when terrain is disabled.

## export_metadata.json

Every run writes a stage-neutral `export_metadata.json` containing:

- The input map filename, serialization information, and world size
- Enabled output selection
- Heatmap categories and rule database identity
- Diagnostic shapes and orientation validation
- Monument counts
- Terrain render and tile metadata
- Tunnel/no-build/cargo warnings and statistics
- Stage timings
- Artifact sizes

This file is the best source for logging, job status, and downstream automation.
`map.path` is deliberately filename-only so sharing an export cannot disclose a
local workstation or game-server directory. Schema version 7 introduced this
privacy behavior; schema version 6 and older may contain the caller-provided path.

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
