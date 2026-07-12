import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from rustmap.config import (
    DataOptions, ExportConfig, ExportOptions, HeatmapOptions, TerrainOptions,
    TileOptions,
)
from rustmap.exporter import _generate, export_orientation


class ExporterTests(unittest.TestCase):
    def test_vertical_only_orientation(self):
        source = np.arange(12, dtype=np.uint8).reshape(3, 4)
        result = export_orientation(source)
        expected = np.array([[8,9,10,11],[4,5,6,7],[0,1,2,3]], dtype=np.uint8)
        np.testing.assert_array_equal(result, expected)
        np.testing.assert_array_equal(result[:,0], source[::-1,0])

    def test_missing_map_is_rejected(self):
        with self.assertRaises(FileNotFoundError):
            ExportConfig(Path("missing.map"), Path("output")).validated()

    def test_invalid_options_are_rejected(self):
        with tempfile.NamedTemporaryFile(suffix=".map") as map_file:
            path = Path(map_file.name)
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    heatmaps=HeatmapOptions(resolution=0))).validated()
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    terrain=TerrainOptions(formats=("gif",)))).validated()
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    terrain=TerrainOptions(ocean_margin=-1))).validated()
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    terrain=TerrainOptions(tiles=TileOptions(0)))).validated()
            with self.assertRaisesRegex(ValueError, "full_size=True"):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    terrain=TerrainOptions(full_size=False, tiles=TileOptions()))).validated()
            with self.assertRaisesRegex(ValueError, "At least one"):
                ExportConfig(path, Path("output"), exports=ExportOptions()).validated()

    def test_presets_select_only_requested_stages(self):
        map_only = ExportOptions.map_only(tiles=True)
        self.assertIsNotNone(map_only.terrain)
        self.assertIsNotNone(map_only.terrain.tiles)
        self.assertIsNone(map_only.heatmaps)
        self.assertFalse(map_only.diagnostics)
        heatmaps = ExportOptions.heatmaps_only(resolution=1024, previews=False)
        self.assertEqual(heatmaps.heatmaps.resolution, 1024)
        self.assertFalse(heatmaps.heatmaps.previews)
        self.assertIsNone(heatmaps.terrain)

    def test_single_terrain_format_string_is_normalized_as_one_format(self):
        with tempfile.NamedTemporaryFile(suffix=".map") as map_file:
            config = ExportConfig(
                Path(map_file.name), Path("output"),
                exports=ExportOptions(terrain=TerrainOptions(formats="PNG")),
            ).validated()
        self.assertEqual(config.exports.terrain.formats, ("png",))

    def test_none_heatmap_resolution_uses_world_size(self):
        options = HeatmapOptions(resolution=None)
        self.assertEqual(options.resolved_resolution(4250), 4250)
        with tempfile.NamedTemporaryFile(suffix=".map") as map_file:
            config = ExportConfig(
                Path(map_file.name), Path("output"),
                exports=ExportOptions(heatmaps=options),
            ).validated()
        self.assertIsNone(config.exports.heatmaps.resolution)

    def test_unused_data_override_is_not_required(self):
        with tempfile.NamedTemporaryFile(suffix=".map") as map_file:
            config = ExportConfig(
                Path(map_file.name), Path("output"),
                exports=ExportOptions.map_only(),
                data=DataOptions(spawn_rules_path=Path("not-needed.json")),
            )
            config.validated()
            with self.assertRaises(FileNotFoundError):
                ExportConfig(
                    Path(map_file.name), Path("output"),
                    exports=ExportOptions.heatmaps_only(),
                    data=DataOptions(spawn_rules_path=Path("required.json")),
                ).validated()

    def test_map_selection_does_not_call_disabled_stages(self):
        world = SimpleNamespace(size=100, serialization_version=1, timestamp=2)
        with tempfile.TemporaryDirectory() as temporary:
            config = ExportConfig(
                Path("unused.map"), Path(temporary),
                exports=ExportOptions(terrain=TerrainOptions(
                    formats=("png",), full_size=False,
                )),
            )
            render_metadata = {"artifacts": {}, "full_size_tiles": None}
            with (
                patch("rustmap.exporter.load_map", return_value=world),
                patch("rustmap.exporter.save_map_render", return_value=render_metadata) as render,
                patch("rustmap.exporter.generate_diagnostics") as diagnostics,
                patch("rustmap.exporter.save_monuments") as monuments,
                patch("rustmap.exporter.save_tunnel_render") as tunnels,
                patch("rustmap.exporter.save_no_build_zones") as no_build,
            ):
                metadata = _generate(config, None, None)
            render.assert_called_once()
            diagnostics.assert_not_called()
            monuments.assert_not_called()
            tunnels.assert_not_called()
            no_build.assert_not_called()
            self.assertEqual(metadata["enabled_outputs"], {
                "heatmaps": False, "diagnostics": False, "monuments": False,
                "terrain": True, "tunnels": False, "no_build_zones": False,
            })


if __name__ == "__main__":
    unittest.main()
