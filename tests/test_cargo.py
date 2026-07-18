import json
from importlib import resources

import numpy as np

from rustmap_parser.cargo import (
    _draw_paths, _prefab_sphere_cast_hits, _rdp_indices, _server_style_nodes,
    _terrain_sphere_cast_hits,
    _terrain_collider_clearance, _smooth_patrol_nodes,
    _reconnect_harbor_approaches,
)


def test_packaged_harbor_paths_are_sanitized_and_complete() -> None:
    resource = resources.files("rustmap_parser.data").joinpath("cargo_harbor_paths.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["prefab_count"] == 2
    assert [len(item["nodes"]) for item in payload["prefabs"]] == [11, 11]
    assert all("prefab_path" in item for item in payload["prefabs"])
    assert "F:\\" not in resource.read_text(encoding="utf-8")


def test_packaged_cargo_collision_tiles_are_sanitized() -> None:
    directory = resources.files("rustmap_parser.data.cargo_collision_tiles")
    payload = json.loads(directory.joinpath("tiles.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["template_count"] == 15
    assert payload["sphere_radius_preexpanded_m"] == 3.0
    assert len([item for item in directory.iterdir() if item.name.endswith(".png")]) == 15
    assert all(item["prefab_path"].startswith(
        "assets/bundled/prefabs/autospawn/decor/"
    ) for item in payload["templates"])
    assert any("/iceberg/" in item["prefab_path"] for item in payload["templates"])
    assert any("/coastal_rocks_large/" in item["prefab_path"]
               for item in payload["templates"])
    assert "F:\\" not in directory.joinpath("tiles.json").read_text(encoding="utf-8")


def test_relaxation_uses_float32_and_converges() -> None:
    # Nodes move through collider-free out-of-bounds space, then stop as they
    # encounter the fully blocked square terrain and the 200 m neighbor rule.
    clearance = np.zeros((8, 8), dtype=np.float32)
    nodes, iterations, converged = _server_style_nodes(
        clearance, np.float32(100.0), max_iterations=50
    )
    assert nodes.dtype == np.float32
    assert len(nodes) == 21
    assert 1 < iterations < 50
    assert converged
    radii = np.linalg.norm(nodes[:, (0, 2)], axis=1)
    assert float(radii.min()) < 50.0
    assert float(radii.max()) < 100.0


def test_rdp_preserves_endpoints_and_removes_collinear_nodes() -> None:
    points = np.asarray(((0, 0), (1, 0), (2, 0), (3, 0)), dtype=np.float32)
    assert _rdp_indices(points, 0.01) == [0, 3]


def test_directional_sphere_cast_hits_serialized_terrain() -> None:
    heights = np.full((101, 101), -20.0, dtype=np.float32)
    # World X=20 maps to heightfield column 70. Raise a wide coastal strip to
    # ocean level so the +X sweep must collide while the -X sweep remains clear.
    heights[:, 69:72] = 0.0
    assert _terrain_sphere_cast_hits(
        heights, np.float32(100.0), np.float32(0.0), np.float32(0.0),
        np.float32(1.0), np.float32(0.0), np.float32(40.0), np.float32(3.0),
    )
    assert not _terrain_sphere_cast_hits(
        heights, np.float32(100.0), np.float32(0.0), np.float32(0.0),
        np.float32(-1.0), np.float32(0.0), np.float32(40.0), np.float32(3.0),
    )


def test_directional_sphere_cast_hits_packaged_prefab_grid() -> None:
    obstacles = np.zeros((100, 100), dtype=np.uint8)
    obstacles[50, 70] = 255
    assert _prefab_sphere_cast_hits(
        obstacles, np.float32(-50.0), np.float32(1.0),
        np.float32(0.0), np.float32(0.0),
        np.float32(1.0), np.float32(0.0), np.float32(40.0),
    )
    assert not _prefab_sphere_cast_hits(
        obstacles, np.float32(-50.0), np.float32(1.0),
        np.float32(0.0), np.float32(0.0),
        np.float32(-1.0), np.float32(0.0), np.float32(40.0),
    )


def test_collider_clearance_includes_shallow_submerged_terrain() -> None:
    class World:
        size = 100

    # world_height_grid only needs a height layer in production. Patch the
    # module-level decoder here to isolate the clearance classification rule.
    heights = np.full((101, 101), -20.0, dtype=np.float32)
    heights[:, 50] = -2.5
    from unittest.mock import patch
    with patch("rustmap_parser.cargo.world_height_grid", return_value=heights):
        clearance = _terrain_collider_clearance(World())
    assert clearance[50, 50] <= 0.0
    assert clearance[50, 60] > 0.0


def test_layer_orientation_preserves_x_and_reverses_z() -> None:
    document = {
        "resolution": [100, 100],
        "map": {"world_size": 100},
        "patrol": {"nodes": [
            {"map_position": {"x": 10.0, "y": 10.0}},
            {"map_position": {"x": 90.0, "y": 90.0}},
        ]},
        "harbor_approaches": [],
    }
    image = _draw_paths(document, (1, 2, 3, 255), (4, 5, 6, 255), 1).convert("RGBA")
    assert image.getpixel((10, 90)) == (1, 2, 3, 255)
    assert image.getpixel((90, 10)) == (1, 2, 3, 255)
    assert image.getpixel((10, 10))[3] == 0


def test_exported_patrol_smoothing_preserves_source_array() -> None:
    angles = np.arange(64, dtype=np.float32) * np.float32(2.0 * np.pi / 64)
    noisy_radii = 100.0 + np.where(np.arange(64) % 2 == 0, 20.0, -20.0)
    patrol = np.column_stack((
        np.sin(angles) * noisy_radii,
        np.zeros(64),
        np.cos(angles) * noisy_radii,
    )).astype(np.float32)
    original = patrol.copy()
    smoothed = _smooth_patrol_nodes(patrol, sample_count=64)
    np.testing.assert_array_equal(patrol, original)
    assert len(smoothed) <= len(patrol)
    assert smoothed.dtype == np.float32
    assert np.all(smoothed[:, 1] == 0.0)
    smoothed_radii = np.linalg.norm(smoothed[:, (0, 2)], axis=1)
    assert float(np.ptp(smoothed_radii)) < float(np.ptp(noisy_radii)) * 0.25
    # Alternating 80/120 m teeth collapse to the conservative outer radius,
    # never the inward 80 m radius or their 100 m average.
    assert float(np.min(smoothed_radii)) >= 119.9


def test_harbor_approach_reconnects_to_exported_smooth_node() -> None:
    patrol = np.asarray((
        (0.0, 0.0, 0.0),
        (25.0, 0.0, 0.0),
        (50.0, 0.0, 0.0),
    ), dtype=np.float32)
    approaches = [{
        "approach_patrol_node_index": 99,
        "nodes": [{
            "position": {"x": 48.0, "y": 0.0, "z": 0.0},
            "map_position": {"x": 98.0, "y": 50.0},
        }],
    }]
    _reconnect_harbor_approaches(approaches, patrol, 100)
    assert approaches[0]["approach_patrol_node_index"] == 2
    assert approaches[0]["nodes"][0]["position"]["x"] == 50.0
    assert approaches[0]["nodes"][0]["map_position"]["x"] == 100.0
