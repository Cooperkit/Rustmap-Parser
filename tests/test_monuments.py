import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rustmap_parser.monuments import (
    _gameplay_database,
    build_monument_export,
    monument_metadata,
)
from rustmap_parser.prefabs import PrefabManifest, PrefabManifestEntry


PREFIX = "assets/bundled/prefabs/autospawn/monument/"


class MonumentMetadataTests(unittest.TestCase):
    def test_safe_zone_metadata(self):
        value = monument_metadata(PREFIX + "medium/compound.prefab")
        self.assertEqual(value["display_name"], "Outpost")
        self.assertEqual(value["classification"]["kind"], "safe_zone")
        self.assertEqual(value["classification"]["size_class"], "medium")
        self.assertTrue(value["gameplay"]["safe_zone"])
        self.assertEqual(value["gameplay"]["recycler_count"], 3)
        self.assertEqual(value["gameplay"]["keycard_requirements"], [])
        self.assertEqual(value["gameplay"]["puzzle_type"], "none")

    def test_extracted_card_and_loot_metadata(self):
        value = monument_metadata(PREFIX + "xlarge/launch_site_1.prefab")
        self.assertEqual(value["gameplay"]["recycler_count"], 1)
        self.assertEqual(value["gameplay"]["keycard_requirements"], ["green", "red"])
        self.assertEqual(value["gameplay"]["loot_tier"], 3)
        self.assertEqual(value["gameplay"]["puzzle_type"], "keycard_and_electrical")

    def test_cave_family_and_environment(self):
        value = monument_metadata(PREFIX + "cave/cave_large_sewers_hard.prefab")
        self.assertEqual(value["family"], "cave_large_sewers")
        self.assertEqual(value["classification"]["kind"], "cave")
        self.assertEqual(value["classification"]["environment"], "underground")
        self.assertFalse(value["gameplay"]["safe_zone"])

    def test_offshore_display_name(self):
        value = monument_metadata(PREFIX + "offshore/oilrig_2.prefab")
        self.assertEqual(value["display_name"], "Large Oil Rig")
        self.assertEqual(value["classification"]["environment"], "offshore")
        self.assertEqual(value["classification"]["size_class"], "large")

    def test_nonstandard_groups_have_size_classes(self):
        cases = {
            "arctic_bases/arctic_research_base_a": "large",
            "cave/cave_small_hard": "small",
            "fishing_village/fishing_village_a": "small",
            "underwater_lab/underwater_lab_a": "large",
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                value = monument_metadata(PREFIX + path + ".prefab")
                self.assertEqual(value["classification"]["size_class"], expected)

    def test_train_tunnel_entrance_and_link_metadata(self):
        entrance = monument_metadata(
            "assets/bundled/prefabs/autospawn/tunnel-entrance/entrance_bunker_a.prefab"
        )
        self.assertEqual(entrance["classification"]["kind"], "train_tunnel_entrance")
        self.assertEqual(entrance["family"], "train_tunnel_entrance")
        link = monument_metadata(
            "assets/bundled/prefabs/autospawn/tunnel-upwards/intersection-b1-e.prefab"
        )
        self.assertEqual(link["classification"]["kind"], "train_tunnel_link")
        self.assertEqual(link["family"], "train_tunnel_link")

    def test_packaged_database_contains_visible_child_landmarks(self):
        prefabs = _gameplay_database()["prefabs"]
        link_path = (
            "assets/bundled/prefabs/autospawn/tunnel-upwards/"
            "intersection-b4-e.prefab"
        )
        link = [item for item in prefabs[link_path]["landmarks"]
                if item["should_display_on_map"]]
        self.assertEqual(len(link), 1)
        self.assertEqual(link[0]["display_token"], "train_tunnel_link_display_name")
        self.assertAlmostEqual(link[0]["local_matrix"][0][3], 432.000058, places=5)
        self.assertAlmostEqual(link[0]["local_matrix"][1][3], 90.0, places=5)

        airfield_path = PREFIX + "large/airfield_1.prefab"
        entrances = [item for item in prefabs[airfield_path]["landmarks"]
                     if item["component_type"] == "DungeonGridInfo" and
                     item["should_display_on_map"]]
        self.assertEqual(len(entrances), 1)
        self.assertEqual(entrances[0]["display_token"], "train_tunnel_display_name")

    def test_export_includes_entrances_and_links_but_not_track_pieces(self):
        paths = {
            1: PREFIX + "large/airfield_1.prefab",
            2: "assets/bundled/prefabs/autospawn/tunnel-entrance/entrance_bunker_a.prefab",
            3: "assets/bundled/prefabs/autospawn/tunnel-upwards/intersection-b1-e.prefab",
            4: "assets/content/structures/train_tunnels/train_tunnel_double_str_a_36m.prefab",
        }
        manifest = PrefabManifest(
            entries={key: PrefabManifestEntry(key, path) for key, path in paths.items()},
            collisions={}, source_bundle="test", source_size=0, source_mtime_ns=0,
        )
        prefabs = [
            SimpleNamespace(
                prefab_id=key,
                position=SimpleNamespace(x=float(key), y=0.0, z=0.0),
                rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                scale=None,
                category="Monument",
            )
            for key in paths
        ]
        world = SimpleNamespace(
            size=4250, serialization_version=1, timestamp=2, prefabs=prefabs,
        )
        with patch("rustmap_parser.monuments._visible_landmarks",
                   return_value=([None], True)):
            payload = build_monument_export(world, manifest)
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(payload["monument_count"], 3)
        exported_paths = {item["prefab_path"] for item in payload["monuments"]}
        self.assertIn(paths[2], exported_paths)
        self.assertIn(paths[3], exported_paths)
        self.assertNotIn(paths[4], exported_paths)

    def test_child_landmark_transform_corrects_link_and_adds_monument_entrance(self):
        airfield = PREFIX + "large/airfield_1.prefab"
        link = "assets/bundled/prefabs/autospawn/tunnel-upwards/intersection-b4-e.prefab"
        manifest = PrefabManifest(
            entries={
                1: PrefabManifestEntry(1, airfield),
                2: PrefabManifestEntry(2, link),
            }, collisions={}, source_bundle="test", source_size=0, source_mtime_ns=0,
        )
        prefabs = [
            SimpleNamespace(
                prefab_id=1, position=SimpleNamespace(x=100.0, y=10.0, z=200.0),
                rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0), scale=None,
                category="Monument",
            ),
            SimpleNamespace(
                prefab_id=2, position=SimpleNamespace(x=-648.0, y=-48.0, z=-432.0),
                rotation=None, scale=None, category="Dungeon",
            ),
        ]
        world = SimpleNamespace(
            size=4250, serialization_version=1, timestamp=2, prefabs=prefabs,
        )

        def landmark(component, token, x, y, z):
            matrix = [[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, y],
                      [0.0, 0.0, 1.0, z], [0.0, 0.0, 0.0, 1.0]]
            return {
                "component_type": component, "object_name": token,
                "display_token": token, "should_display_on_map": True,
                "local_matrix": matrix,
            }

        def visible(path):
            if path == airfield:
                return ([
                    landmark("MonumentInfo", "airfield", 0.0, 0.0, 0.0),
                    landmark("DungeonGridInfo", "train_tunnel_display_name",
                             -156.64, 0.564, -92.49),
                ], False)
            return ([landmark("MonumentInfo", "train_tunnel_link_display_name",
                              432.0, 90.0, 0.0)], False)

        with patch("rustmap_parser.monuments._visible_landmarks", side_effect=visible):
            payload = build_monument_export(world, manifest)
        self.assertEqual(payload["monument_count"], 3)
        link_item = next(item for item in payload["monuments"]
                         if item["metadata"]["classification"]["kind"] == "train_tunnel_link")
        self.assertEqual(link_item["position"], {"x": -216.0, "y": 42.0, "z": -432.0})
        self.assertEqual(link_item["map_position"], {"x": 1909.0, "y": 1693.0})
        entrances = [item for item in payload["monuments"]
                     if item["metadata"]["classification"]["kind"] == "train_tunnel_entrance"]
        self.assertEqual(len(entrances), 1)
        self.assertEqual(entrances[0]["position"], {
            "x": -56.64, "y": 10.564, "z": 107.51,
        })


if __name__ == "__main__":
    unittest.main()
