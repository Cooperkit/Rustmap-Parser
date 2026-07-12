"""Extract Rust's GameManifest and resolve numeric map prefab IDs."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from .parser import RustMap, RustMapError


@dataclass(slots=True)
class PrefabManifestEntry:
    prefab_id: int
    path: str
    guid: str | None = None
    pooled: bool | None = None


@dataclass(slots=True)
class PrefabResolution:
    prefab_id: int
    path: str | None
    instance_count: int
    map_categories: list[str]
    classification: str
    heatmap_relevance: str
    guid: str | None = None
    pooled: bool | None = None


@dataclass(slots=True)
class PrefabManifest:
    entries: dict[int, PrefabManifestEntry]
    collisions: dict[int, list[str]]
    source_bundle: str
    source_size: int
    source_mtime_ns: int

    def get(self, prefab_id: int) -> PrefabManifestEntry | None:
        return self.entries.get(prefab_id)

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "source_bundle": self.source_bundle,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "entry_count": len(self.entries),
            "entries": {
                str(key): asdict(value) for key, value in sorted(self.entries.items())
            },
            "collisions": {str(k): v for k, v in sorted(self.collisions.items())},
        }
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "PrefabManifest":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = {
            int(key): PrefabManifestEntry(**value)
            for key, value in payload["entries"].items()
        }
        return cls(
            entries=entries,
            collisions={int(k): v for k, v in payload.get("collisions", {}).items()},
            source_bundle=payload["source_bundle"],
            source_size=int(payload["source_size"]),
            source_mtime_ns=int(payload["source_mtime_ns"]),
        )


def find_content_bundle(rust_install: str | Path) -> Path:
    root = Path(rust_install)
    candidates = (
        root / "Bundles" / "shared" / "content.bundle",
        root / "shared" / "content.bundle",
        root if root.name.casefold() == "content.bundle" else None,
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Could not find Bundles/shared/content.bundle below {root}")


def extract_game_manifest(content_bundle: str | Path) -> PrefabManifest:
    """Extract the authoritative ID/path table from Rust's GameManifest asset."""
    try:
        import UnityPy
    except ImportError as exc:
        raise RuntimeError("UnityPy is required to extract the installed GameManifest") from exc

    bundle = Path(content_bundle).resolve()
    stat = bundle.stat()
    environment = UnityPy.load(str(bundle))

    game_manifest_script_id: int | None = None
    for obj in environment.objects:
        if obj.type.name != "MonoScript":
            continue
        script = obj.read()
        if getattr(script, "m_ClassName", "") == "GameManifest":
            game_manifest_script_id = obj.path_id
            break
    if game_manifest_script_id is None:
        raise RustMapError("GameManifest MonoScript was not found in content.bundle")

    tree = None
    for obj in environment.objects:
        if obj.type.name != "MonoBehaviour":
            continue
        behaviour = obj.read()
        if behaviour.m_Script.path_id == game_manifest_script_id:
            tree = obj.read_typetree()
            break
    if tree is None:
        raise RustMapError("GameManifest data object was not found in content.bundle")

    properties_by_path: dict[str, dict] = {
        item["name"].casefold(): item for item in tree.get("prefabProperties", [])
    }
    entries: dict[int, PrefabManifestEntry] = {}
    all_paths: dict[int, list[str]] = defaultdict(list)
    for item in tree.get("pooledStrings", []):
        prefab_id = int(item["hash"])
        asset_path = item["str"]
        all_paths[prefab_id].append(asset_path)
        prop = properties_by_path.get(asset_path.casefold())
        entries[prefab_id] = PrefabManifestEntry(
            prefab_id=prefab_id,
            path=asset_path,
            guid=prop.get("guid") if prop else None,
            pooled=bool(prop.get("pool")) if prop else None,
        )

    collisions = {
        prefab_id: sorted(set(paths))
        for prefab_id, paths in all_paths.items()
        if len(set(paths)) > 1
    }
    return PrefabManifest(
        entries=entries,
        collisions=collisions,
        source_bundle=str(bundle),
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
    )


def load_or_extract_manifest(
    rust_install: str | Path, cache_path: str | Path | None = None
) -> PrefabManifest:
    bundle = find_content_bundle(rust_install)
    stat = bundle.stat()
    if cache_path is not None and Path(cache_path).is_file():
        cached = PrefabManifest.load(cache_path)
        if cached.source_size == stat.st_size and cached.source_mtime_ns == stat.st_mtime_ns:
            return cached
    manifest = extract_game_manifest(bundle)
    if cache_path is not None:
        manifest.save(cache_path)
    return manifest


def classify_prefab(path: str | None, map_categories: list[str]) -> tuple[str, str]:
    if path is None:
        return "unresolved", "unknown"
    value = path.casefold().replace("\\", "/")
    category_text = " ".join(map_categories).casefold()

    if "/monument/" in value or "monument" in category_text:
        return "monument", "spawn exclusion and orientation reference"
    if any(token in value for token in (
        "rock_formation", "/cliff", "cliff_", "coastal_rock", "shore_ice",
        "iceberg", "ice_sheet", "rockformation",
    )):
        return "terrain_geometry", "slope/ore environment and spawn exclusion"
    if any(token in category_text for token in ("road", "powerline")) or any(
        token in value for token in ("/road", "roadside", "powerline")
    ):
        return "transport_corridor", "vehicle and junkpile spawn environment"
    if "river" in category_text or any(
        token in value for token in ("/river", "riversound", "waterfall", "water_")
    ):
        return "water_feature", "water/river spawn environment"
    if "dungeon" in category_text or "/dungeon" in value:
        return "dungeon", "spawn exclusion and underground geometry"
    if any(token in value for token in (
        "/resource/", "/collectable/", "/animals/", "/plant/", "tree",
        "bush", "foliage", "vine",
    )):
        return "natural_resource", "possible direct resource or vegetation feature"
    if "decor" in category_text or "/decor/" in value:
        return "decor", "possible spawn environment or exclusion geometry"
    return "other", "not yet assigned"


def resolve_world_prefabs(
    world: RustMap, manifest: PrefabManifest
) -> list[PrefabResolution]:
    counts = Counter(item.prefab_id for item in world.prefabs)
    categories: dict[int, set[str]] = defaultdict(set)
    for item in world.prefabs:
        categories[item.prefab_id].add(item.category)

    result: list[PrefabResolution] = []
    for prefab_id, count in counts.most_common():
        entry = manifest.get(prefab_id)
        map_categories = sorted(categories[prefab_id])
        classification, relevance = classify_prefab(
            entry.path if entry else None, map_categories
        )
        result.append(PrefabResolution(
            prefab_id=prefab_id,
            path=entry.path if entry else None,
            instance_count=count,
            map_categories=map_categories,
            classification=classification,
            heatmap_relevance=relevance,
            guid=entry.guid if entry else None,
            pooled=entry.pooled if entry else None,
        ))
    return result


def write_resolution_reports(
    world: RustMap,
    manifest: PrefabManifest,
    output_dir: str | Path,
) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    resolutions = resolve_world_prefabs(world, manifest)
    resolved = [item for item in resolutions if item.path is not None]
    unresolved = [item for item in resolutions if item.path is None]
    classification_instances = Counter()
    for item in resolutions:
        classification_instances[item.classification] += item.instance_count

    report = {
        "manifest": {
            "source_bundle": manifest.source_bundle,
            "source_size": manifest.source_size,
            "entry_count": len(manifest.entries),
            "hash_collision_count": len(manifest.collisions),
        },
        "map": {
            "world_size": world.size,
            "prefab_instances": len(world.prefabs),
            "unique_prefab_ids": len(resolutions),
        },
        "coverage": {
            "resolved_unique_ids": len(resolved),
            "unresolved_unique_ids": len(unresolved),
            "resolved_instances": sum(x.instance_count for x in resolved),
            "unresolved_instances": sum(x.instance_count for x in unresolved),
        },
        "classification_instance_counts": dict(classification_instances.most_common()),
        "unresolved": [asdict(item) for item in unresolved],
        "resolved": [asdict(item) for item in resolutions],
    }
    (output / "phase2_resolution.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    with (output / "resolved_prefabs.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow((
            "prefab_id", "instance_count", "map_categories", "classification",
            "heatmap_relevance", "path", "guid", "pooled",
        ))
        for item in resolutions:
            writer.writerow((
                item.prefab_id, item.instance_count, ";".join(item.map_categories),
                item.classification, item.heatmap_relevance, item.path or "",
                item.guid or "", "" if item.pooled is None else item.pooled,
            ))
    return report
