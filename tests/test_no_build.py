import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from rustmap.config import ExportConfig, ExportOptions, NoBuildZoneOptions
from rustmap.no_build import _draw_zones, build_no_build_export, save_no_build_zones
from rustmap.parser import Prefab, RustMap, Vector3
from rustmap.prefabs import PrefabManifest, PrefabManifestEntry
from rustmap.no_build_assets import _include_surface_blocker, _primitive_contains, _remove_contained


PATH = "assets/bundled/prefabs/autospawn/monument/test/test.prefab"


def fixture_data():
    return {
        "source": {"content_bundle_size": 10, "content_bundle_mtime_ns": 20},
        "excluded_definition_counts": {"unsupported_shape": 0, "below_minimum_area": 0,
                                       "nonrepresentable": 0, "deployable_only": 0},
        "prefabs": [{"prefab_path": PATH, "zones": [{
            "shape": "rectangle", "source": "block_placement",
            "local_matrix": [[1,0,0,10],[0,1,0,0],[0,0,1,0],[0,0,0,1]],
            "local_transform": {
                "position":{"x":10,"y":0,"z":0},
                "rotation_euler":{"x":0,"y":0,"z":0},
                "scale":{"x":1,"y":1,"z":1},
            },
            "center": {"x":0,"y":0,"z":0},
            "size": {"x":40,"y":2,"z":30},
            "local_y_bounds": [-1,1],
            "projected_area_m2": 1200,
        }]}],
    }


class NoBuildZoneTests(unittest.TestCase):
    def test_surface_family_and_connector_tag_filter(self):
        self.assertTrue(_include_surface_blocker("monument",False))
        self.assertTrue(_include_surface_blocker("power substations",True))
        self.assertTrue(_include_surface_blocker("tunnel-entrance",False))
        self.assertTrue(_include_surface_blocker("tunnel-upwards",True))
        self.assertFalse(_include_surface_blocker("tunnel-upwards",False))
        self.assertFalse(_include_surface_blocker("tunnel-link",True))

    @staticmethod
    def circle(x=0.0, z=0.0, radius=1.0):
        return {"shape":"circle", "center":[x,z], "radius":radius}

    @staticmethod
    def rectangle(x=0.0, z=0.0, half_width=1.0, half_height=1.0,
                  axis_x=(1.0,0.0), axis_z=(0.0,1.0)):
        return {"shape":"rectangle", "center":[x,z], "half_width":half_width,
                "half_height":half_height, "axis_x":list(axis_x), "axis_z":list(axis_z)}

    def test_all_primitive_containment_combinations(self):
        circle=self.circle(0,0,10)
        rectangle=self.rectangle(0,0,5,5)
        self.assertTrue(_primitive_contains(circle,self.circle(3,0,2)))
        self.assertFalse(_primitive_contains(circle,self.circle(9,0,2)))
        self.assertTrue(_primitive_contains(circle,self.rectangle(0,0,2,2)))
        self.assertFalse(_primitive_contains(circle,self.rectangle(9,0,2,2)))
        self.assertTrue(_primitive_contains(rectangle,self.circle(0,0,2)))
        self.assertFalse(_primitive_contains(rectangle,self.circle(4,0,2)))
        self.assertTrue(_primitive_contains(rectangle,self.rectangle(0,0,2,2)))
        self.assertFalse(_primitive_contains(rectangle,self.rectangle(5,0,2,2)))

    def test_rotated_rectangles_touching_and_small_isolated_zones(self):
        root=2**-0.5
        outer=self.rectangle(0,0,10,5,(root,root),(-root,root))
        inner=self.rectangle(0,0,2,1,(root,root),(-root,root))
        touching=self.rectangle(8,0,2,1,(1,0),(0,1))
        self.assertTrue(_primitive_contains(outer,inner))
        self.assertFalse(_primitive_contains(outer,touching))

        def zone(name,area,primitive):
            return {"object_name":name,"test_id":name,"shape":primitive["shape"],
                    "projected_area_m2":area,"local_matrix":[[1,0,0,0],[0,1,0,0],
                    [0,0,1,0],[0,0,0,1]],"_analysis":primitive}
        retained,removed=_remove_contained([
            zone("outer",1000,self.circle(0,0,20)),
            zone("contained_large",950,self.circle(0,0,15)),
            zone("isolated_small",36,self.rectangle(100,0,3,3)),
        ])
        self.assertEqual(removed,1)
        self.assertEqual({item["test_id"] for item in retained},{"outer","isolated_small"})

    def test_transform_and_vertical_only_orientation(self):
        manifest = PrefabManifest(
            {1: PrefabManifestEntry(1, PATH)}, {}, "content.bundle", 10, 20
        )
        world = RustMap(1, 2, 100, [], [
            Prefab("Monument", 1, Vector3(-20,5,20), Vector3(0,90,0), Vector3(1,1,1))
        ], [], bytearray())
        document, zones = build_no_build_export(world, manifest, fixture_data(), 100)
        self.assertEqual(document["zone_count"], 1)
        self.assertFalse(document["data_version_mismatch"])
        zone = zones[0]
        self.assertLess(zone["transform"]["position"]["x"], 0)
        self.assertLess(zone["transform"]["image_position"]["x"], 0.5)
        self.assertLess(zone["transform"]["image_position"]["y"], 0.5)
        self.assertEqual(zone["shape"], "rectangle")
        self.assertNotIn("footprint", zone)
        image = _draw_zones(zones, 100, (255,0,0,64), (255,0,0,255), 2)
        self.assertEqual(image.size, (100,100))
        self.assertIsNotNone(image.getbbox())

    def test_missing_definition_is_reported_without_approximation(self):
        manifest = PrefabManifest(
            {1: PrefabManifestEntry(1, PATH)}, {}, "content.bundle", 10, 20
        )
        world = RustMap(1, 2, 100, [], [Prefab("Monument", 1, Vector3())], [], bytearray())
        document, zones = build_no_build_export(world, manifest, {"source":{}, "prefabs":[]})
        self.assertEqual(zones, [])
        self.assertEqual(document["skipped_owner_count"], 1)
        self.assertIn("owners_skipped_without_blocker_geometry", document["warnings"])

    def test_config_validation(self):
        with tempfile.NamedTemporaryFile(suffix=".map") as map_file:
            path = Path(map_file.name)
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    no_build_zones=NoBuildZoneOptions(resolution=0))).validated()
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    no_build_zones=NoBuildZoneOptions(fill_color=(300,0,0,0)))).validated()
            with self.assertRaises(ValueError):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    no_build_zones=NoBuildZoneOptions(outline_width=-1))).validated()
            with self.assertRaisesRegex(ValueError, "export_images or export_json"):
                ExportConfig(path, Path("output"), exports=ExportOptions(
                    no_build_zones=NoBuildZoneOptions(
                        export_images=False, export_json=False))).validated()

    def test_json_only_skips_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = PrefabManifest({}, {}, "content.bundle", 1, 1)
            manifest_path = root / "manifest.json"
            manifest.save(manifest_path)
            world = RustMap(1, 0, 100, [], [], [], bytearray())
            metadata = save_no_build_zones(
                world, manifest_path, root,
                export_images=False, export_json=True,
            )
            self.assertTrue((root / "no_build_zones.json").is_file())
            self.assertFalse((root / "no_build_zones.png").exists())
            self.assertFalse((root / "no_build_zones_on_map.png").exists())
            self.assertEqual(metadata["requested_outputs"], {"images": False, "json": True})

    def test_packaged_data_is_sanitized(self):
        payload = json.loads(resources.files("rustmap.data").joinpath(
            "no_build_zones.json").read_text(encoding="utf-8"))
        self.assertGreater(payload["prefab_count"], 0)
        self.assertGreater(payload["zone_definition_count"], 0)
        self.assertEqual(payload["schema_version"], 4)
        self.assertLess(len(json.dumps(payload).encode()), 250 * 1024)
        self.assertEqual(
            {zone["shape"] for prefab in payload["prefabs"] for zone in prefab["zones"]},
            {"circle", "rectangle"},
        )
        serialized = json.dumps(payload)
        self.assertNotIn("SteamLibrary", serialized)
        self.assertNotIn("geometry.npz", serialized)
        self.assertNotIn("footprint_rings", serialized)


if __name__ == "__main__":
    unittest.main()
