import importlib.util
from importlib import resources

import rustmap_parser


def test_new_namespace_exposes_public_api_and_version() -> None:
    assert rustmap_parser.__version__ == "0.3.1"
    for name in (
        "CargoShipPathOptions", "ExportConfig", "ExportOptions", "RustMapExporter",
        "generate_cargo_patrol_path", "refresh_cargo_harbor_paths",
        "refresh_cargo_collision_tiles", "load_map",
    ):
        assert hasattr(rustmap_parser, name)


def test_old_namespace_is_not_shipped() -> None:
    assert importlib.util.find_spec("rustmap") is None


def test_new_namespace_owns_packaged_resources() -> None:
    data = resources.files("rustmap_parser.data")
    tiles = resources.files("rustmap_parser.data.tunnel_tiles")
    cargo_tiles = resources.files("rustmap_parser.data.cargo_collision_tiles")
    assert len([item for item in data.iterdir() if item.name.endswith(".json")]) == 5
    assert len([item for item in tiles.iterdir() if item.name.endswith(".png")]) == 81
    assert len([item for item in cargo_tiles.iterdir() if item.name.endswith(".png")]) == 15
