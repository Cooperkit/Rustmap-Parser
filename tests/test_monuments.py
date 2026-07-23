import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rustmap_parser.config import TransformOptions
from rustmap_parser.monuments import (
    _gameplay_database,
    _build_monument_documents,
    _major_puzzle_routes,
    _map_loot_type,
    build_monument_export,
    monument_metadata,
)
from rustmap_parser.prefabs import PrefabManifest, PrefabManifestEntry


PREFIX = "assets/bundled/prefabs/autospawn/monument/"


class MonumentMetadataTests(unittest.TestCase):
    def test_direct_loot_detection_avoids_spawner_prefab_false_counts(self):
        self.assertEqual(_map_loot_type(
            "assets/bundled/prefabs/radtown/oil_barrel.prefab"
        ), "barrel")
        self.assertEqual(_map_loot_type(
            "assets/prefabs/deployable/chinooklockedcrate/"
            "codelockedhackablecrate.prefab"
        ), "crate")
        self.assertIsNone(_map_loot_type(
            "assets/bundled/prefabs/radtown/underwater_labs/spawners/"
            "spawner_elite_crate.prefab"
        ))
        self.assertEqual(_map_loot_type(
            "assets/content/structures/excavator/prefabs/"
            "diesel_collectable.prefab"
        ), "diesel_fuel")

    def test_major_routes_merge_reset_graphs_and_drop_exit_controls(self):
        def matrix(x):
            return [[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]

        def node(node_id, component, name, x, depth, properties=None):
            return {
                "id": node_id, "component_type": component,
                "object_name": name, "local_matrix": matrix(x),
                "depth": depth, "properties": properties or {},
            }

        card = node("card", "CardReader", "RCD", 10, 2, {"keycard": "red"})
        door = node("door", "DoorManipulator", "RCD", 11, 3)
        alternate_card = node(
            "card-alt", "CardReader", "RCD_Alt", 20, 2, {"keycard": "red"}
        )
        alternate_door = node(
            "door-alt", "DoorManipulator", "RCD_Alt", 21, 3
        )
        graphs = [{
            "nodes": [
                node("fuse-a", "ItemBasedFlowRestrictor", "FuseBox_A", 1, 0),
                node("switch-a", "ElectricSwitch", "SimpleSwitch_A", 2, 1),
                card, door, alternate_card, alternate_door,
            ],
            "edges": [
                {"from": "fuse-a", "to": "switch-a"},
                {"from": "switch-a", "to": "card"},
                {"from": "card", "to": "door"},
                {"from": "switch-a", "to": "card-alt"},
                {"from": "card-alt", "to": "door-alt"},
            ],
        }, {
            "nodes": [
                node("fuse-b", "ItemBasedFlowRestrictor", "FuseBox_B", 3, 0),
                node("switch-b", "ElectricSwitch", "SimpleSwitch_B", 4, 1),
                card, door, alternate_card, alternate_door,
                node("exit", "PressButton", "ExitButton", 12, 0),
                node("light", "ElectricSwitch", "LightSwitch_Hall", 13, 0),
            ],
            "edges": [
                {"from": "fuse-b", "to": "switch-b"},
                {"from": "switch-b", "to": "card"},
                {"from": "card", "to": "door"},
                {"from": "switch-b", "to": "card-alt"},
                {"from": "card-alt", "to": "door-alt"},
                {"from": "exit", "to": "door"},
                {"from": "light", "to": "door"},
            ],
        }]

        routes = _major_puzzle_routes(graphs)

        self.assertEqual(len(routes), 1)
        self.assertEqual(
            [step["action"] for step in routes[0]["steps"]],
            ["insert_fuse", "turn_on_switch", "insert_fuse",
             "turn_on_switch", "swipe_keycard", "door_opens"],
        )
        self.assertEqual(len(routes[0]["alternate_endings"]), 1)
        self.assertEqual(
            [step["action"] for step in routes[0]["alternate_endings"][0]],
            ["swipe_keycard", "door_opens"],
        )

    def test_radtown_prefab_display_names_match_rust_landmarks(self):
        sewer = monument_metadata(PREFIX + "medium/radtown_small_3.prefab")
        mining = monument_metadata(PREFIX + "roadside/radtown_1.prefab")

        self.assertEqual(sewer["display_name"], "Sewer Branch")
        self.assertEqual(mining["display_name"], "Mining Outpost")

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
        self.assertEqual(payload["schema_version"], 13)
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

    def test_detailed_export_merges_asset_and_custom_map_interactables(self):
        compound = PREFIX + "medium/compound.prefab"
        recycler = "assets/bundled/prefabs/static/recycler_static.prefab"
        custom_crate = "assets/bundled/prefabs/radtown/crate_normal.prefab"
        diesel = (
            "assets/content/structures/excavator/prefabs/"
            "diesel_collectable.prefab"
        )
        manifest = PrefabManifest(
            entries={
                1: PrefabManifestEntry(1, compound),
                2: PrefabManifestEntry(2, recycler),
                3: PrefabManifestEntry(3, custom_crate),
                4: PrefabManifestEntry(4, diesel),
            }, collisions={}, source_bundle="test", source_size=0,
            source_mtime_ns=0,
        )
        prefabs = [
            SimpleNamespace(
                prefab_id=1,
                position=SimpleNamespace(x=100.0, y=10.0, z=200.0),
                rotation=SimpleNamespace(x=0.0, y=90.0, z=0.0),
                scale=None, category="Monument",
            ),
            SimpleNamespace(
                prefab_id=2,
                position=SimpleNamespace(x=110.0, y=12.0, z=210.0),
                rotation=SimpleNamespace(x=0.0, y=90.0, z=0.0),
                scale=None, category="Decor",
            ),
            SimpleNamespace(
                prefab_id=3,
                position=SimpleNamespace(x=120.0, y=12.0, z=200.0),
                rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                scale=None, category="Decor",
            ),
            SimpleNamespace(
                prefab_id=4,
                position=SimpleNamespace(x=120.0, y=13.0, z=210.0),
                rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0),
                scale=None, category="Decor",
            ),
        ]
        world = SimpleNamespace(
            size=1000, serialization_version=1, timestamp=2, prefabs=prefabs,
        )

        def matrix(x, y, z):
            return [[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, y],
                    [0.0, 0.0, 1.0, z], [0.0, 0.0, 0.0, 1.0]]

        database = {
            "schema_version": 3,
            "prefab_count": 1,
            "source": {"rust_build_id": "test"},
            "extraction": {},
            "prefabs": {compound: {
                "safe_zone": True, "recycler_count": 1,
                "keycard_requirements": [], "puzzle_type": "electrical",
                "loot_tier": 0,
                "bounds": {
                    "center": {"x": 0.0, "y": 0.0, "z": 0.0},
                    "extents": {"x": 100.0, "y": 100.0, "z": 100.0},
                },
                "interactables": [{
                    "type": "recycler", "component_type": "Recycler",
                    "object_name": "vanilla recycler", "properties": {},
                    "local_matrix": matrix(1.0, 2.0, 3.0),
                }],
                "loot_spawn_groups": [{
                    "kind": "barrel", "object_name": "Barrel Spawner",
                    "max_population": 2,
                    "spawn_per_tick_min": 1, "spawn_per_tick_max": 2,
                    "respawn_seconds_min": 1800.0,
                    "respawn_seconds_max": 2200.0,
                    "wants_initial_spawn": True, "prevent_duplicates": False,
                    "variants": [{
                        "type": "barrel",
                        "prefab_path": "assets/test/loot_barrel_1.prefab",
                        "weight": 1,
                    }],
                    "spawn_points": [{
                        "component_type": "GenericSpawnPoint",
                        "local_position": {"x": 10.0, "y": 0.0, "z": 0.0},
                        "radius": 0.0,
                    }, {
                        "component_type": "RadialSpawnPoint",
                        "local_position": {"x": 20.0, "y": 0.0, "z": 0.0},
                        "radius": 2.0,
                    }],
                }],
                "radiation_zones": [{
                    "object_name": "RadiationSphere",
                    "local_matrix": matrix(0.0, 0.0, 0.0),
                    "tier": "low", "tier_value": 1,
                    "radiation_amount": 10.0, "dynamic": False,
                    "bypass_armor": False, "falloff": 0.5,
                    "increase_near_center": True,
                    "use_line_of_sight": False,
                    "ignore_above_ground": False,
                    "minimum_local_height": None,
                    "shape": "sphere", "radius": 5.0,
                    "scale_radius": True,
                }],
                "puzzles": [{
                    "id": "puzzle-001", "root_node_ids": ["node-001"],
                    "ordering": "test ordering",
                    "reset": {"component_type": "PuzzleReset",
                              "object_name": "reset",
                              "local_matrix": matrix(2.0, 0.0, 4.0)},
                    "nodes": [{
                        "id": "node-001", "depth": 0,
                        "component_type": "ItemBasedFlowRestrictor",
                        "object_name": "FuseBox", "action": None,
                        "player_action": False, "properties": {},
                        "local_matrix": matrix(2.0, 1.0, 4.0),
                    }, {
                        "id": "node-002", "depth": 1,
                        "component_type": "ElectricSwitch",
                        "object_name": "switch", "action": "activate_switch",
                        "player_action": True, "properties": {},
                        "local_matrix": matrix(3.0, 1.0, 5.0),
                    }, {
                        "id": "node-003", "depth": 2,
                        "component_type": "CardReader",
                        "object_name": "GCD", "action": "swipe_keycard",
                        "player_action": True,
                        "properties": {"keycard": "green", "access_level": 1},
                        "local_matrix": matrix(4.0, 1.0, 6.0),
                    }, {
                        "id": "node-004", "depth": 3,
                        "component_type": "DoorManipulator",
                        "object_name": "GCD", "action": None,
                        "player_action": False, "properties": {},
                        "local_matrix": matrix(5.0, 1.0, 7.0),
                    }, {
                        "id": "node-005", "depth": 0,
                        "component_type": "ElectricSwitch",
                        "object_name": "LightSwitch_Hallway",
                        "action": "activate_switch", "player_action": True,
                        "properties": {},
                        "local_matrix": matrix(6.0, 1.0, 8.0),
                    }, {
                        "id": "node-006", "depth": 0,
                        "component_type": "PressButton",
                        "object_name": "ExitButton", "action": "press_button",
                        "player_action": True, "properties": {},
                        "local_matrix": matrix(7.0, 1.0, 9.0),
                    }, {
                        "id": "node-007", "depth": 2,
                        "component_type": "CardReader",
                        "object_name": "GCD_Alternate",
                        "action": "swipe_keycard", "player_action": True,
                        "properties": {"keycard": "green", "access_level": 1},
                        "local_matrix": matrix(8.0, 1.0, 10.0),
                    }, {
                        "id": "node-008", "depth": 3,
                        "component_type": "DoorManipulator",
                        "object_name": "GCD_Alternate", "action": None,
                        "player_action": False, "properties": {},
                        "local_matrix": matrix(9.0, 1.0, 11.0),
                    }],
                    "edges": [
                        {"from": "node-001", "to": "node-002"},
                        {"from": "node-002", "to": "node-003"},
                        {"from": "node-003", "to": "node-004"},
                        {"from": "node-005", "to": "node-004"},
                        {"from": "node-006", "to": "node-004"},
                        {"from": "node-002", "to": "node-007"},
                        {"from": "node-007", "to": "node-008"},
                    ],
                    "ordered_steps": [],
                }],
            }},
        }
        with (
            patch("rustmap_parser.monuments._visible_landmarks",
                  return_value=([None], False)),
            patch("rustmap_parser.monuments._gameplay_database",
                  return_value=database),
        ):
            payload, interactables, puzzles, loot, radiation = (
                _build_monument_documents(
                world, manifest, interactable=True, puzzles=True, loot=True,
                radiation_zones=True,
                )
            )
            map_only = _build_monument_documents(
                world, manifest, interactable=True, puzzles=True, loot=True,
                radiation_zones=True,
                transforms=TransformOptions(
                    local_position=False, position=False, map_position=True,
                ),
            )

        item = payload["monuments"][0]
        self.assertTrue(payload["details"]["interactables_enabled"])
        self.assertTrue(payload["details"]["puzzles_enabled"])
        self.assertEqual(payload["details"]["interactable_count"], 2)
        self.assertEqual(payload["details"]["puzzle_count"], 1)
        self.assertNotIn("unassigned_interactables", payload)
        self.assertEqual(item["metadata"]["gameplay"]["recycler_count"], 2)
        self.assertEqual(item["metadata"]["gameplay"]["maximum_radiation"], 10.0)
        self.assertNotIn("interactables", item)
        self.assertNotIn("puzzles", item)
        self.assertNotIn("loot", item)
        self.assertNotIn("radiation", item)
        self.assertEqual(interactables["interactable_count"], 2)
        self.assertEqual(interactables["unassigned_interactables"], [])
        interactable_items = interactables["monuments"][0]["interactables"]
        self.assertEqual(
            [value["source"] for value in interactable_items],
            ["prefab_asset", "map_prefab"],
        )
        self.assertEqual(interactable_items[0]["position"], {
            "x": 103.0, "y": 12.0, "z": 199.0,
        })
        self.assertEqual(interactable_items[1]["local_position"], {
            "x": -10.0, "y": 2.0, "z": 10.0,
        })
        self.assertEqual(loot["loot_count"], 4)
        self.assertEqual(loot["monuments"][0]["prefab"], compound)
        first_loot = loot["monuments"][0]["loot"][0]
        self.assertEqual(first_loot["kind"], "barrel")
        first_marker = first_loot["positions"][0]
        self.assertEqual(first_marker["local_position"], {
            "x": 10.0, "y": 0.0, "z": 0.0,
        })
        self.assertEqual(first_marker["position"], {
            "x": 100.0, "y": 10.0, "z": 190.0,
        })
        self.assertEqual(first_marker["map_position"], {
            "x": 600.0, "y": 690.0,
        })
        self.assertEqual(first_marker["radius"], 0.0)
        custom_loot = loot["monuments"][0]["loot"][1]
        self.assertEqual(custom_loot["kind"], "crate")
        self.assertEqual(custom_loot["prefabs"], [{
            "kind": "crate", "prefab": custom_crate,
        }])
        diesel_loot = loot["monuments"][0]["loot"][2]
        self.assertEqual(diesel_loot["kind"], "diesel_fuel")
        self.assertEqual(diesel_loot["prefabs"], [{
            "kind": "diesel_fuel", "prefab": diesel,
        }])
        self.assertEqual(radiation["zone_count"], 1)
        self.assertEqual(radiation["monuments"][0]["prefab"], compound)
        zone = radiation["monuments"][0]["zones"][0]
        self.assertEqual(zone["radiation_amount"], 10.0)
        self.assertEqual(zone["radius"], 5.0)
        self.assertEqual(zone["local_position"], {
            "x": 0.0, "y": 0.0, "z": 0.0,
        })
        self.assertEqual(zone["position"], {
            "x": 100.0, "y": 10.0, "z": 200.0,
        })
        self.assertEqual(zone["map_position"], {
            "x": 600.0, "y": 700.0,
        })
        self.assertEqual(puzzles["puzzle_count"], 1)
        route = puzzles["monuments"][0]["puzzles"][0]
        self.assertEqual(route["kind"], "keycard_route")
        self.assertEqual(route["required_keycards"], ["green"])
        self.assertEqual(route["endpoint_count"], 2)
        self.assertEqual(route["common_step_count"], 2)
        self.assertEqual(
            [step["action"] for step in route["steps"]],
            ["insert_fuse", "turn_on_switch", "swipe_keycard", "door_opens"],
        )
        self.assertNotIn("nodes", route)
        self.assertNotIn("edges", route)
        self.assertEqual(
            [step["action"] for step in route["alternate_endings"][0]["steps"]],
            ["swipe_keycard", "door_opens"],
        )
        self.assertEqual(route["steps"][1]["position"], {
            "x": 105.0, "y": 11.0, "z": 197.0,
        })
        serialized_map_only = str(map_only)
        self.assertNotIn("'local_position':", serialized_map_only)
        self.assertNotIn("'position':", serialized_map_only)
        self.assertIn("'map_position':", serialized_map_only)
        for document in map_only:
            self.assertEqual(document["exported_position_fields"], [
                "map_position",
            ])

    def test_detailed_export_includes_nonstandard_monument_category_roots(self):
        path = "assets/custom/my_server/fortified_scrapyard.prefab"
        manifest = PrefabManifest(
            entries={1: PrefabManifestEntry(1, path)}, collisions={},
            source_bundle="test", source_size=0, source_mtime_ns=0,
        )
        world = SimpleNamespace(
            size=1000, serialization_version=1, timestamp=2,
            prefabs=[SimpleNamespace(
                prefab_id=1,
                position=SimpleNamespace(x=10.0, y=5.0, z=20.0),
                rotation=None, scale=None, category="Monument",
            )],
        )
        database = {
            "schema_version": 3, "prefab_count": 0,
            "source": {}, "extraction": {}, "prefabs": {},
        }
        with patch("rustmap_parser.monuments._gameplay_database",
                   return_value=database):
            basic = build_monument_export(world, manifest)
            detailed = build_monument_export(
                world, manifest, interactable=True, puzzles=True
            )
        self.assertEqual(basic["monument_count"], 0)
        self.assertEqual(detailed["monument_count"], 1)
        item = detailed["monuments"][0]
        self.assertEqual(item["metadata"]["classification"]["kind"],
                         "custom_monument")
        self.assertEqual(item["metadata"]["display_name"],
                         "Fortified Scrapyard")


if __name__ == "__main__":
    unittest.main()
