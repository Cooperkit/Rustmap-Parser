"""Typed raster decoders and diagnostics for parsed Rust maps."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .parser import RustMap, RustMapError
from .png import save_png


SPLAT_CHANNELS = (
    "dirt", "snow", "sand", "rock", "grass", "forest", "stones", "gravel"
)

# The first four indices are defined by Facepunch's Rust.World SDK. Jungle was
# added to the game in May 2025 and is the fifth channel in current maps.
BIOME_CHANNELS = ("arid", "temperate", "tundra", "arctic", "jungle")

TOPOLOGY_BITS = (
    "field", "cliff", "summit", "beachside", "beach", "forest",
    "forestside", "ocean", "oceanside", "decor", "monument", "road",
    "roadside", "swamp", "river", "riverside", "lake", "lakeside",
    "offshore", "powerline", "runway", "building", "cliffside",
    "mountain", "clutter", "alt", "tier0", "tier1", "tier2", "mainland",
    "hilltop",
)

SHORT_NORMALIZER = 32766.0
TERRAIN_VERTICAL_SIZE = 1000.0
TERRAIN_VERTICAL_OFFSET = -500.0


def _square_resolution(element_count: int, layer_name: str) -> int:
    resolution = math.isqrt(element_count)
    if resolution * resolution != element_count:
        raise RustMapError(
            f"Layer {layer_name!r} has {element_count} elements, not a square grid"
        )
    return resolution


def int16_grid(world: RustMap, name: str) -> np.ndarray:
    """Decode a signed-16-bit, single-channel `[z, x]` layer."""
    layer = world.layer(name)
    if len(layer.data) % 2:
        raise RustMapError(f"Layer {name!r} has an odd byte count")
    source = np.frombuffer(layer.data, dtype="<i2")
    resolution = _square_resolution(source.size, name)
    return source.reshape(resolution, resolution)


def normalized_int16_grid(world: RustMap, name: str) -> np.ndarray:
    return int16_grid(world, name).astype(np.float32) / SHORT_NORMALIZER


def world_height_grid(world: RustMap, name: str = "height") -> np.ndarray:
    """Convert a Rust normalized height layer to Unity world-space Y metres."""
    return (
        normalized_int16_grid(world, name) * TERRAIN_VERTICAL_SIZE
        + TERRAIN_VERTICAL_OFFSET
    )


def uint8_grid(world: RustMap, name: str) -> np.ndarray:
    layer = world.layer(name)
    source = np.frombuffer(layer.data, dtype=np.uint8)
    resolution = _square_resolution(source.size, name)
    return source.reshape(resolution, resolution)


def topology_grid(world: RustMap) -> np.ndarray:
    """Decode topology as an unsigned 32-bit `[z, x]` bitmask."""
    layer = world.layer("topology")
    if len(layer.data) % 4:
        raise RustMapError("Topology byte count is not divisible by four")
    source = np.frombuffer(layer.data, dtype="<u4")
    resolution = _square_resolution(source.size, "topology")
    return source.reshape(resolution, resolution)


def _texture_resolution(world: RustMap) -> int:
    """Use alpha/topology to obtain the current map's texture resolution."""
    try:
        return uint8_grid(world, "alpha").shape[0]
    except KeyError:
        return topology_grid(world).shape[0]


def channel_grid(world: RustMap, name: str, channel_names: tuple[str, ...]) -> np.ndarray:
    """Decode a channel-major byte layer as `[channel, z, x]`."""
    resolution = _texture_resolution(world)
    source = np.frombuffer(world.layer(name).data, dtype=np.uint8)
    pixels = resolution * resolution
    if source.size % pixels:
        raise RustMapError(
            f"Layer {name!r} does not contain whole {resolution}x{resolution} channels"
        )
    channels = source.size // pixels
    if channels > len(channel_names):
        raise RustMapError(
            f"Layer {name!r} contains {channels} channels but only "
            f"{len(channel_names)} names are known"
        )
    return source.reshape(channels, resolution, resolution)


def splat_grid(world: RustMap) -> np.ndarray:
    return channel_grid(world, "splat", SPLAT_CHANNELS)


def biome_grid(world: RustMap) -> np.ndarray:
    return channel_grid(world, "biome", BIOME_CHANNELS)


def topology_mask(world: RustMap, name_or_bit: str | int) -> np.ndarray:
    bit = TOPOLOGY_BITS.index(name_or_bit) if isinstance(name_or_bit, str) else name_or_bit
    if not 0 <= bit < 32:
        raise ValueError(f"Invalid topology bit: {bit}")
    return (topology_grid(world) & np.uint32(1 << bit)) != 0


def world_to_grid(
    world: RustMap, world_x: float, world_z: float, resolution: int
) -> tuple[float, float]:
    """Convert centered Unity `(x,z)` metres to fractional `(grid_x,grid_z)`."""
    scale = resolution - 1
    return (
        (world_x / world.size + 0.5) * scale,
        (world_z / world.size + 0.5) * scale,
    )


def grid_to_world(
    world: RustMap, grid_x: float, grid_z: float, resolution: int
) -> tuple[float, float]:
    scale = resolution - 1
    return (
        (grid_x / scale - 0.5) * world.size,
        (grid_z / scale - 0.5) * world.size,
    )


def sample_bilinear(grid: np.ndarray, grid_x: float, grid_z: float) -> float:
    if grid.ndim != 2:
        raise ValueError("Bilinear sampling requires a two-dimensional grid")
    x = float(np.clip(grid_x, 0, grid.shape[1] - 1))
    z = float(np.clip(grid_z, 0, grid.shape[0] - 1))
    x0, z0 = int(math.floor(x)), int(math.floor(z))
    x1, z1 = min(x0 + 1, grid.shape[1] - 1), min(z0 + 1, grid.shape[0] - 1)
    tx, tz = x - x0, z - z0
    return float(
        grid[z0, x0] * (1 - tx) * (1 - tz)
        + grid[z0, x1] * tx * (1 - tz)
        + grid[z1, x0] * (1 - tx) * tz
        + grid[z1, x1] * tx * tz
    )


def sample_world_height(
    world: RustMap, world_x: float, world_z: float, name: str = "height"
) -> float:
    grid = world_height_grid(world, name)
    x, z = world_to_grid(world, world_x, world_z, grid.shape[0])
    return sample_bilinear(grid, x, z)


def _resize_diagnostic(image: Image.Image, resolution: int | None,
                       resample: Image.Resampling) -> Image.Image:
    if resolution is None or image.size == (resolution, resolution):
        return image
    return image.resize((resolution, resolution), resample=resample)


def _save_gray(path: Path, values: np.ndarray, resolution: int | None = None,
               resample: Image.Resampling = Image.Resampling.BILINEAR) -> None:
    # Rust's decoded Z origin is opposite the image/viewer Y origin. Preserve
    # X (left/right) and reverse rows only; analytical arrays stay unchanged.
    exported = np.asarray(values, dtype=np.uint8)[::-1, :]
    image = Image.fromarray(exported, mode="L")
    save_png(_resize_diagnostic(image, resolution, resample), path)


def _height_preview(values: np.ndarray) -> np.ndarray:
    # The SDK defines the full vertical range as -500..+500 metres.
    normalized = np.clip((values - TERRAIN_VERTICAL_OFFSET) / TERRAIN_VERTICAL_SIZE, 0, 1)
    return np.rint(normalized * 255).astype(np.uint8)


def _dominant_color(channels: np.ndarray, colors: np.ndarray) -> np.ndarray:
    weights = channels.astype(np.float32) / 255.0
    rgb = np.einsum("czx,ck->zxk", weights, colors.astype(np.float32))
    return np.clip(rgb, 0, 255).astype(np.uint8)


def validate_orientation(world: RustMap) -> dict:
    """Check the documented raw `[z,x]` orientation against prefab elevations."""
    heights = world_height_grid(world, "height")
    errors: list[float] = []
    samples = 0
    for prefab in world.prefabs:
        if prefab.position is None:
            continue
        x, z = world_to_grid(world, prefab.position.x, prefab.position.z, heights.shape[0])
        if not (0 <= x < heights.shape[1] and 0 <= z < heights.shape[0]):
            continue
        predicted = sample_bilinear(heights, x, z)
        errors.append(abs(predicted - prefab.position.y))
        samples += 1
    values = np.asarray(errors, dtype=np.float64)
    return {
        "method": "raw grid indexed as [z,x], centered world coordinates, no axis flip",
        "sample_count": samples,
        "median_absolute_height_error_m": float(np.median(values)),
        "p75_absolute_height_error_m": float(np.percentile(values, 75)),
        "mean_absolute_height_error_m": float(np.mean(values)),
    }


def generate_diagnostics(world: RustMap, output_dir: str | Path,
                         resolution: int | None = None) -> dict:
    """Write diagnostic PNGs and return their machine-readable statistics."""
    if resolution is not None and resolution <= 0:
        raise ValueError("diagnostics resolution must be positive")
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    stats: dict = {
        "world_size": world.size,
        "output_resolution": resolution,
        "resolution_mode": "uniform" if resolution is not None else "native_layers",
        "orientation_validation": validate_orientation(world),
        "layers": {},
    }

    for name in ("terrain", "height", "water"):
        raw = int16_grid(world, name)
        metres = world_height_grid(world, name)
        _save_gray(output / f"{name}.png", _height_preview(metres), resolution)
        stats["layers"][name] = {
            "shape": list(raw.shape), "dtype": "int16-le",
            "raw_min": int(raw.min()), "raw_max": int(raw.max()),
            "world_y_min_m": float(metres.min()), "world_y_max_m": float(metres.max()),
        }

    alpha = uint8_grid(world, "alpha")
    _save_gray(output / "alpha.png", alpha, resolution)
    stats["layers"]["alpha"] = {
        "shape": list(alpha.shape), "dtype": "uint8",
        "min": int(alpha.min()), "max": int(alpha.max()),
        "visible_pixels": int(np.count_nonzero(alpha)),
    }

    splat = splat_grid(world)
    splat_stats = {}
    for index, name in enumerate(SPLAT_CHANNELS[: splat.shape[0]]):
        _save_gray(output / f"splat_{index}_{name}.png", splat[index], resolution)
        splat_stats[name] = {
            "min": int(splat[index].min()), "max": int(splat[index].max()),
            "nonzero_pixels": int(np.count_nonzero(splat[index])),
        }
    splat_colors = np.array([
        [105, 72, 45], [225, 238, 245], [218, 190, 120], [95, 95, 95],
        [92, 145, 65], [45, 95, 48], [125, 120, 110], [145, 135, 120],
    ])
    splat_composite = Image.fromarray(
        _dominant_color(splat, splat_colors[: splat.shape[0]])[::-1, :], mode="RGB"
    )
    save_png(
        _resize_diagnostic(splat_composite, resolution, Image.Resampling.BILINEAR),
        output / "splat_composite.png",
    )
    stats["layers"]["splat"] = {
        "shape": list(splat.shape), "dtype": "uint8-channel-major",
        "pixel_sum_min": int(splat.astype(np.uint16).sum(axis=0).min()),
        "pixel_sum_max": int(splat.astype(np.uint16).sum(axis=0).max()),
        "channels": splat_stats,
    }

    biome = biome_grid(world)
    biome_stats = {}
    for index, name in enumerate(BIOME_CHANNELS[: biome.shape[0]]):
        _save_gray(output / f"biome_{index}_{name}.png", biome[index], resolution)
        biome_stats[name] = {
            "min": int(biome[index].min()), "max": int(biome[index].max()),
            "nonzero_pixels": int(np.count_nonzero(biome[index])),
        }
    biome_colors = np.array([
        [210, 135, 60], [90, 155, 75], [105, 125, 95], [220, 235, 245], [35, 120, 58]
    ])
    biome_composite = Image.fromarray(
        _dominant_color(biome, biome_colors[: biome.shape[0]])[::-1, :], mode="RGB"
    )
    save_png(
        _resize_diagnostic(biome_composite, resolution, Image.Resampling.BILINEAR),
        output / "biome_composite.png",
    )
    stats["layers"]["biome"] = {
        "shape": list(biome.shape), "dtype": "uint8-channel-major",
        "pixel_sum_min": int(biome.astype(np.uint16).sum(axis=0).min()),
        "pixel_sum_max": int(biome.astype(np.uint16).sum(axis=0).max()),
        "channels": biome_stats,
    }

    topology = topology_grid(world)
    topology_stats = {}
    for bit, name in enumerate(TOPOLOGY_BITS):
        mask = (topology & np.uint32(1 << bit)) != 0
        _save_gray(
            output / f"topology_{bit:02d}_{name}.png",
            mask.astype(np.uint8) * 255,
            resolution,
            Image.Resampling.NEAREST,
        )
        topology_stats[name] = {"bit": bit, "set_pixels": int(np.count_nonzero(mask))}
    stats["layers"]["topology"] = {
        "shape": list(topology.shape), "dtype": "uint32-le-bitmask",
        "channels": topology_stats,
    }

    # Overlay path nodes and monument prefab positions on the height map. This
    # uses the exact same raw [z,x] transform verified by elevation sampling.
    preview = Image.fromarray(_height_preview(world_height_grid(world, "height")), mode="L").convert("RGB")
    preview = _resize_diagnostic(preview, resolution, Image.Resampling.BILINEAR)
    draw = ImageDraw.Draw(preview)
    resolution = preview.width
    for path in world.paths:
        points = [world_to_grid(world, node.x, node.z, resolution) for node in path.nodes]
        if len(points) >= 2:
            draw.line(points, fill=(255, 80, 40), width=max(1, resolution // 1024))
    radius = max(2, resolution // 512)
    for prefab in world.prefabs:
        if prefab.category.casefold() != "monument" or prefab.position is None:
            continue
        x, z = world_to_grid(world, prefab.position.x, prefab.position.z, resolution)
        draw.ellipse((x - radius, z - radius, x + radius, z + radius), fill=(40, 190, 255))
    save_png(
        preview.transpose(Image.Transpose.FLIP_TOP_BOTTOM),
        output / "orientation_paths_monuments.png",
    )

    (output / "diagnostics.json").write_text(
        json.dumps(stats, indent=2) + "\n", encoding="utf-8"
    )
    return stats
