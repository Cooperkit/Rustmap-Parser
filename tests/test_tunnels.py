import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path

import numpy as np
from PIL import Image

from rustmap_parser.config import ExportConfig, ExportOptions, TunnelOptions
from rustmap_parser.parser import Prefab, RustMap, Vector3
from rustmap_parser.prefabs import PrefabManifest, PrefabManifestEntry
from rustmap_parser.tunnels import _euler_matrix, _instance_matrix, render_tunnel_map, save_tunnel_render
from rustmap_parser.tunnel_assets import _cache_key, _rasterize_template


class TunnelRendererTests(unittest.TestCase):
    def test_y_rotation_and_instance_translation(self):
        prefab = Prefab(position=Vector3(10, 2, 20), rotation=Vector3(0, 90, 0),
                        scale=Vector3(2, 1, 1))
        point = _instance_matrix(prefab) @ np.array([1, 0, 0, 1], dtype=float)
        np.testing.assert_allclose(point, [10, 2, 18, 1], atol=1e-6)
        np.testing.assert_allclose(_euler_matrix(Vector3()), np.eye(3), atol=1e-7)

    def test_mesh_render_is_transparent_and_flips_z_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            path = "assets/bundled/prefabs/autospawn/tunnel/test.prefab"
            vertices = np.array([[-4,0,-4],[4,0,-4],[4,0,4],[-4,0,4]], dtype=np.float32)
            triangles = np.array([[0,1,2],[0,2,3]], dtype=np.int32)
            template_mask, mask_metadata = _rasterize_template(vertices, triangles)
            (cache / "templates").mkdir()
            Image.fromarray(template_mask).save(cache / "templates" / "template.png")
            np.savez_compressed(cache / "geometry.npz",
                                template_000_vertices=vertices,
                                template_000_triangles=triangles)
            (cache / "metadata.json").write_text(json.dumps({
                "schema_version": 2, "template_count": 1,
                "template_pixels_per_meter": mask_metadata["mask_pixels_per_meter"],
                "templates": [{"key":"template_000", "prefab_path":path,
                               "mask_file":"templates/template.png",
                               **mask_metadata}],
            }), encoding="utf-8")
            manifest = PrefabManifest(
                entries={1: PrefabManifestEntry(1, path)}, collisions={},
                source_bundle="content.bundle", source_size=1, source_mtime_ns=1,
            )
            world = RustMap(1, 0, 100, [], [
                Prefab("Dungeon", 1, Vector3(-20,0,20), Vector3(), Vector3(1,1,1)),
            ], [], bytearray())
            image, metadata = render_tunnel_map(world, manifest, cache, resolution=100)
            alpha = np.asarray(image.getchannel("A"))
            ys, xs = np.nonzero(alpha)
            self.assertLess(xs.mean(), 50)  # X remains left of center.
            self.assertLess(ys.mean(), 50)  # Positive Z becomes upper image rows.
            self.assertEqual(alpha[99,99], 0)
            self.assertGreater(metadata["alpha_nonzero_pixels"], 0)
            self.assertEqual(metadata["fast_path_instance_count"], 1)
            self.assertEqual(metadata["tile_source"], "override_cache")
            reference, reference_metadata = render_tunnel_map(
                world, manifest, cache, resolution=100, force_triangles=True
            )
            cached_alpha = np.asarray(image.getchannel("A"), dtype=np.int16)
            reference_alpha = np.asarray(reference.getchannel("A"), dtype=np.int16)
            self.assertLess(float(np.abs(cached_alpha-reference_alpha).mean()), 1.0)
            intersection = np.count_nonzero((cached_alpha>0) & (reference_alpha>0))
            union = np.count_nonzero((cached_alpha>0) | (reference_alpha>0))
            self.assertGreater(intersection/union, 0.98)
            self.assertEqual(reference_metadata["fallback_instance_count"], 1)

            world.prefabs = [
                Prefab("Dungeon", 1, Vector3(0,0,0), Vector3(0,angle,0), Vector3(1,1,1))
                for angle in (0,90,180,270)
            ]
            _, cardinal = render_tunnel_map(world, manifest, cache, resolution=100)
            self.assertEqual(cardinal["fast_path_instance_count"], 4)
            custom, custom_metadata = render_tunnel_map(world, manifest, cache, resolution=50)
            self.assertEqual(custom.size, (50, 50))
            self.assertEqual(custom_metadata["train_layer_instance_count"], 4)

            world.prefabs = [
                Prefab("Dungeon", 1, Vector3(.1,0,0), Vector3(), Vector3(1,1,1)),
                Prefab("Dungeon", 1, Vector3(0,0,0), Vector3(0,45,0), Vector3(1,1,1)),
                Prefab("Dungeon", 1, Vector3(0,0,0), Vector3(), Vector3(2,1,1)),
            ]
            _, fallback = render_tunnel_map(world, manifest, cache, resolution=100)
            self.assertEqual(fallback["fallback_instance_count"], 3)

    def test_tunnel_config_validation(self):
        with tempfile.NamedTemporaryFile(suffix=".map") as map_file:
            path = Path(map_file.name)
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    tunnels=TunnelOptions(resolution=0))).validated()
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    tunnels=TunnelOptions(overlay_opacity=1.1))).validated()
            with self.assertRaisesRegex(ValueError, "export_layer or export_overlay"):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    tunnels=TunnelOptions(export_layer=False, export_overlay=False))).validated()
            with self.assertRaisesRegex(ValueError, "full-size terrain"):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    tunnels=TunnelOptions(export_layer=False, export_overlay=True))).validated()

    def test_overlay_only_omits_transparent_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = PrefabManifest({}, {}, "content.bundle", 1, 1)
            manifest_path = root / "manifest.json"
            manifest.save(manifest_path)
            world = RustMap(1, 0, 100, [], [], [], bytearray())
            terrain = Image.new("RGBA", (100, 100), (30, 40, 50, 255))
            metadata = save_tunnel_render(
                world, manifest_path, root, terrain_image=terrain,
                export_layer=False, export_overlay=True,
            )
            self.assertFalse((root / "tunnels.png").exists())
            self.assertTrue((root / "tunnels_on_map.png").is_file())
            self.assertEqual(metadata["requested_outputs"], {"layer": False, "overlay": True})
            self.assertEqual(metadata["terrain_tint_rgba"], [50, 45, 105, 104])
            with Image.open(root / "tunnels_on_map.png") as overlay:
                expected = Image.alpha_composite(
                    terrain, Image.new("RGBA", terrain.size, (50, 45, 105, 104))
                )
                self.assertEqual(overlay.getpixel((0, 0)), expected.getpixel((0, 0)))

    def test_packaged_tiles_render_without_rust_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = PrefabManifest({}, {}, "content.bundle", 1, 1)
            manifest_path = root / "manifest.json"
            manifest.save(manifest_path)
            world = RustMap(1, 0, 100, [], [], [], bytearray())
            metadata = save_tunnel_render(
                world, manifest_path, root / "output",
                rust_install_path=root / "definitely-missing",
            )
            self.assertEqual(metadata["status"], "rendered")
            self.assertEqual(metadata["tile_source"], "packaged")
            self.assertTrue(metadata["tile_version_mismatch"])
            self.assertIn("tile_version_mismatch", metadata["tile_warnings"])
            self.assertTrue((root / "output" / "tunnels.png").exists())
            self.assertTrue((root / "output" / "tunnels_metadata.json").is_file())

    def test_bundle_change_invalidates_cache_key(self):
        identity = {"schema_version": 1, "rust_build_id": "1", "bundles": {
            "content": {"size": 10, "mtime_ns": 20},
            "asset_scenes": {"size": 30, "mtime_ns": 40},
            "maps": {"size": 50, "mtime_ns": 60},
        }}
        first = _cache_key(identity)
        identity["bundles"]["content"]["size"] += 1
        self.assertNotEqual(first, _cache_key(identity))

    def test_packaged_tile_set_is_sanitized_and_complete(self):
        root = resources.files("rustmap_parser.data.tunnel_tiles")
        payload = json.loads(root.joinpath("tiles.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["template_count"], 81)
        self.assertEqual(len([item for item in root.iterdir() if item.name.endswith(".png")]), 81)
        serialized = json.dumps(payload)
        self.assertNotIn("SteamLibrary", serialized)
        self.assertNotIn("geometry.npz", serialized)


if __name__ == "__main__":
    unittest.main()
