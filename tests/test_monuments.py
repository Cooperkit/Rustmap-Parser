import unittest

from rustmap_parser.monuments import monument_metadata


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


if __name__ == "__main__":
    unittest.main()
