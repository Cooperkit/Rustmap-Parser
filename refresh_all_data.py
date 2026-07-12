"""Refresh every versioned ``src/rustmap/data`` resource from one Rust install.

Run this file directly from a source checkout. No command-line arguments are
used. Set RUST_INSTALL_PATH below, set the RUST_INSTALL_PATH environment
variable, or leave it as None to search common Steam locations.
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

from rustmap.tunnel_assets import bundle_identity, find_rust_install


# --- Maintainer configuration ---------------------------------------------
RUST_INSTALL_PATH: Path | None = None
TUNNEL_CACHE_PATH: Path | None = PROJECT_DIR / ".rustmap-cache" / "tunnel-geometry"
# ---------------------------------------------------------------------------


JSON_RESOURCES = (
    "prefab_manifest.json",
    "spawn_rules.json",
    "monument_metadata.json",
    "no_build_zones.json",
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
            from rustmap import refresh_prefab_manifest
            target = staging / "prefab_manifest.json"
            refresh_prefab_manifest(install, target)
            _sanitize_manifest(target)
        elif stage == "spawn_rules":
            from rustmap import refresh_spawn_rules
            target = staging / "spawn_rules.json"
            refresh_spawn_rules(install, target)
            _sanitize_spawn_rules(target)
        elif stage == "monument_metadata":
            from rustmap import refresh_monument_metadata
            refresh_monument_metadata(install, staging / "monument_metadata.json")
        elif stage == "no_build_zones":
            from rustmap import refresh_no_build_zone_data
            refresh_no_build_zone_data(install, staging / "no_build_zones.json")
        elif stage == "tunnel_tiles":
            from rustmap import install_packaged_tunnel_templates, refresh_tunnel_templates
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


def _validate_staging(staging: Path, install: Path) -> dict:
    paths = {name: staging / name for name in JSON_RESOURCES}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    tile_metadata_path = staging / "tunnel_tiles" / "tiles.json"
    if not tile_metadata_path.is_file():
        missing.append(str(tile_metadata_path))
    if missing:
        raise RuntimeError("Refresh did not produce: " + ", ".join(missing))

    manifest = _read_json(paths["prefab_manifest.json"])
    rules = _read_json(paths["spawn_rules.json"])
    monuments = _read_json(paths["monument_metadata.json"])
    no_build = _read_json(paths["no_build_zones.json"])
    tunnels = _read_json(tile_metadata_path)
    identity = bundle_identity(install)
    bundles = identity["bundles"]

    _assert_bundle({"size": manifest["source_size"],
                    "mtime_ns": manifest["source_mtime_ns"]}, bundles["content"], "manifest/content")
    _assert_bundle(rules["sources"]["content_bundle"], bundles["content"], "rules/content")
    _assert_bundle(rules["sources"]["maps_bundle"], bundles["maps"], "rules/maps")
    for name in ("content", "asset_scenes", "maps"):
        _assert_bundle(monuments["source"]["bundles"][name], bundles[name], f"monuments/{name}")
        _assert_bundle(tunnels["identity"]["bundles"][name], bundles[name], f"tunnels/{name}")
    _assert_bundle({"size": no_build["source"]["content_bundle_size"],
                    "mtime_ns": no_build["source"]["content_bundle_mtime_ns"]},
                   bundles["content"], "no-build/content")

    build_ids = {
        value for value in (
            identity.get("rust_build_id"),
            monuments.get("source", {}).get("rust_build_id"),
            no_build.get("source", {}).get("rust_build_id"),
            tunnels.get("identity", {}).get("rust_build_id"),
        ) if value is not None
    }
    if len(build_ids) > 1:
        raise RuntimeError(f"Packaged resources contain mixed Rust build IDs: {sorted(build_ids)}")

    tile_files = sorted((staging / "tunnel_tiles").glob("*.png"))
    if len(tile_files) != int(tunnels["template_count"]):
        raise RuntimeError("Tunnel PNG count does not match tiles.json")
    leaked_path = str(install).casefold()
    for path in list(paths.values()) + [tile_metadata_path]:
        if leaked_path in path.read_text(encoding="utf-8").casefold():
            raise RuntimeError(f"Machine-specific Rust path leaked into {path.name}")

    return {
        "rust_build_id": next(iter(build_ids), None),
        "prefab_entries": int(manifest["entry_count"]),
        "spawn_rules": int(rules["rule_count"]),
        "monument_prefabs": int(monuments["prefab_count"]),
        "no_build_prefabs": int(no_build["prefab_count"]),
        "no_build_zones": int(no_build["zone_definition_count"]),
        "tunnel_tiles": int(tunnels["template_count"]),
    }


def _replace_packaged_data(staging: Path) -> None:
    data_dir = SRC_DIR / "rustmap" / "data"
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


def main() -> None:
    install = find_rust_install(RUST_INSTALL_PATH)
    if install is None:
        raise FileNotFoundError(
            "Rust installation not found. Set RUST_INSTALL_PATH near the top of "
            "refresh_all_data.py or define the RUST_INSTALL_PATH environment variable."
        )
    print(f"Rust install: {install}")
    stages = ("prefab_manifest", "spawn_rules", "monument_metadata",
              "no_build_zones", "tunnel_tiles")
    timings = {}
    with tempfile.TemporaryDirectory(prefix="rustmap-data-refresh-") as temporary:
        staging = Path(temporary)
        for stage in stages:
            print(f"Refreshing {stage.replace('_', ' ')}...")
            timings[stage] = _run_stage(stage, install, staging, TUNNEL_CACHE_PATH)
            print(f"  completed in {timings[stage]:.2f}s")
        print("Validating staged resources...")
        summary = _validate_staging(staging, install)
        _replace_packaged_data(staging)

    print("\nAll packaged Rust data updated successfully")
    print(f"  Rust build:       {summary['rust_build_id']}")
    print(f"  Prefab entries:   {summary['prefab_entries']}")
    print(f"  Spawn rules:      {summary['spawn_rules']}")
    print(f"  Monument prefabs: {summary['monument_prefabs']}")
    print(f"  No-build prefabs: {summary['no_build_prefabs']}")
    print(f"  No-build zones:   {summary['no_build_zones']}")
    print(f"  Tunnel tiles:     {summary['tunnel_tiles']}")
    print(f"  Total time:       {sum(timings.values()):.2f}s")


if __name__ == "__main__":
    mp.freeze_support()
    main()
