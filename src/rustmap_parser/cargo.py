"""Reconstruct and export Rust's generated cargo-ship navigation paths."""

from __future__ import annotations

import json
import math
import time
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .layers import sample_bilinear, world_height_grid, world_to_grid
from .png import save_png
from .prefabs import PrefabManifest
from .renderer import _signed_distance
from .tunnels import _instance_matrix

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*args, **kwargs):
        def decorate(fn):
            return fn
        return decorate


SCHEMA_VERSION = 3
ORIENTATION = "flip_vertical (reverse Z; preserve X left/right)"
HARBOR_PREFIX = "assets/bundled/prefabs/autospawn/monument/harbor/"
CARGO_COLLISION_RESOURCE = "cargo_collision_tiles"
COLLISION_GRID_MARGIN_M = 260.0
COLLISION_GRID_PIXELS_PER_METER = 1.0
CARGO_FIXED_DELTA_SECONDS = 0.05
CARGO_WAYPOINT_REACHED_M = 80.0
CARGO_TURN_RATE_DEGREES_PER_SECOND = 2.5
CARGO_MAX_SPEED_METRES_PER_SECOND = 8.0
CARGO_CONTROL_LERP_PER_SECOND = 0.2
CARGO_TRAJECTORY_SAMPLE_SECONDS = 0.25
CARGO_TRAJECTORY_TOLERANCE_M = 0.5
CARGO_TRAJECTORY_MAX_STEPS = 500_000


@njit(cache=True)
def _sample_clearance(clearance: np.ndarray, x: np.float32, z: np.float32,
                      world_size: np.float32) -> np.float32:
    """Bilinearly sample metres-to-collider; outside TerrainCollider is clear."""
    half = world_size * np.float32(0.5)
    if x < -half or x > half or z < -half or z > half:
        return np.float32(1.0e9)
    last = clearance.shape[0] - 1
    gx = (x / world_size + np.float32(0.5)) * last
    gz = (z / world_size + np.float32(0.5)) * last
    x0 = min(max(int(gx), 0), last)
    z0 = min(max(int(gz), 0), last)
    x1 = min(x0 + 1, last)
    z1 = min(z0 + 1, last)
    tx = np.float32(gx - x0)
    tz = np.float32(gz - z0)
    top = clearance[z0, x0] + (clearance[z0, x1] - clearance[z0, x0]) * tx
    bottom = clearance[z1, x0] + (clearance[z1, x1] - clearance[z1, x0]) * tx
    return np.float32(top + (bottom - top) * tz)


@njit(cache=True)
def _server_style_nodes(clearance: np.ndarray, world_size: np.float32,
                        node_spacing: np.float32 = np.float32(30.0),
                        inward_step: np.float32 = np.float32(4.0),
                        neighbor_limit: np.float32 = np.float32(200.0),
                        required_clearance: np.float32 = np.float32(203.0),
                        max_iterations: int = 100000) -> tuple[np.ndarray, int, bool]:
    """Port BaseBoat's ordered inward relaxation using a terrain clearance field."""
    count = int(math.ceil(float(world_size * np.float32(2.0 * math.pi) / node_spacing)))
    nodes = np.empty((count, 3), dtype=np.float32)
    for index in range(count):
        angle = np.float32(index / count * np.float32(2.0 * math.pi))
        nodes[index, 0] = np.float32(math.sin(float(angle))) * world_size
        nodes[index, 1] = np.float32(0.0)
        nodes[index, 2] = np.float32(math.cos(float(angle))) * world_size

    neighbor_limit2 = neighbor_limit * neighbor_limit
    for iteration in range(max_iterations):
        changed = False
        for index in range(count):
            x = nodes[index, 0]
            z = nodes[index, 2]
            length = np.float32(math.sqrt(float(x * x + z * z)))
            if length <= inward_step:
                continue
            candidate_x = x - x / length * inward_step
            candidate_z = z - z / length * inward_step
            previous = count - 1 if index == 0 else index - 1
            following = 0 if index == count - 1 else index + 1
            pdx = candidate_x - nodes[previous, 0]
            pdz = candidate_z - nodes[previous, 2]
            ndx = candidate_x - nodes[following, 0]
            ndz = candidate_z - nodes[following, 2]
            if pdx * pdx + pdz * pdz > neighbor_limit2:
                continue
            if ndx * ndx + ndz * ndz > neighbor_limit2:
                continue
            if _sample_clearance(clearance, x, z, world_size) <= required_clearance:
                continue
            nodes[index, 0] = candidate_x
            nodes[index, 2] = candidate_z
            changed = True
        if not changed:
            return nodes, iteration + 1, True
    return nodes, max_iterations, False


@njit(cache=True)
def _sample_terrain_height(heights: np.ndarray, x: np.float32, z: np.float32,
                           world_size: np.float32) -> np.float32:
    """Sample the serialized Unity TerrainCollider height; no terrain exists outside."""
    half = world_size * np.float32(0.5)
    if x < -half or x > half or z < -half or z > half:
        return np.float32(-1.0e9)
    last = heights.shape[0] - 1
    gx = (x / world_size + np.float32(0.5)) * last
    gz = (z / world_size + np.float32(0.5)) * last
    x0 = min(max(int(gx), 0), last)
    z0 = min(max(int(gz), 0), last)
    x1 = min(x0 + 1, last)
    z1 = min(z0 + 1, last)
    tx = np.float32(gx - x0)
    tz = np.float32(gz - z0)
    top = heights[z0, x0] + (heights[z0, x1] - heights[z0, x0]) * tx
    bottom = heights[z1, x0] + (heights[z1, x1] - heights[z1, x0]) * tx
    return np.float32(top + (bottom - top) * tz)


@njit(cache=True)
def _terrain_sphere_cast_hits(heights: np.ndarray, world_size: np.float32,
                              origin_x: np.float32, origin_z: np.float32,
                              direction_x: np.float32, direction_z: np.float32,
                              distance: np.float32 = np.float32(200.0),
                              radius: np.float32 = np.float32(3.0)) -> bool:
    """Sweep Rust's radius-3 sphere horizontally over the terrain heightfield.

    Five parallel height probes reproduce the circular X/Z cross-section. A
    one-metre longitudinal step is smaller than the native terrain spacing on
    normal Rust worlds and prevents gaps between successive sphere positions.
    """
    perpendicular_x = -direction_z
    perpendicular_z = direction_x
    offsets = (-3.0, -1.5, 0.0, 1.5, 3.0)
    allowances = (0.0, 2.598076211, 3.0, 2.598076211, 0.0)
    steps = int(math.ceil(float(distance)))
    for step in range(1, steps + 1):
        amount = min(np.float32(step), distance)
        center_x = origin_x + direction_x * amount
        center_z = origin_z + direction_z * amount
        for probe in range(5):
            offset = np.float32(offsets[probe])
            x = center_x + perpendicular_x * offset
            z = center_z + perpendicular_z * offset
            if _sample_terrain_height(heights, x, z, world_size) >= -np.float32(allowances[probe]):
                return True
    return False


@njit(cache=True)
def _prefab_sphere_cast_hits(obstacles: np.ndarray, obstacle_min: np.float32,
                             pixels_per_metre: np.float32,
                             origin_x: np.float32, origin_z: np.float32,
                             direction_x: np.float32, direction_z: np.float32,
                             distance: np.float32 = np.float32(200.0)) -> bool:
    """Test a cast against pre-expanded prefab collision footprints."""
    steps = int(math.ceil(float(distance * np.float32(2.0))))
    for step in range(1, steps + 1):
        amount = min(np.float32(step) * np.float32(0.5), distance)
        x = origin_x + direction_x * amount
        z = origin_z + direction_z * amount
        column = int(math.floor(float((x - obstacle_min) * pixels_per_metre)))
        row = int(math.floor(float((z - obstacle_min) * pixels_per_metre)))
        if (0 <= row < obstacles.shape[0] and 0 <= column < obstacles.shape[1]
                and obstacles[row, column] != 0):
            return True
    return False


@njit(cache=True)
def _server_sphere_casts_clear(heights: np.ndarray, obstacles: np.ndarray,
                               obstacle_min: np.float32,
                               obstacle_pixels_per_metre: np.float32,
                               world_size: np.float32,
                               x: np.float32, z: np.float32) -> bool:
    """Literal 16 fixed-world-direction clearance loop from BaseBoat.cs."""
    for index in range(16):
        angle = np.float32(index / 16.0 * np.float32(2.0 * math.pi))
        direction_x = np.float32(math.sin(float(angle)))
        direction_z = np.float32(math.cos(float(angle)))
        if _terrain_sphere_cast_hits(
            heights, world_size, x, z, direction_x, direction_z
        ):
            return False
        if _prefab_sphere_cast_hits(
            obstacles, obstacle_min, obstacle_pixels_per_metre,
            x, z, direction_x, direction_z,
        ):
            return False
    return True


@njit(cache=True)
def _server_style_nodes_directional(clearance: np.ndarray, heights: np.ndarray,
                                    obstacles: np.ndarray,
                                    obstacle_bounds: np.ndarray,
                                    obstacle_min: np.float32,
                                    obstacle_pixels_per_metre: np.float32,
                                    world_size: np.float32,
                                    max_iterations: int = 100000) -> tuple[np.ndarray, int, bool, int]:
    """BaseBoat relaxation with its actual 16-direction terrain sphere sweeps."""
    node_spacing = np.float32(30.0)
    inward_step = np.float32(4.0)
    neighbor_limit2 = np.float32(200.0 * 200.0)
    count = int(math.ceil(float(world_size * np.float32(2.0 * math.pi) / node_spacing)))
    nodes = np.empty((count, 3), dtype=np.float32)
    for index in range(count):
        angle = np.float32(index / count * np.float32(2.0 * math.pi))
        nodes[index, 0] = np.float32(math.sin(float(angle))) * world_size
        nodes[index, 1] = np.float32(0.0)
        nodes[index, 2] = np.float32(math.cos(float(angle))) * world_size

    exact_sweep_count = 0
    for iteration in range(max_iterations):
        changed = False
        for index in range(count):
            x = nodes[index, 0]
            z = nodes[index, 2]
            length = np.float32(math.sqrt(float(x * x + z * z)))
            if length <= inward_step:
                continue
            candidate_x = x - x / length * inward_step
            candidate_z = z - z / length * inward_step
            previous = count - 1 if index == 0 else index - 1
            following = 0 if index == count - 1 else index + 1
            pdx = candidate_x - nodes[previous, 0]
            pdz = candidate_z - nodes[previous, 2]
            ndx = candidate_x - nodes[following, 0]
            ndz = candidate_z - nodes[following, 2]
            if pdx * pdx + pdz * pdz > neighbor_limit2:
                continue
            if ndx * ndx + ndz * ndz > neighbor_limit2:
                continue

            # More than 220 m from projected TerrainCollider geometry guarantees
            # that a 200 m, radius-3 sweep cannot hit it. Near a collider, use
            # Rust's discrete 16-direction test. The extra margin covers
            # heightfield sampling between serialized vertices.
            near_terrain = _sample_clearance(clearance, x, z, world_size) <= np.float32(220.0)
            near_prefab = False
            for bound_index in range(obstacle_bounds.shape[0]):
                dx = x - obstacle_bounds[bound_index, 0]
                dz = z - obstacle_bounds[bound_index, 1]
                reach = obstacle_bounds[bound_index, 2] + np.float32(203.0)
                if dx * dx + dz * dz <= reach * reach:
                    near_prefab = True
                    break
            if near_terrain or near_prefab:
                exact_sweep_count += 1
                if not _server_sphere_casts_clear(
                    heights, obstacles, obstacle_min,
                    obstacle_pixels_per_metre, world_size, x, z,
                ):
                    continue
            nodes[index, 0] = candidate_x
            nodes[index, 2] = candidate_z
            changed = True
        if not changed:
            return nodes, iteration + 1, True, exact_sweep_count
    return nodes, max_iterations, False, exact_sweep_count


def _terrain_collider_clearance(world) -> np.ndarray:
    """Return clearance from terrain touched by Rust's patrol sphere casts.

    The server casts at world Y=0 with a three-metre-radius sphere. Shallow
    submerged terrain down to Y=-3 is therefore an obstacle even though it is
    drawn as water on the map. A shoreline-only field misses those shoals and
    pulls the reconstructed route visibly too close to some coasts.
    """
    heights = np.asarray(world_height_grid(world), dtype=np.float32)
    collider_projection = np.where(
        heights >= np.float32(-3.0), 255, 0
    ).astype(np.uint8)
    pixel_clearance = _signed_distance(collider_projection)
    metres_per_pixel = np.float32(world.size / (heights.shape[0] - 1))
    return np.asarray(pixel_clearance * metres_per_pixel, dtype=np.float32)


def _load_collision_tiles() -> tuple[dict, object]:
    directory = resources.files("rustmap_parser.data").joinpath(CARGO_COLLISION_RESOURCE)
    metadata = json.loads(directory.joinpath("tiles.json").read_text(encoding="utf-8"))
    return metadata, directory


def _prefab_collision_grid(world, manifest: PrefabManifest | None) -> tuple[np.ndarray, np.ndarray, float, dict]:
    """Place packaged, sphere-expanded world-prefab collision masks."""
    ppm = COLLISION_GRID_PIXELS_PER_METER
    half_extent = world.size * 0.5 + COLLISION_GRID_MARGIN_M
    minimum = -half_extent
    side = int(math.ceil(half_extent * 2.0 * ppm)) + 1
    grid = np.zeros((side, side), dtype=np.uint8)
    if manifest is None:
        return grid, np.empty((0, 3), dtype=np.float32), minimum, {
            "status": "unavailable", "reason": "prefab_manifest_not_provided",
            "placed_instance_count": 0, "packaged_template_count": 0,
        }
    try:
        metadata, directory = _load_collision_tiles()
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError) as exc:
        return grid, np.empty((0, 3), dtype=np.float32), minimum, {
            "status": "unavailable", "reason": f"{type(exc).__name__}: {exc}",
            "placed_instance_count": 0, "packaged_template_count": 0,
        }

    templates = {
        item["prefab_path"].casefold(): item for item in metadata.get("templates", [])
    }
    selected_prefixes = tuple(
        str(value).casefold() for value in
        metadata.get("selection", {}).get("prefab_prefixes", [])
    )
    point_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    placed = 0
    bounds: list[tuple[float, float, float]] = []
    missing: set[str] = set()
    unpackaged: set[str] = set()
    for prefab in world.prefabs:
        entry = manifest.get(prefab.prefab_id)
        path = entry.path.casefold().replace("\\", "/") if entry else ""
        template = templates.get(path)
        if template is None or prefab.position is None:
            if (prefab.position is not None and selected_prefixes and
                    path.startswith(selected_prefixes)):
                unpackaged.add(path)
            continue
        key = template["mask_file"]
        points = point_cache.get(key)
        if points is None:
            try:
                with directory.joinpath(key).open("rb") as stream:
                    mask = np.asarray(Image.open(stream).convert("L"), dtype=np.uint8)
            except (FileNotFoundError, OSError):
                missing.add(path)
                continue
            rows, columns = np.nonzero(mask)
            source_ppm = float(template["pixels_per_metre"])
            local_x = float(template["left_x_m"]) + (columns + 0.5) / source_ppm
            local_z = float(template["top_z_m"]) - (rows + 0.5) / source_ppm
            points = (local_x.astype(np.float32), local_z.astype(np.float32))
            point_cache[key] = points

        local_x, local_z = points
        heading = float(prefab.rotation.y) if prefab.rotation is not None else 0.0
        radians = math.radians(heading)
        cosine, sine = math.cos(radians), math.sin(radians)
        scale_x = float(prefab.scale.x) if prefab.scale is not None else 1.0
        scale_z = float(prefab.scale.z) if prefab.scale is not None else 1.0
        x = prefab.position.x + cosine * local_x * scale_x + sine * local_z * scale_z
        z = prefab.position.z - sine * local_x * scale_x + cosine * local_z * scale_z
        columns = np.floor((x - minimum) * ppm).astype(np.int32)
        rows = np.floor((z - minimum) * ppm).astype(np.int32)
        valid = ((rows >= 0) & (rows < side) & (columns >= 0) & (columns < side))
        grid[rows[valid], columns[valid]] = 255
        radius = float(np.max(np.sqrt(
            (local_x * scale_x) ** 2 + (local_z * scale_z) ** 2
        ))) if len(local_x) else 0.0
        bounds.append((float(prefab.position.x), float(prefab.position.z), radius))
        placed += 1

    source = metadata.get("source", {})
    source_content = source.get("bundles", {}).get("content", {})
    # Installed/copy operations commonly rewrite bundle mtimes without changing
    # the bundle bytes. The manifest currently carries no Rust build ID, so size
    # is the stable compatibility signal available to normal installed users.
    mismatch = bool(
        int(source_content.get("size", -1)) != int(manifest.source_size)
    )
    return grid, np.asarray(bounds, dtype=np.float32).reshape((-1, 3)), minimum, {
        "status": "loaded", "resource": f"rustmap_parser.data/{CARGO_COLLISION_RESOURCE}",
        "schema_version": metadata.get("schema_version"),
        "source_rust_build_id": source.get("rust_build_id"),
        "version_mismatch": mismatch,
        "packaged_template_count": len(templates),
        "loaded_template_count": len(point_cache),
        "placed_instance_count": placed,
        "missing_templates": sorted(missing),
        "unpackaged_prefabs": sorted(unpackaged),
        "grid_pixels_per_metre": ppm,
        "sphere_radius_preexpanded_m": metadata.get("sphere_radius_preexpanded_m"),
    }


def _rdp_indices(points: np.ndarray, tolerance: float) -> list[int]:
    """Unity LineUtility.Simplify-compatible Ramer-Douglas-Peucker indices."""
    if len(points) <= 2:
        return list(range(len(points)))
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    tolerance2 = tolerance * tolerance
    while stack:
        start, end = stack.pop()
        a, b = points[start], points[end]
        delta = b - a
        length2 = float(delta @ delta)
        best_index, best_distance2 = -1, -1.0
        for index in range(start + 1, end):
            point = points[index]
            if length2 == 0.0:
                distance2 = float((point - a) @ (point - a))
            else:
                amount = max(0.0, min(1.0, float((point - a) @ delta) / length2))
                nearest = a + delta * amount
                distance2 = float((point - nearest) @ (point - nearest))
            if distance2 > best_distance2:
                best_index, best_distance2 = index, distance2
        if best_index >= 0 and best_distance2 > tolerance2:
            keep.add(best_index)
            stack.append((start, best_index))
            stack.append((best_index, end))
    return sorted(keep)


def generate_cargo_patrol_path(world, manifest: PrefabManifest | None = None) -> tuple[np.ndarray, dict]:
    """Generate the startup patrol loop from terrain and placed world prefabs."""
    started = time.perf_counter()
    clearance_started = time.perf_counter()
    clearance = _terrain_collider_clearance(world)
    clearance_seconds = time.perf_counter() - clearance_started
    prefab_started = time.perf_counter()
    prefab_obstacles, obstacle_bounds, obstacle_min, prefab_collision = _prefab_collision_grid(
        world, manifest
    )
    prefab_seconds = time.perf_counter() - prefab_started
    generation_started = time.perf_counter()
    terrain_heights = np.asarray(world_height_grid(world), dtype=np.float32)
    raw, iterations, converged, directional_sweep_count = (
        _server_style_nodes_directional(
            clearance, terrain_heights, prefab_obstacles, obstacle_bounds,
            np.float32(obstacle_min),
            np.float32(COLLISION_GRID_PIXELS_PER_METER),
            np.float32(world.size)
        )
    )
    generation_seconds = time.perf_counter() - generation_started
    simplify_started = time.perf_counter()
    indices = _rdp_indices(raw[:, (0, 2)], 5.0)
    nodes = np.ascontiguousarray(raw[indices])
    simplify_seconds = time.perf_counter() - simplify_started
    return nodes, {
        "accuracy": "world_setup_collision_reconstructed",
        "collision_model": (
            "16-direction, radius-3, 200 m sphere sweeps over serialized "
            "TerrainCollider geometry and packaged placed-prefab collision footprints"
        ),
        "prefab_collision": prefab_collision,
        "raw_node_count": int(len(raw)),
        "simplified_node_count": int(len(nodes)),
        "iterations": int(iterations),
        "converged": bool(converged),
        "directional_sweep_count": int(directional_sweep_count),
        "timings": {
            "terrain_collider_clearance_seconds": clearance_seconds,
            "prefab_collision_grid_seconds": prefab_seconds,
            "relaxation_seconds": generation_seconds,
            "simplification_seconds": simplify_seconds,
            "total_seconds": time.perf_counter() - started,
        },
    }


def _load_harbor_data() -> tuple[dict, str]:
    item = resources.files("rustmap_parser.data").joinpath("cargo_harbor_paths.json")
    return json.loads(item.read_text(encoding="utf-8")), (
        "rustmap_parser.data/cargo_harbor_paths.json"
    )


def _point_document(point: np.ndarray, world_size: int) -> dict:
    return {
        "position": {
            "x": float(point[0]), "y": float(point[1]), "z": float(point[2]),
        },
        "map_position": {
            "x": float(point[0] + world_size / 2.0),
            "y": float(point[2] + world_size / 2.0),
        },
    }


def _terrain_line_of_sight(world, start: np.ndarray, end: np.ndarray) -> bool:
    """Approximate GamePhysics.LineOfSightRadius at Y+3 using terrain height."""
    distance = float(np.linalg.norm(end[[0, 2]] - start[[0, 2]]))
    samples = max(2, int(math.ceil(distance / 2.0)))
    heights = world_height_grid(world)
    for amount in np.linspace(0.0, 1.0, samples, dtype=np.float32):
        point = start + (end - start) * amount
        gx, gz = world_to_grid(world, float(point[0]), float(point[2]), heights.shape[0])
        if sample_bilinear(heights, gx, gz) >= float(point[1]):
            return False
    return True


def _harbor_approaches(world, manifest: PrefabManifest, patrol: np.ndarray,
                       database: dict) -> tuple[list[dict], list[dict]]:
    definitions = {
        item["prefab_path"].casefold(): item for item in database.get("prefabs", [])
    }
    approaches: list[dict] = []
    skipped: list[dict] = []
    for prefab in world.prefabs:
        entry = manifest.get(prefab.prefab_id)
        path = entry.path.casefold().replace("\\", "/") if entry else ""
        if not path.startswith(HARBOR_PREFIX) or prefab.position is None:
            continue
        definition = definitions.get(path)
        if definition is None:
            # Ferry Terminal shares the harbor folder but does not register a
            # CargoNotifier docking BasePath. Only harbor_* prefabs are expected
            # to participate in CargoShip.harbors.
            if Path(path).stem.startswith("harbor_"):
                skipped.append({"prefab_path": path, "reason": "missing_packaged_harbor_path"})
            continue
        matrix = _instance_matrix(prefab)
        local = np.asarray([
            [float(node["position"]["x"]), float(node["position"]["y"]),
             float(node["position"]["z"]), 1.0]
            for node in definition["nodes"]
        ], dtype=np.float64)
        world_nodes = (matrix @ local.T).T[:, :3]
        first = world_nodes[0]
        best_index, best_score = -1, float("inf")
        candidates = sorted(
            (float(np.linalg.norm(point - first)), index)
            for index, point in enumerate(patrol)
        )
        for distance, index in candidates:
            # A farther node cannot beat an already visible nearer node, and a
            # blocked node only increases its score by the server's 20x penalty.
            if distance >= best_score:
                break
            point = patrol[index]
            score = distance if _terrain_line_of_sight(
                world, point + np.asarray((0.0, 3.0, 0.0)),
                first + np.asarray((0.0, 3.0, 0.0)),
            ) else distance * 20.0
            if score < best_score:
                best_index, best_score = index, score
        all_nodes = np.vstack((patrol[best_index], world_nodes))
        approaches.append({
            "prefab_path": path,
            "approach_patrol_node_index": int(best_index),
            "nodes": [_point_document(point, world.size) for point in all_nodes],
            "node_speeds": [None] + [
                float(node.get("max_velocity_on_approach", 0.0))
                for node in definition["nodes"]
            ],
        })
    approaches.sort(key=lambda item: (
        item["prefab_path"], item["nodes"][1]["position"]["x"],
        item["nodes"][1]["position"]["z"],
    ))
    return approaches, skipped


def _smooth_patrol_nodes(patrol: np.ndarray,
                         sample_count: int | None = None) -> np.ndarray:
    """Simulate CargoShip.UpdateMovement to produce its smooth patrol track.

    Rust does not spline OceanPatrolFar. The ship steers toward decreasing node
    indices, accepts a waypoint from 80 metres away, and is limited to a 2.5
    degree-per-second turn. Simulating those controls produces the rounded,
    corner-cutting centreline seen in game. ``sample_count`` remains accepted
    for source compatibility but no longer controls the trajectory density.
    """
    del sample_count
    if len(patrol) < 3:
        return np.ascontiguousarray(patrol)

    waypoints = np.asarray(patrol[:, (0, 2)], dtype=np.float64)
    position = waypoints[0].copy()
    target_index = len(waypoints) - 1
    initial_direction = waypoints[target_index] - position
    initial_length = float(np.linalg.norm(initial_direction))
    if initial_length <= 1.0e-9:
        return np.ascontiguousarray(patrol)
    forward = initial_direction / initial_length
    throttle = 1.0
    turn_scale = 0.0
    completed_loops = 0
    sample_every = max(1, int(round(
        CARGO_TRAJECTORY_SAMPLE_SECONDS / CARGO_FIXED_DELTA_SECONDS
    )))
    collected: list[np.ndarray] = []

    for step in range(CARGO_TRAJECTORY_MAX_STEPS):
        to_target = waypoints[target_index] - position
        distance = float(np.linalg.norm(to_target))
        if distance <= 1.0e-9:
            desired = forward.copy()
        else:
            desired = to_target / distance

        # CargoShip.UpdateShip uses transform.right dot desired direction to
        # choose the turn side and eases both steering and throttle at 0.2/s.
        right = np.asarray((forward[1], -forward[0]), dtype=np.float64)
        side = float(right @ desired)
        turn_demand = float(np.clip((abs(side) - 0.05) / 0.45, 0.0, 1.0))
        if turn_demand == 0.0 and float(desired @ -forward) >= 0.95:
            turn_demand = 1.0
        lerp = min(1.0, CARGO_FIXED_DELTA_SECONDS * CARGO_CONTROL_LERP_PER_SECOND)
        turn_scale += (turn_demand - turn_scale) * lerp
        turn_sign = -1.0 if side < 0.0 else 1.0
        yaw = math.radians(
            CARGO_TURN_RATE_DEGREES_PER_SECOND * turn_scale * turn_sign *
            CARGO_FIXED_DELTA_SECONDS
        )
        cosine, sine = math.cos(yaw), math.sin(yaw)
        forward = np.asarray((
            forward[0] * cosine + forward[1] * sine,
            -forward[0] * sine + forward[1] * cosine,
        ), dtype=np.float64)
        forward /= max(float(np.linalg.norm(forward)), 1.0e-12)

        desired_throttle = float(np.clip(forward @ desired, 0.0, 1.0))
        throttle += (desired_throttle - throttle) * lerp
        position += (
            forward * CARGO_MAX_SPEED_METRES_PER_SECOND * throttle *
            CARGO_FIXED_DELTA_SECONDS
        )

        if completed_loops == 1 and step % sample_every == 0:
            collected.append(position.copy())

        if float(np.linalg.norm(waypoints[target_index] - position)) < CARGO_WAYPOINT_REACHED_M:
            if target_index == 0:
                completed_loops += 1
                if completed_loops == 2:
                    break
            target_index = (target_index - 1 + len(waypoints)) % len(waypoints)
    else:
        raise RuntimeError("Cargo patrol trajectory simulation did not complete two loops")

    if len(collected) < 3:
        return np.ascontiguousarray(patrol)
    # Collected points follow the ship's decreasing-index travel direction.
    # Reverse them so the exported array retains the source convention and the
    # existing decreasing_node_index metadata remains true.
    points = np.asarray(collected[::-1], dtype=np.float32)
    closed = np.vstack((points, points[0]))
    indices = _rdp_indices(closed, CARGO_TRAJECTORY_TOLERANCE_M)
    indices = [index for index in indices if index < len(points)]
    smoothed = np.zeros((len(indices), 3), dtype=np.float32)
    smoothed[:, (0, 2)] = points[indices]
    return smoothed


def _reconnect_harbor_approaches(approaches: list[dict], patrol: np.ndarray,
                                 world_size: int) -> None:
    """Reconnect exact harbor paths to the nearest exported patrol node."""
    if not len(patrol):
        return
    for approach in approaches:
        position = approach["nodes"][0]["position"]
        source = np.asarray(
            (position["x"], position["y"], position["z"]), dtype=np.float32
        )
        index = int(np.argmin(np.sum((patrol - source) ** 2, axis=1)))
        approach["approach_patrol_node_index"] = index
        approach["nodes"][0] = _point_document(patrol[index], world_size)


def build_cargo_ship_path_export(world, manifest: PrefabManifest,
                                 resolution: int | None = None,
                                 smooth_patrol: bool = False,
                                 ) -> tuple[dict, np.ndarray]:
    resolution = world.size if resolution is None else int(resolution)
    patrol, generation = generate_cargo_patrol_path(world, manifest)
    harbor_data, resource_name = _load_harbor_data()
    approaches, skipped = _harbor_approaches(world, manifest, patrol, harbor_data)
    source_patrol_count = len(patrol)
    if smooth_patrol:
        patrol = _smooth_patrol_nodes(patrol, generation["raw_node_count"])
        _reconnect_harbor_approaches(approaches, patrol, world.size)
    source_content = harbor_data.get("source", {}).get("bundles", {}).get("content", {})
    version_mismatch = bool(
        int(source_content.get("size", -1)) != int(manifest.source_size)
    )
    warnings = []
    if version_mismatch:
        warnings.append("cargo_harbor_data_version_mismatch")
    if skipped:
        warnings.append("harbor_paths_missing_for_some_placed_harbors")
    prefab_collision = generation.get("prefab_collision", {})
    if prefab_collision.get("status") != "loaded":
        warnings.append("cargo_prefab_collision_data_unavailable")
    if prefab_collision.get("version_mismatch"):
        warnings.append("cargo_prefab_collision_data_version_mismatch")
    if prefab_collision.get("missing_templates"):
        warnings.append("cargo_prefab_collision_templates_missing")
    if prefab_collision.get("unpackaged_prefabs"):
        warnings.append("cargo_prefab_collision_prefabs_unpackaged")
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": "rendered" if len(patrol) else "skipped",
        "map": {
            "serialization_version": int(world.serialization_version),
            "timestamp": int(world.timestamp), "world_size": int(world.size),
        },
        "resolution": [resolution, resolution],
        "orientation": ORIENTATION,
        "coordinates": {
            "world": "centered Unity X/Y/Z metres",
            "map_position": "bottom-left origin in world metres",
            "image": "X left-to-right; positive world Z points upward",
        },
        "generation": {
            **generation,
            "server_constants": {
                "initial_radius": int(world.size), "initial_node_spacing_m": 30.0,
                "inward_step_m": 4.0, "neighbor_limit_m": 200.0,
                "sphere_cast_radius_m": 3.0, "sphere_cast_distance_m": 200.0,
                "sphere_cast_directions": 16, "simplification_tolerance_m": 5.0,
                "maximum_iterations": 100000,
            },
            "limitations": [
                "offline reconstruction cannot reproduce every Unity monument and underwater-lab collider",
                "only colliders present during WorldSetup affect this route; later SpawnHandler populations and loaded saves do not",
                "smooth patrol simulates normal ocean steering only; harbor docking, spawn transients, and egress are separate runtime states",
            ],
        },
        "patrol": {
            "closed": True, "travel_direction": "decreasing_node_index",
            "source_node_count": int(source_patrol_count),
            "node_count": int(len(patrol)),
            "smoothing": {
                "enabled": bool(smooth_patrol),
                "algorithm": (
                    "rust_cargo_update_movement_simulation"
                    if smooth_patrol else None
                ),
                "fixed_delta_seconds": (
                    CARGO_FIXED_DELTA_SECONDS if smooth_patrol else None
                ),
                "waypoint_reached_distance_m": (
                    CARGO_WAYPOINT_REACHED_M if smooth_patrol else None
                ),
                "turn_rate_degrees_per_second": (
                    CARGO_TURN_RATE_DEGREES_PER_SECOND if smooth_patrol else None
                ),
                "maximum_speed_m_per_second": (
                    CARGO_MAX_SPEED_METRES_PER_SECOND if smooth_patrol else None
                ),
                "control_lerp_per_second": (
                    CARGO_CONTROL_LERP_PER_SECOND if smooth_patrol else None
                ),
                "trajectory_sample_seconds": (
                    CARGO_TRAJECTORY_SAMPLE_SECONDS if smooth_patrol else None
                ),
                "final_simplification_tolerance_m": (
                    CARGO_TRAJECTORY_TOLERANCE_M if smooth_patrol else None
                ),
            },
            "nodes": [_point_document(point, world.size) for point in patrol],
        },
        "harbor_approaches": approaches,
        "harbor_approach_count": len(approaches),
        "skipped_harbors": skipped,
        "packaged_harbor_data": {
            "resource": resource_name,
            "schema_version": harbor_data.get("schema_version"),
            "source": harbor_data.get("source"),
            "version_mismatch": version_mismatch,
        },
        "warnings": warnings,
    }
    return document, patrol


def _image_point(point: dict, resolution: int, world_size: int,
                 supersample: int) -> tuple[float, float]:
    map_position = point["map_position"]
    scale = resolution * supersample / world_size
    return map_position["x"] * scale, (world_size - map_position["y"]) * scale


def _draw_paths(document: dict, patrol_color, harbor_color,
                line_width: int) -> Image.Image:
    resolution = int(document["resolution"][0])
    world_size = int(document["map"]["world_size"])
    # At the native one-pixel-per-metre output, a 4 px path already has enough
    # coverage for a clean map overlay. Avoid an 8500² temporary RGBA image,
    # which would add hundreds of MiB and tens of seconds just to downsample it.
    supersample = 1
    # A three-entry indexed image encodes this sparse layer dramatically faster
    # than filtering four full RGBA channels, while retaining exact per-entry
    # alpha and converting losslessly to RGBA for terrain composition.
    image = Image.new("P", (resolution * supersample,) * 2, 0)
    palette = [0] * (256 * 3)
    palette[3:6] = list(patrol_color[:3])
    palette[6:9] = list(harbor_color[:3])
    image.putpalette(palette)
    transparency = bytearray([255] * 256)
    transparency[0] = 0
    transparency[1] = int(patrol_color[3])
    transparency[2] = int(harbor_color[3])
    image.info["transparency"] = bytes(transparency)
    draw = ImageDraw.Draw(image)
    patrol = document["patrol"]["nodes"]
    points = [_image_point(point, resolution, world_size, supersample) for point in patrol]
    if len(points) > 1:
        draw.line(points + [points[0]], fill=1,
                  width=line_width * supersample, joint="curve")
    for approach in document["harbor_approaches"]:
        points = [
            _image_point(point, resolution, world_size, supersample)
            for point in approach["nodes"]
        ]
        if len(points) > 1:
            draw.line(points, fill=2,
                      width=line_width * supersample, joint="curve")
    return image


def save_cargo_ship_path(world, manifest_path: str | Path, output_dir: str | Path,
                         resolution: int | None = None,
                         patrol_color=(62, 203, 255, 255),
                         harbor_color=(255, 184, 61, 255), line_width: int = 4,
                         smooth_patrol: bool = False,
                         terrain_image: str | Path | Image.Image | None = None,
                         export_layer: bool = True, export_overlay: bool = True,
                         export_json: bool = True) -> dict:
    started = time.perf_counter()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = PrefabManifest.load(manifest_path)
    document, _ = build_cargo_ship_path_export(
        world, manifest, resolution, smooth_patrol=smooth_patrol,
    )
    layer_path = output / "cargo_ship_path.png"
    overlay_path = output / "cargo_ship_path_on_map.png"
    json_path = output / "cargo_ship_path.json"
    document["requested_outputs"] = {
        "layer": export_layer, "overlay": export_overlay, "json": export_json,
    }
    document["style"] = {
        "patrol_color": list(patrol_color), "harbor_color": list(harbor_color),
        "line_width": int(line_width),
        "smooth_patrol": bool(smooth_patrol),
        "smoothing_algorithm": (
            "rust_cargo_update_movement_simulation_rdp_0.5m"
            if smooth_patrol else None
        ),
    }
    image = None
    if export_layer or export_overlay:
        render_started = time.perf_counter()
        image = _draw_paths(document, patrol_color, harbor_color, line_width)
        document["render_seconds"] = time.perf_counter() - render_started
    else:
        document["render_seconds"] = 0.0
    if export_layer and image is not None:
        # Lossless DEFLATE level 1 avoids spending tens of seconds squeezing a
        # few extra KiB from a mostly transparent world-size indexed layer.
        save_png(image, layer_path, compress_level=1)
        document["image_file"] = layer_path.name
    else:
        document["image_file"] = None
        if layer_path.is_file():
            layer_path.unlink()
    document["overlay_file"] = None
    if overlay_path.is_file():
        overlay_path.unlink()
    if export_overlay and image is not None:
        if isinstance(terrain_image, Image.Image):
            base = terrain_image
        else:
            terrain = Path(terrain_image) if terrain_image is not None else None
            if terrain is not None and terrain.is_file():
                with Image.open(terrain) as source:
                    base = source.convert("RGBA")
            else:
                base = None
        if base is None:
            document["warnings"].append("terrain_image_unavailable_overlay_omitted")
        elif base.size != image.size:
            document["warnings"].append("terrain_resolution_mismatch_overlay_omitted")
        else:
            save_png(Image.alpha_composite(base, image.convert("RGBA")), overlay_path)
            document["overlay_file"] = overlay_path.name
    document["elapsed_seconds"] = time.perf_counter() - started
    document["artifact_sizes_bytes"] = {}
    for path in (layer_path, overlay_path):
        if path.is_file():
            document["artifact_sizes_bytes"][path.name] = path.stat().st_size
    if export_json:
        json_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
        document["artifact_sizes_bytes"][json_path.name] = json_path.stat().st_size
    elif json_path.is_file():
        json_path.unlink()
    return document
