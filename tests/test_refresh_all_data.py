import unittest

from refresh_all_data import _monument_detail_summary


DIESEL_PREFAB = (
    "assets/content/structures/excavator/prefabs/"
    "diesel_collectable.prefab"
)


def _metadata(diesel_type: str = "diesel_fuel") -> dict:
    return {
        "prefabs": {
            "assets/test/monument.prefab": {
                "bounds": {},
                "interactables": [{}],
                "puzzles": [{}],
                "loot_spawn_groups": [{
                    "variants": [{
                        "prefab_path": DIESEL_PREFAB,
                        "type": diesel_type,
                    }],
                }],
                "radiation_zones": [{}],
            },
        },
    }


class RefreshAllDataTests(unittest.TestCase):
    def test_detailed_monument_summary_covers_every_export_category(self):
        self.assertEqual(_monument_detail_summary(_metadata()), {
            "monument_interactables": 1,
            "monument_puzzles": 1,
            "monument_loot_groups": 1,
            "monument_radiation_zones": 1,
            "monument_diesel_groups": 1,
        })

    def test_diesel_collectible_must_keep_explicit_loot_type(self):
        with self.assertRaisesRegex(RuntimeError, "invalid loot type"):
            _monument_detail_summary(_metadata(diesel_type=None))


if __name__ == "__main__":
    unittest.main()
