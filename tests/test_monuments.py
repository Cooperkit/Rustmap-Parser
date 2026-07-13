import unittest
from types import SimpleNamespace

from rustmap_parser.monuments import build_monument_export, monument_metadata
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
                category="Monument",
            )
            for key in paths
        ]
        world = SimpleNamespace(
            size=4250, serialization_version=1, timestamp=2, prefabs=prefabs,
        )
        payload = build_monument_export(world, manifest)
        self.assertEqual(payload["schema_version"], 4)
        self.assertEqual(payload["monument_count"], 3)
        exported_paths = {item["prefab_path"] for item in payload["monuments"]}
        self.assertIn(paths[2], exported_paths)
        self.assertIn(paths[3], exported_paths)
        self.assertNotIn(paths[4], exported_paths)


if __name__ == "__main__":
    unittest.main()
