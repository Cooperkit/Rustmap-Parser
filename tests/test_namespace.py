import importlib.util
from importlib import resources

import rustmap_parser


def test_new_namespace_exposes_public_api_and_version() -> None:
    assert rustmap_parser.__version__ == "0.2.4"
    for name in ("ExportConfig", "ExportOptions", "RustMapExporter", "load_map"):
        assert hasattr(rustmap_parser, name)


def test_old_namespace_is_not_shipped() -> None:
    assert importlib.util.find_spec("rustmap") is None


def test_new_namespace_owns_packaged_resources() -> None:
    data = resources.files("rustmap_parser.data")
    tiles = resources.files("rustmap_parser.data.tunnel_tiles")
    assert len([item for item in data.iterdir() if item.name.endswith(".json")]) == 4
    assert len([item for item in tiles.iterdir() if item.name.endswith(".png")]) == 81
