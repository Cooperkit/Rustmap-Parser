"""Refresh every versioned ``src/rustmap_parser/data`` resource.

Run this file directly from a source checkout. No command-line arguments are
used. Set RUST_INSTALL_PATH below, set the RUST_INSTALL_PATH environment
variable, or leave it as None to search common Steam locations. Exact monument
details may optionally come from a separate Rust dedicated-server install.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

from rustmap_parser.monument_assets import (  # noqa: E402
    SCHEMA_VERSION as MONUMENT_SCHEMA_VERSION,
)
from rustmap_parser.tunnel_assets import bundle_identity, find_rust_install  # noqa: E402


# --- Maintainer configuration ---------------------------------------------
RUST_INSTALL_PATH: Path | None = None
MONUMENT_DETAILS_INSTALL_PATH: Path | None = None
TUNNEL_CACHE_PATH: Path | None = PROJECT_DIR / ".rustmap-cache" / "tunnel-geometry"
# ---------------------------------------------------------------------------


JSON_RESOURCES = (
    "prefab_manifest.json",
    "spawn_rules.json",
    "monument_metadata.json",
    "no_build_zones.json",
    "cargo_harbor_paths.json",
)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sanitize_manifest(path: Path) -> None:
    payload = _read_json(path)
    payload["source_bundle"] = "Bundles/shared/content.bundle"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def _sanitize_spawn_rules(path: Path) -> None:
    payload = _read_json(path)
    sources = payload.get("sources", {})
    if "content_bundle" in sources:
        sources["content_bundle"]["path"] = "Bundles/shared/content.bundle"
    if "maps_bundle" in sources:
        sources["maps_bundle"]["path"] = "Bundles/maps/maps.bundle"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def _stage_worker(stage: str, install_text: str, staging_text: str,
                  tunnel_cache_text: str | None, connection) -> None:
    try:
        install = Path(install_text)
        staging = Path(staging_text)
        if stage == "prefab_manifest":
            from rustmap_parser import refresh_prefab_manifest
            target = staging / "prefab_manifest.json"
            refresh_prefab_manifest(install, target)
            _sanitize_manifest(target)
        elif stage == "spawn_rules":
            from rustmap_parser import refresh_spawn_rules
            target = staging / "spawn_rules.json"
            refresh_spawn_rules(install, target)
            _sanitize_spawn_rules(target)
        elif stage == "monument_metadata":
            from rustmap_parser import refresh_monument_metadata
            refresh_monument_metadata(install, staging / "monument_metadata.json")
        elif stage == "monument_details":
            from rustmap_parser import refresh_monument_details
            refresh_monument_details(install, staging / "monument_metadata.json")
        elif stage == "no_build_zones":
            from rustmap_parser import refresh_no_build_zone_data
            refresh_no_build_zone_data(install, staging / "no_build_zones.json")
        elif stage == "cargo_harbor_paths":
            from rustmap_parser import refresh_cargo_harbor_paths
            refresh_cargo_harbor_paths(install, staging / "cargo_harbor_paths.json")
        elif stage == "cargo_collision_tiles":
            from rustmap_parser import refresh_cargo_collision_tiles
            refresh_cargo_collision_tiles(install, staging / "cargo_collision_tiles")
        elif stage == "tunnel_tiles":
            from rustmap_parser import install_packaged_tunnel_templates, refresh_tunnel_templates
            cache = refresh_tunnel_templates(
                install, Path(tunnel_cache_text) if tunnel_cache_text else None
            )
            install_packaged_tunnel_templates(cache, staging / "tunnel_tiles")
        else:
            raise ValueError(f"Unknown refresh stage: {stage}")
        connection.send({"ok": True})
    except BaseException as exc:
        connection.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


def _run_stage(stage: str, install: Path, staging: Path,
               tunnel_cache: Path | None) -> float:
    started = time.perf_counter()
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_stage_worker,
        args=(stage, str(install), str(staging),
              str(tunnel_cache) if tunnel_cache else None, child),
    )
    process.start()
    child.close()
    process.join()
    result = parent.recv() if parent.poll() else {
        "ok": False, "error": f"worker exited with code {process.exitcode} without a result"
    }
    parent.close()
    if not result.get("ok"):
        raise RuntimeError(f"{stage} refresh failed: {result.get('error', 'unknown error')}")
    return time.perf_counter() - started


def _assert_bundle(value: dict, expected: dict, label: str) -> None:
    if int(value.get("size", -1)) != int(expected["size"]):
        raise RuntimeError(f"{label} bundle size does not match the selected Rust install")
    if int(value.get("mtime_ns", -1)) != int(expected["mtime_ns"]):
        raise RuntimeError(f"{label} bundle timestamp does not match the selected Rust install")


def _monument_detail_summary(monuments: dict) -> dict[str, int]:
    totals = {
        "monument_interactables": 0,
        "monument_puzzles": 0,
        "monument_loot_groups": 0,
        "monument_radiation_zones": 0,
        "monument_diesel_groups": 0,
    }
    diesel_path = (
        "assets/content/structures/excavator/prefabs/"
        "diesel_collectable.prefab"
    )
    required = (
        "bounds", "interactables", "puzzles", "loot_spawn_groups",
        "radiation_zones",
    )
    for prefab_path, item in monuments.get("prefabs", {}).items():
        missing = [name for name in required if name not in item]
        if missing:
            raise RuntimeError(
                f"Monument {prefab_path} is missing detailed fields: "
                + ", ".join(missing)
            )
        totals["monument_interactables"] += len(item["interactables"])
        totals["monument_puzzles"] += len(item["puzzles"])
        totals["monument_loot_groups"] += len(item["loot_spawn_groups"])
        totals["monument_radiation_zones"] += len(item["radiation_zones"])
        for group in item["loot_spawn_groups"]:
            variants = group.get("variants", [])
            diesel = [
                variant for variant in variants
                if str(variant.get("prefab_path", "")).casefold() == diesel_path
            ]
            if not diesel:
                continue
            if any(variant.get("type") != "diesel_fuel" for variant in diesel):
                raise RuntimeError(
                    f"Diesel collectible has an invalid loot type in {prefab_path}"
                )
            totals["monument_diesel_groups"] += 1
    if not totals["monument_interactables"]:
        raise RuntimeError("Monument detail extraction produced no interactables")
    if not totals["monument_puzzles"]:
        raise RuntimeError("Monument detail extraction produced no puzzle routes")
    if not totals["monument_loot_groups"]:
        raise RuntimeError("Monument detail extraction produced no loot groups")
    if not totals["monument_radiation_zones"]:
        raise RuntimeError("Monument detail extraction produced no radiation zones")
    if not totals["monument_diesel_groups"]:
        raise RuntimeError("Monument detail extraction produced no Diesel Fuel groups")
    return totals


def _validate_staging(staging: Path, install: Path,
                      details_install: Path) -> dict:
    paths = {name: staging / name for name in JSON_RESOURCES}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    tile_metadata_path = staging / "tunnel_tiles" / "tiles.json"
    cargo_collision_metadata_path = staging / "cargo_collision_tiles" / "tiles.json"
    if not tile_metadata_path.is_file():
        missing.append(str(tile_metadata_path))
    if not cargo_collision_metadata_path.is_file():
        missing.append(str(cargo_collision_metadata_path))
    if missing:
        raise RuntimeError("Refresh did not produce: " + ", ".join(missing))

    manifest = _read_json(paths["prefab_manifest.json"])
    rules = _read_json(paths["spawn_rules.json"])
    monuments = _read_json(paths["monument_metadata.json"])
    no_build = _read_json(paths["no_build_zones.json"])
    cargo = _read_json(paths["cargo_harbor_paths.json"])
    tunnels = _read_json(tile_metadata_path)
    cargo_collisions = _read_json(cargo_collision_metadata_path)
    identity = bundle_identity(install)
    details_identity = bundle_identity(details_install)
    bundles = identity["bundles"]

    _assert_bundle({"size": manifest["source_size"],
                    "mtime_ns": manifest["source_mtime_ns"]}, bundles["content"], "manifest/content")
    _assert_bundle(rules["sources"]["content_bundle"], bundles["content"], "rules/content")
    _assert_bundle(rules["sources"]["maps_bundle"], bundles["maps"], "rules/maps")
    for name in ("content", "asset_scenes", "maps"):
        _assert_bundle(monuments["source"]["bundles"][name], bundles[name], f"monuments/{name}")
        _assert_bundle(cargo["source"]["bundles"][name], bundles[name], f"cargo/{name}")
        _assert_bundle(tunnels["identity"]["bundles"][name], bundles[name], f"tunnels/{name}")
        _assert_bundle(cargo_collisions["source"]["bundles"][name], bundles[name], f"cargo-collisions/{name}")
        _assert_bundle(
            monuments["details_source"]["bundles"][name],
            details_identity["bundles"][name], f"monument-details/{name}",
        )
    _assert_bundle({"size": no_build["source"]["content_bundle_size"],
                    "mtime_ns": no_build["source"]["content_bundle_mtime_ns"]},
                   bundles["content"], "no-build/content")

    build_ids = {
        value for value in (
            identity.get("rust_build_id"),
            monuments.get("source", {}).get("rust_build_id"),
            cargo.get("source", {}).get("rust_build_id"),
            no_build.get("source", {}).get("rust_build_id"),
            tunnels.get("identity", {}).get("rust_build_id"),
            cargo_collisions.get("source", {}).get("rust_build_id"),
        ) if value is not None
    }
    if len(build_ids) > 1:
        raise RuntimeError(f"Packaged resources contain mixed Rust build IDs: {sorted(build_ids)}")

    if int(monuments.get("schema_version", -1)) != MONUMENT_SCHEMA_VERSION:
        raise RuntimeError(
            "Monument schema does not match the current extractor: "
            f"expected {MONUMENT_SCHEMA_VERSION}, got "
            f"{monuments.get('schema_version')!r}"
        )
    expected_details_build = details_identity.get("rust_build_id")
    actual_details_build = monuments.get("details_source", {}).get("rust_build_id")
    if (expected_details_build is not None and
            actual_details_build != expected_details_build):
        raise RuntimeError(
            "Monument details build ID does not match the selected details install"
        )
    detail_summary = _monument_detail_summary(monuments)

    tile_files = sorted((staging / "tunnel_tiles").glob("*.png"))
    if len(tile_files) != int(tunnels["template_count"]):
        raise RuntimeError("Tunnel PNG count does not match tiles.json")
    cargo_collision_files = sorted((staging / "cargo_collision_tiles").glob("*.png"))
    if len(cargo_collision_files) != int(cargo_collisions["template_count"]):
        raise RuntimeError("Cargo collision PNG count does not match tiles.json")
    leaked_paths = {str(install).casefold(), str(details_install).casefold()}
    for path in list(paths.values()) + [tile_metadata_path, cargo_collision_metadata_path]:
        text = path.read_text(encoding="utf-8").casefold()
        if any(leaked_path in text for leaked_path in leaked_paths):
            raise RuntimeError(f"Machine-specific Rust path leaked into {path.name}")

    return {
        "rust_build_id": next(iter(build_ids), None),
        "prefab_entries": int(manifest["entry_count"]),
        "spawn_rules": int(rules["rule_count"]),
        "monument_prefabs": int(monuments["prefab_count"]),
        "no_build_prefabs": int(no_build["prefab_count"]),
        "no_build_zones": int(no_build["zone_definition_count"]),
        "cargo_harbor_paths": int(cargo["prefab_count"]),
        "cargo_collision_tiles": int(cargo_collisions["template_count"]),
        "tunnel_tiles": int(tunnels["template_count"]),
        **detail_summary,
    }


def _find_details_install(default: Path) -> Path:
    explicit = MONUMENT_DETAILS_INSTALL_PATH
    environment = os.environ.get("RUST_MONUMENT_DETAILS_INSTALL_PATH")
    if explicit is None and not environment:
        return default
    candidate = find_rust_install(explicit or environment)
    if candidate is None:
        raise FileNotFoundError(
            "Monument-details Rust installation is invalid. Set "
            "MONUMENT_DETAILS_INSTALL_PATH or "
            "RUST_MONUMENT_DETAILS_INSTALL_PATH to a Rust client or dedicated "
            "server directory containing Bundles."
        )
    return candidate


def _replace_packaged_data(staging: Path) -> None:
    data_dir = SRC_DIR / "rustmap_parser" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in JSON_RESOURCES:
        destination = data_dir / name
        temporary = data_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        shutil.copy2(staging / name, temporary)
        os.replace(temporary, destination)

    destination = data_dir / "tunnel_tiles"
    refreshing = data_dir / f".tunnel_tiles.{uuid.uuid4().hex}.refreshing"
    backup = data_dir / f".tunnel_tiles.{uuid.uuid4().hex}.backup"
    shutil.copytree(staging / "tunnel_tiles", refreshing)
    initializer = destination / "__init__.py"
    if initializer.is_file():
        shutil.copy2(initializer, refreshing / "__init__.py")
    try:
        if destination.exists():
            destination.rename(backup)
        refreshing.rename(destination)
    except BaseException:
        if not destination.exists() and backup.exists():
            backup.rename(destination)
        raise
    finally:
        if refreshing.exists():
            shutil.rmtree(refreshing)
        if backup.exists():
            shutil.rmtree(backup)

    cargo_destination = data_dir / "cargo_collision_tiles"
    cargo_refreshing = data_dir / f".cargo_collision_tiles.{uuid.uuid4().hex}.refreshing"
    cargo_backup = data_dir / f".cargo_collision_tiles.{uuid.uuid4().hex}.backup"
    shutil.copytree(staging / "cargo_collision_tiles", cargo_refreshing)
    initializer = cargo_destination / "__init__.py"
    if initializer.is_file():
        shutil.copy2(initializer, cargo_refreshing / "__init__.py")
    try:
        if cargo_destination.exists():
            cargo_destination.rename(cargo_backup)
        cargo_refreshing.rename(cargo_destination)
    except BaseException:
        if not cargo_destination.exists() and cargo_backup.exists():
            cargo_backup.rename(cargo_destination)
        raise
    finally:
        if cargo_refreshing.exists():
            shutil.rmtree(cargo_refreshing)
        if cargo_backup.exists():
            shutil.rmtree(cargo_backup)


def main() -> None:
    install = find_rust_install(RUST_INSTALL_PATH)
    if install is None:
        raise FileNotFoundError(
            "Rust installation not found. Set RUST_INSTALL_PATH near the top of "
            "refresh_all_data.py or define the RUST_INSTALL_PATH environment variable."
        )
    print(f"Rust install: {install}")
    details_install = _find_details_install(install)
    print(f"Monument details install: {details_install}")
    stages = ("prefab_manifest", "spawn_rules", "monument_metadata",
              "monument_details",
              "no_build_zones", "cargo_harbor_paths", "cargo_collision_tiles",
              "tunnel_tiles")
    timings = {}
    with tempfile.TemporaryDirectory(prefix="rustmap_parser-data-refresh-") as temporary:
        staging = Path(temporary)
        for stage in stages:
            print(f"Refreshing {stage.replace('_', ' ')}...")
            stage_install = details_install if stage == "monument_details" else install
            timings[stage] = _run_stage(
                stage, stage_install, staging, TUNNEL_CACHE_PATH
            )
            print(f"  completed in {timings[stage]:.2f}s")
        print("Validating staged resources...")
        summary = _validate_staging(staging, install, details_install)
        _replace_packaged_data(staging)

    print("\nAll packaged Rust data updated successfully")
    print(f"  Rust build:       {summary['rust_build_id']}")
    print(f"  Prefab entries:   {summary['prefab_entries']}")
    print(f"  Spawn rules:      {summary['spawn_rules']}")
    print(f"  Monument prefabs: {summary['monument_prefabs']}")
    print(f"  Interactables:    {summary['monument_interactables']}")
    print(f"  Puzzle routes:    {summary['monument_puzzles']}")
    print(f"  Loot groups:      {summary['monument_loot_groups']}")
    print(f"  Diesel groups:    {summary['monument_diesel_groups']}")
    print(f"  Radiation zones:  {summary['monument_radiation_zones']}")
    print(f"  No-build prefabs: {summary['no_build_prefabs']}")
    print(f"  No-build zones:   {summary['no_build_zones']}")
    print(f"  Cargo harbors:    {summary['cargo_harbor_paths']}")
    print(f"  Cargo collisions: {summary['cargo_collision_tiles']}")
    print(f"  Tunnel tiles:     {summary['tunnel_tiles']}")
    print(f"  Total time:       {sum(timings.values()):.2f}s")


if __name__ == "__main__":
    mp.freeze_support()
    main()
