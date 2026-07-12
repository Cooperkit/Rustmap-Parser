"""Extract and evaluate Rust server DensitySpawnPopulation filters."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .layers import biome_grid, splat_grid, topology_grid


@dataclass(slots=True)
class SpawnRule:
    asset_path: str
    name: str
    class_name: str
    active: bool
    resource_folder: str
    resources: list[str]
    target_density: float
    scale_with_large_maps: bool
    scale_with_spawn_filter: bool
    splat_mask: int
    biome_mask: int
    topology_any: int
    topology_all: int
    topology_not: int
    filter_cutoff: float
    filter_radius: float
    filter_out_tutorial_islands: bool
    filter_out_monuments: list[int]
    align_to_normal: bool
    npc_radius_check_distance: float
    population_convar: str | None = None


def heatmap_categories(rules: list[SpawnRule]) -> dict[str, list[str]]:
    """Map Rust+ Desktop's category keys to active population asset paths."""
    exact = {
        "ores": {"ores", "ores_sand", "ores_snow", "v3_ores_jungle"},
        "wood": {"collectable-resource-wood"},
        "mushroom": {"collectable-food-mushroom"},
        "corn": {"plant-corn"}, "pumpkin": {"plant-pumpkin"},
        "potato": {"plant-potato"}, "wheat": {"plant-wheat"},
        "junkpiles": {"junkpiles", "junkpiles_water"},
        "rowboat": {"rowboat.population"}, "modularcar": {"modularcar.population"},
        "horse": {"ridablehorse.population"},
        # Monument bikes use fixed spawn points, not a terrain distribution.
        # Including their unrestricted default filter makes the raster solid.
        "pedalbike": {"pedalbikes_world.population"},
        "hab": {"hab.population"},
        "hemp": {"collectable-resource-hemp", "jungle-collectable-resource-hemp"},
        "bear": {"bear.population", "polarbear.population"},
        "boar": {"boar.population"}, "chicken": {"chicken.population"},
        "wolf": {"wolf.population"}, "stag": {"stag.population"},
        "crocodile": {"crocodile.population"}, "tiger": {"tiger.population"},
        "snake": {"snake.population"},
    }
    result = {key: [] for key in exact}
    result.update({"logs": [], "berries": [], "flowers": []})
    for rule in rules:
        if not rule.active:
            continue
        name = rule.name.casefold()
        for category, names in exact.items():
            if name in names:
                result[category].append(rule.asset_path)
        if name.startswith("logs_") or name in {"driftwood", "wood_log_pile"}:
            result["logs"].append(rule.asset_path)
        if "plant-berry-" in name:
            result["berries"].append(rule.asset_path)
        if name in {"plant-rose", "plant-orchid", "plant-sunflower"}:
            result["flowers"].append(rule.asset_path)
    return {key: sorted(value) for key, value in sorted(result.items())}


def _u32(value: int) -> int:
    return int(value) & 0xFFFFFFFF


def _ptr_id(value: Any) -> int:
    if isinstance(value, dict):
        return int(value.get("m_PathID", value.get("path_id", 0)))
    return int(getattr(value, "path_id", 0))


def _script_classes(environment) -> dict[int, str]:
    result = {}
    for obj in environment.objects:
        if obj.type.name == "MonoScript":
            data = obj.read()
            result[obj.path_id] = getattr(data, "m_ClassName", "")
    return result


def _class_name(data, fallback: dict[int, str], obj=None) -> str:
    """Resolve a script within its serialized file (path IDs repeat per file)."""
    try:
        return str(data.m_Script.get_obj().read().m_ClassName)
    except Exception:
        try:
            script = obj.assets_file.objects[data.m_Script.path_id]
            return str(script.read().m_ClassName)
        except Exception:
            return fallback.get(data.m_Script.path_id, "")


def _guid_paths(environment, classes: dict[int, str]) -> dict[str, str]:
    for obj in environment.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        data = obj.read()
        if _class_name(data, classes, obj) != "GameManifest":
            continue
        tree = obj.read_typetree()
        # Current Rust stores parallel GUID/path arrays. Retain fallbacks for
        # older manifest layouts.
        guids = tree.get("guidList") or tree.get("guids") or []
        paths = tree.get("guidPaths") or tree.get("paths") or []
        if isinstance(paths, list) and paths and isinstance(paths[0], dict):
            return {
                str(x.get("guid", "")): str(x.get("path", x.get("name", "")))
                for x in paths if x.get("guid")
            }
        if len(guids) == len(paths):
            return {str(g): str(p) for g, p in zip(guids, paths)}
        for item in tree.get("prefabProperties", []):
            if item.get("guid") and item.get("name"):
                guids.append(item["guid"]); paths.append(item["name"])
        return {str(g): str(p) for g, p in zip(guids, paths)}
    return {}


def _active_population_ids_local(maps_bundle: Path) -> tuple[set[int], dict]:
    import UnityPy

    env = UnityPy.load(str(maps_bundle))
    classes = _script_classes(env)
    candidates = []
    # Locate SpawnHandler scripts first, then inspect only their serialized
    # files. A full typetree scan of the 50k behaviours is unnecessarily large.
    for script in env.objects:
        if script.type.name != "MonoScript" or getattr(script.read(), "m_ClassName", "") != "SpawnHandler":
            continue
        for obj in script.assets_file.objects.values():
            if obj.type.name != "MonoBehaviour":
                continue
            data = obj.read()
            if data.m_Script.path_id != script.path_id:
                continue
            tree = obj.read_typetree()
            regular = tree.get("SpawnPopulations", [])
            convar = tree.get("ConvarSpawnPopulations", [])
            refs = regular + convar
            ids = {_ptr_id(x) for x in refs if _ptr_id(x)}
            if ids:
                candidates.append((len(ids), ids, obj.path_id, len(regular), len(convar)))
    if not candidates:
        raise RuntimeError("No SpawnHandler was found in maps.bundle")
    count, ids, path_id, regular_count, convar_count = max(candidates, key=lambda x: x[0])
    result = ids, {"handler_count": len(candidates), "selected_path_id": path_id,
                   "selected_population_count": count,
                   "regular_population_count": regular_count,
                   "convar_population_count": convar_count}
    del env
    return result


def _active_worker(path: str, connection) -> None:
    try:
        ids, metadata = _active_population_ids_local(Path(path))
        connection.send((sorted(ids), metadata, None))
    except Exception as exc:
        connection.send(([], {}, repr(exc)))
    finally:
        connection.close()


def _active_population_ids(maps_bundle: Path) -> tuple[set[int], dict]:
    """Read maps.bundle in a disposable process to release UnityPy's graph."""
    import multiprocessing as mp
    parent, child = mp.get_context("spawn").Pipe(False)
    process = mp.get_context("spawn").Process(
        target=_active_worker, args=(str(maps_bundle), child)
    )
    process.start()
    ids, metadata, error = parent.recv()
    process.join()
    if error:
        raise RuntimeError(f"SpawnHandler extraction failed: {error}")
    return set(ids), metadata


def extract_spawn_rules(content_bundle: str | Path, maps_bundle: str | Path) -> dict:
    """Extract all population assets and mark those in the main SpawnHandler."""
    import UnityPy

    content = Path(content_bundle).resolve()
    maps = Path(maps_bundle).resolve()
    active_ids, handler = _active_population_ids(maps)
    env = UnityPy.load(str(content))
    classes = _script_classes(env)
    guid_paths = _guid_paths(env, classes)

    populations = {
        "DensitySpawnPopulation", "ConvarControlledSpawnPopulation",
        "ConvarControlledSpawnPopulationRail", "SpawnPointSpawnPopulation",
        "ConvarControlledSpawnPointPopulation",
    }
    rules = []
    for asset_path, obj in env.container.items():
        normalized_path = str(asset_path).casefold().replace("\\", "/")
        if "/spawnpopulation/" not in normalized_path and not normalized_path.endswith(".population.asset"):
            continue
        if obj.type.name != "MonoBehaviour":
            continue
        data = obj.read()
        class_name = _class_name(data, classes, obj)
        if class_name not in populations:
            continue
        tree = obj.read_typetree()
        filt = tree.get("Filter", {})
        resources = []
        for ref in tree.get("ResourceList", []):
            guid = ref.get("guid", ref) if isinstance(ref, dict) else ref
            resources.append(guid_paths.get(str(guid), str(guid)))
        convar = tree.get("PopulationConvar")
        if isinstance(convar, dict):
            convar = convar.get("name") or convar.get("Value") or str(convar)
        rules.append(SpawnRule(
            asset_path=str(asset_path), name=str(tree.get("m_Name", Path(asset_path).stem)),
            class_name=class_name, active=obj.path_id in active_ids,
            resource_folder=str(tree.get("ResourceFolder", "")), resources=resources,
            target_density=float(tree.get("_targetDensity", 0)),
            scale_with_large_maps=bool(tree.get("ScaleWithLargeMaps", False)),
            scale_with_spawn_filter=bool(tree.get("ScaleWithSpawnFilter", False)),
            splat_mask=int(filt.get("SplatType", -1)), biome_mask=int(filt.get("BiomeType", -1)),
            topology_any=_u32(filt.get("TopologyAny", -1)),
            topology_all=_u32(filt.get("TopologyAll", -1)),
            topology_not=_u32(filt.get("TopologyNot", 0)),
            filter_cutoff=float(tree.get("FilterCutoff", 0)),
            filter_radius=float(tree.get("FilterRadius", 0)),
            filter_out_tutorial_islands=bool(tree.get("FilterOutTutorialIslands", False)),
            filter_out_monuments=[int(x) for x in tree.get("FilterOutMonuments", [])],
            align_to_normal=bool(tree.get("AlignToNormal", False)),
            npc_radius_check_distance=float(tree.get("NpcRadiusCheckDistance", 0)),
            population_convar=str(convar) if convar else None,
        ))
    rules.sort(key=lambda x: x.asset_path.casefold())
    return {
        "schema_version": 1,
        "sources": {
            "content_bundle": {"path": str(content), "size": content.stat().st_size,
                               "mtime_ns": content.stat().st_mtime_ns},
            "maps_bundle": {"path": str(maps), "size": maps.stat().st_size,
                            "mtime_ns": maps.stat().st_mtime_ns},
        },
        "spawn_handler": handler,
        "rule_count": len(rules), "active_rule_count": sum(r.active for r in rules),
        "heatmap_categories": heatmap_categories(rules),
        "rules": [asdict(r) for r in rules],
    }


def save_rule_database(database: dict, path: str | Path) -> None:
    target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(database, indent=2) + "\n", encoding="utf-8")


class SpawnFilterEvaluator:
    """Reusable population evaluator with resolution-specific sampling caches."""

    def __init__(self, world, resolution: int = 512):
        self.resolution = resolution
        self.source_splat = splat_grid(world)
        source_biome = biome_grid(world)
        source_topology = topology_grid(world)
        self.norm = (np.arange(resolution, dtype=np.float32) + 0.5) / resolution
        ix = np.minimum((self.norm * source_topology.shape[1]).astype(int), source_topology.shape[1] - 1)
        iz = np.minimum((self.norm * source_topology.shape[0]).astype(int), source_topology.shape[0] - 1)
        self.topology = source_topology[np.ix_(iz, ix)]
        sampled_biome = source_biome[:, iz][:, :, ix]
        # Server uses >=, so ties select the last/highest channel.
        self.dominant_biome = sampled_biome.shape[0] - 1 - np.argmax(sampled_biome[::-1], axis=0)
        self.biome_count = sampled_biome.shape[0]
        self._splat_cache: dict[int, np.ndarray] = {}
        # Bilinear sampling a 2048² splat channel is relatively expensive, and
        # the rule database combines the same eight channels into many masks.
        # Keep float64 terms because the original NumPy expression evaluates in
        # float64 before each in-place float32 addition.  Storing float32 here
        # changes a small number of final uint8 values.
        self._splat_channel_cache: dict[int, np.ndarray] = {}
        self._evaluation_cache: dict[tuple[int, int, int, int, int, float], np.ndarray] = {}

        source = self.source_splat
        self._splat_pos = self.norm * (source.shape[2] - 1)
        self._splat_x0 = np.floor(self._splat_pos).astype(int)
        self._splat_x1 = np.minimum(self._splat_x0 + 1, source.shape[2] - 1)
        self._splat_t = self._splat_pos - self._splat_x0

    def _sample_splat_channel(self, channel: int) -> np.ndarray:
        cached = self._splat_channel_cache.get(channel)
        if cached is not None:
            return cached
        grid = self.source_splat[channel]
        x0, x1, t = self._splat_x0, self._splat_x1, self._splat_t
        a = grid[np.ix_(x0, x0)]
        b = grid[np.ix_(x0, x1)]
        c = grid[np.ix_(x1, x0)]
        d = grid[np.ix_(x1, x1)]
        sampled = ((a * (1-t)[None,:] + b * t[None,:]) * (1-t)[:,None]
                   + (c * (1-t)[None,:] + d * t[None,:]) * t[:,None]) / 255.0
        self._splat_channel_cache[channel] = sampled
        return sampled

    def _splat_factor(self, mask: int) -> np.ndarray:
        cached = self._splat_cache.get(mask)
        if cached is not None:
            return cached
        value = np.zeros((self.resolution, self.resolution), dtype=np.float32)
        for channel in range(self.source_splat.shape[0]):
            if not (mask & (1 << channel)):
                continue
            value += self._sample_splat_channel(channel)
        value = np.clip(value, 0, 1)
        self._splat_cache[mask] = value
        return value

    def evaluate(self, rule: SpawnRule | dict) -> np.ndarray:
        if isinstance(rule, dict):
            rule = SpawnRule(**rule)
        cache_key = (
            _u32(rule.topology_any), _u32(rule.topology_all), _u32(rule.topology_not),
            int(rule.biome_mask), int(rule.splat_mask), float(rule.filter_cutoff),
        )
        cached = self._evaluation_cache.get(cache_key)
        if cached is not None:
            return cached
        topo = self.topology
        factor = np.ones((self.resolution, self.resolution), dtype=np.float32)
        any_mask, all_mask, not_mask = map(_u32, (rule.topology_any, rule.topology_all, rule.topology_not))
        if any_mask != 0xFFFFFFFF: factor[(topo & any_mask) == 0] = 0
        if all_mask != 0xFFFFFFFF: factor[(topo & all_mask) != all_mask] = 0
        if not_mask: factor[(topo & not_mask) != 0] = 0

        if rule.biome_mask != -1:
            allowed = np.array([(rule.biome_mask & (1 << i)) != 0 for i in range(self.biome_count)])
            factor[~allowed[self.dominant_biome]] = 0
        if rule.splat_mask != -1:
            factor *= self._splat_factor(rule.splat_mask)
        result = np.where(factor > rule.filter_cutoff, factor * 255, 0).astype(np.uint8)
        self._evaluation_cache[cache_key] = result
        return result


def evaluate_filter(world, rule: SpawnRule | dict, resolution: int = 512) -> np.ndarray:
    """Reproduce DensitySpawnPopulation.GetBaseMapValues (placement map excluded)."""
    return SpawnFilterEvaluator(world, resolution).evaluate(rule)
