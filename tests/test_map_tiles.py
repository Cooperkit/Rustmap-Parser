import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from rustmap_parser.renderer import save_full_map_tiles
from rustmap_parser.png import PNG_SOURCE_KEY, PROJECT_URL


class MapTileTests(unittest.TestCase):
    def test_bottom_left_tiles_reconstruct_exact_source(self):
        source = np.zeros((5, 7, 3), dtype=np.uint8)
        for image_y in range(5):
            for x in range(7):
                source[image_y, x] = (x * 20, image_y * 30, 100)
        image = Image.fromarray(source, "RGB")

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            metadata = save_full_map_tiles(image, output, tile_size=4)
            self.assertEqual((metadata["columns"], metadata["rows"]), (2, 2))
            self.assertEqual(metadata["tile_count"], 4)

            reconstructed = Image.new("RGBA", image.size, (0, 0, 0, 0))
            for item in metadata["tiles"]:
                with Image.open(output / item["filename"]) as opened:
                    self.assertEqual(opened.info[PNG_SOURCE_KEY], PROJECT_URL)
                    tile = opened.copy()
                self.assertEqual(tile.mode, "RGBA")
                self.assertEqual(tile.size, (4, 4))
                width = item["content_width"]
                height = item["content_height"]
                offset_y = item["image_content_offset"]["y"]
                content = tile.crop((0, offset_y, width, offset_y + height))
                bounds = item["map_bounds"]
                paste_y = image.height - bounds["top"]
                reconstructed.paste(content, (bounds["left"], paste_y))

                alpha = np.asarray(tile.getchannel("A"))
                self.assertTrue(np.all(alpha[offset_y:offset_y + height, :width] == 255))
                outside = alpha.copy()
                outside[offset_y:offset_y + height, :width] = 0
                self.assertEqual(int(outside.max()), 0)

            np.testing.assert_array_equal(np.asarray(reconstructed.convert("RGB")), source)
            top_left = next(item for item in metadata["tiles"]
                            if item["x"] == 0 and item["y"] == 1)
            self.assertEqual(top_left["content_height"], 1)
            self.assertEqual(top_left["image_content_offset"]["y"], 3)

    def test_exact_multiple_small_image_and_deterministic_bytes(self):
        image = Image.fromarray(np.arange(48, dtype=np.uint8).reshape(4, 4, 3), "RGB")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            first = save_full_map_tiles(image, output, tile_size=4)
            self.assertEqual(first["tile_count"], 1)
            stale = output / "x_99_y_99.png"
            image.save(stale, "PNG")
            first_hash = hashlib.sha256((output / "x_0_y_0.png").read_bytes()).digest()
            second = save_full_map_tiles(image, output, tile_size=4)
            second_hash = hashlib.sha256((output / "x_0_y_0.png").read_bytes()).digest()
            self.assertEqual(first_hash, second_hash)
            self.assertFalse(stale.exists())
            persisted = json.loads((output / "tiles.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["tile_count"], second["tile_count"])
            self.assertEqual(persisted["directory_size_bytes"], second["directory_size_bytes"])

    def test_smaller_than_tile_is_transparent_padded(self):
        image = Image.new("RGB", (2, 3), (10, 20, 30))
        with tempfile.TemporaryDirectory() as temporary:
            metadata = save_full_map_tiles(image, temporary, tile_size=4)
            item = metadata["tiles"][0]
            self.assertEqual((item["content_width"], item["content_height"]), (2, 3))
            with Image.open(Path(temporary) / item["filename"]) as tile:
                alpha = np.asarray(tile.getchannel("A"))
            np.testing.assert_array_equal(alpha[:, 2:], 0)
            np.testing.assert_array_equal(alpha[0, :], 0)


if __name__ == "__main__":
    unittest.main()
