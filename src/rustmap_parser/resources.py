"""APIs for refreshing version-specific Rust data resources."""

from __future__ import annotations

from pathlib import Path

from .populations import extract_spawn_rules, save_rule_database
from .prefabs import extract_game_manifest, find_content_bundle


def refresh_prefab_manifest(rust_install: str | Path, output_path: str | Path):
    """Extract and save the installed Rust GameManifest."""
    manifest = extract_game_manifest(find_content_bundle(rust_install))
    manifest.save(output_path)
    return manifest


def refresh_spawn_rules(rust_install: str | Path, output_path: str | Path) -> dict:
    """Extract and save population rules from an installed Rust build."""
    root = Path(rust_install)
    database = extract_spawn_rules(
        root / "Bundles" / "shared" / "content.bundle",
        root / "Bundles" / "maps" / "maps.bundle",
    )
    save_rule_database(database, output_path)
    return database
