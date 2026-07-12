import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from rustmap.renderer import MapRenderResult, _apply_ocean_level, save_map_render


class RendererWaterTests(unittest.TestCase):
    def test_ocean_level_is_applied_only_to_ocean_topology(self):
        water = np.asarray([[-20.0, -20.0, 8.0]], dtype=np.float32)
        topology = np.asarray([[0, 128, 0]], dtype=np.uint32)
        adjusted = _apply_ocean_level(water, topology)
        np.testing.assert_array_equal(adjusted, [[-20.0, 0.0, 8.0]])

    def test_oceanside_topology_also_uses_ocean_level(self):
        water = np.asarray([[-5.0, -5.0]], dtype=np.float32)
        topology = np.asarray([[256, 384]], dtype=np.uint32)
        np.testing.assert_array_equal(_apply_ocean_level(water, topology), [[0.0, 0.0]])

    def test_full_size_render_supersedes_scaled_formats(self):
        world = SimpleNamespace(size=8)
        prepared = SimpleNamespace(prepare_seconds=0.01)
        rendered = MapRenderResult(
            Image.new("RGB", (8, 8), (1, 2, 3)), 8, 8, (1, 2, 3),
            {"total_seconds": 0.1},
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            (output / "map_render.png").write_bytes(b"stale")
            (output / "map_render.jpg").write_bytes(b"stale")
            with (
                patch("rustmap.renderer._prepare_render_inputs", return_value=prepared),
                patch("rustmap.renderer.render_map_image", return_value=rendered) as render,
            ):
                metadata = save_map_render(
                    world, output, scale=0.5, formats=("png", "jpg"),
                    full_size_png=True,
                )

            render.assert_called_once_with(
                world, scale=1.0, ocean_margin=0, prepared=prepared
            )
            self.assertFalse((output / "map_render.png").exists())
            self.assertFalse((output / "map_render.jpg").exists())
            self.assertTrue((output / "map_render_full.png").is_file())
            self.assertEqual(metadata["artifacts"].keys(), {"map_render_full.png"})


if __name__ == "__main__":
    unittest.main()
