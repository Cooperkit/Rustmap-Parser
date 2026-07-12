import json
from importlib import resources

from rustmap_parser.populations import SpawnRule, heatmap_categories


def _rule(name: str, asset_path: str) -> SpawnRule:
    return SpawnRule(
        asset_path=asset_path,
        name=name,
        class_name="DensitySpawnPopulation",
        active=True,
        resource_folder="collectable/hemp",
        resources=[],
        target_density=1.0,
        scale_with_large_maps=False,
        scale_with_spawn_filter=True,
        splat_mask=0,
        biome_mask=0,
        topology_any=0,
        topology_all=0,
        topology_not=0,
        filter_cutoff=0.0,
        filter_radius=0.0,
        filter_out_tutorial_islands=False,
        filter_out_monuments=[],
        align_to_normal=False,
        npc_radius_check_distance=0.0,
    )


def test_hemp_category_combines_temperate_and_jungle_populations() -> None:
    normal = "assets/content/properties/spawnpopulation/collectable-resource-hemp.asset"
    jungle = "assets/content/properties/spawnpopulation/jungle-collectable-resource-hemp.asset"
    categories = heatmap_categories([
        _rule("collectable-resource-hemp", normal),
        _rule("jungle-collectable-resource-hemp", jungle),
    ])
    assert categories["hemp"] == [normal, jungle]


def test_packaged_spawn_database_contains_hemp_category() -> None:
    payload = json.loads(
        resources.files("rustmap_parser.data").joinpath("spawn_rules.json").read_text(encoding="utf-8")
    )
    assert len(payload["heatmap_categories"]["hemp"]) == 2
