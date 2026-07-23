from pathlib import Path

import numpy as np
from PIL import Image

from rustmap_parser import DiagnosticsOptions
from rustmap_parser.layers import generate_diagnostics
from rustmap_parser.parser import MapLayer, Prefab, RustMap, Vector3


def _layer(name: str, values: np.ndarray) -> MapLayer:
    return MapLayer(name, memoryview(values.tobytes()))


def _diagnostic_world() -> RustMap:
    height = np.zeros((2, 2), dtype="<i2")
    alpha = np.asarray(((0, 255), (255, 0)), dtype=np.uint8)
    topology = np.asarray(((1, 0), (0, 1)), dtype="<u4")
    splat = np.zeros((8, 2, 2), dtype=np.uint8)
    splat[0] = 255
    biome = np.zeros((5, 2, 2), dtype=np.uint8)
    biome[1] = 255
    return RustMap(
        1,
        0,
        8,
        [
            _layer("terrain", height),
            _layer("height", height),
            _layer("water", height),
            _layer("alpha", alpha),
            _layer("topology", topology),
            _layer("splat", splat),
            _layer("biome", biome),
        ],
        [Prefab("Monument", 1, Vector3(0.0, -500.0, 0.0))],
        [],
        bytearray(),
    )


def test_diagnostics_none_resolves_to_map_world_size(tmp_path: Path) -> None:
    options = DiagnosticsOptions(resolution=None)
    resolution = options.resolved_resolution(8)
    stats = generate_diagnostics(_diagnostic_world(), tmp_path, resolution)

    assert resolution == 8
    assert stats["output_resolution"] == 8
    assert stats["resolution_mode"] == "uniform"
    pngs = list(tmp_path.glob("*.png"))
    assert pngs
    for path in pngs:
        with Image.open(path) as image:
            assert image.size == (8, 8)

    with Image.open(tmp_path / "topology_00_field.png") as topology:
        assert set(np.unique(np.asarray(topology))) == {0, 255}


def test_diagnostics_without_sizing_preserve_native_layers(tmp_path: Path) -> None:
    stats = generate_diagnostics(_diagnostic_world(), tmp_path)
    assert stats["output_resolution"] is None
    assert stats["resolution_mode"] == "native_layers"
    with Image.open(tmp_path / "height.png") as image:
        assert image.size == (2, 2)
