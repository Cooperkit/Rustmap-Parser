"""Extract and cache Rust's final-LOD train-tunnel map geometry."""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing as mp
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .png import save_png


CACHE_SCHEMA_VERSION = 5
TEMPLATE_PIXELS_PER_METER = 8.0
ASSET_SCENE_FILE = "BuildPlayer-AssetScene-monument.25"
TUNNEL_PREFIX = "assets/bundled/prefabs/autospawn/tunnel"


def default_tunnel_cache() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "rustmap" / "tunnel-geometry"


def find_rust_install(explicit: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit is not None:
        candidate = Path(explicit)
        return candidate.resolve() if (
            candidate / "Bundles" / "shared" / "content.bundle"
        ).is_file() else None
    if os.environ.get("RUST_INSTALL_PATH"):
        candidates.append(Path(os.environ["RUST_INSTALL_PATH"]))
    if sys.platform == "win32":
        for drive in "CDEF":
            candidates.append(Path(f"{drive}:\\SteamLibrary\\steamapps\\common\\Rust"))
        candidates.append(Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) /
                          "Steam" / "steamapps" / "common" / "Rust")
    for candidate in candidates:
        if (candidate / "Bundles" / "shared" / "content.bundle").is_file():
            return candidate.resolve()
    return None


def _bundle_paths(install: Path) -> dict[str, Path]:
    result = {
        "content": install / "Bundles" / "shared" / "content.bundle",
        "asset_scenes": install / "Bundles" / "shared" / "assetscenes.bundle",
        "maps": install / "Bundles" / "maps" / "maps.bundle",
    }
    missing = [str(path) for path in result.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing Rust bundle(s): " + ", ".join(missing))
    return result


def _build_id(install: Path) -> str | None:
    manifest = install.parent.parent / "appmanifest_252490.acf"
    if not manifest.is_file():
        return None
    match = re.search(r'"buildid"\s+"([^"]+)"', manifest.read_text(encoding="utf-8", errors="ignore"))
    return match.group(1) if match else None


def bundle_identity(install: Path) -> dict:
    bundles = _bundle_paths(install)
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "rust_build_id": _build_id(install),
        "bundles": {
            name: {"path": str(path), "size": path.stat().st_size,
                   "mtime_ns": path.stat().st_mtime_ns}
            for name, path in bundles.items()
        },
    }


def _cache_key(identity: dict) -> str:
    stable = {
        "schema_version": identity["schema_version"],
        "rust_build_id": identity["rust_build_id"],
        "bundles": {name: {"size": value["size"], "mtime_ns": value["mtime_ns"]}
                    for name, value in identity["bundles"].items()},
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:20]


def _vec(value, default: float = 0.0) -> np.ndarray:
    return np.array([getattr(value, "x", default), getattr(value, "y", default),
                     getattr(value, "z", default)], dtype=np.float64)


def _local_matrix(transform) -> np.ndarray:
    p = _vec(transform.m_LocalPosition)
    s = _vec(transform.m_LocalScale, 1.0)
    q = transform.m_LocalRotation
    x, y, z, w = float(q.x), float(q.y), float(q.z), float(q.w)
    rotation = np.array([
        [1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
        [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
        [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)],
    ], dtype=np.float64)
    matrix = np.eye(4, dtype=np.float64)
    matrix[:3, :3] = rotation @ np.diag(s)
    matrix[:3, 3] = p
    return matrix


def _world_matrix(transform, memo: dict[int, np.ndarray]) -> np.ndarray:
    path_id = transform.object_reader.path_id
    if path_id in memo:
        return memo[path_id]
    local = _local_matrix(transform)
    parent = transform.m_Father
    result = _world_matrix(parent.read(), memo) @ local if parent.path_id else local
    memo[path_id] = result
    return result


def _component(game_object, type_name: str):
    for item in game_object.m_Component:
        if item.component.type.name == type_name:
            return item.component.read()
    return None


def _rasterize_template(vertices: np.ndarray, triangles: np.ndarray) -> tuple[np.ndarray, dict]:
    """Rasterize prefab-local X/Z triangles with stable world-grid pixel phase."""
    started = time.perf_counter()
    ppm = TEMPLATE_PIXELS_PER_METER
    min_x, max_x = float(vertices[:, 0].min()), float(vertices[:, 0].max())
    min_z, max_z = float(vertices[:, 2].min()), float(vertices[:, 2].max())
    # Align bounds to the global 2 px/m lattice and retain one transparent pixel
    # around the silhouette so Lanczos downsampling cannot clip edge coverage.
    left_x = math.floor(min_x * ppm) / ppm - 1.0 / ppm
    right_x = math.ceil(max_x * ppm) / ppm + 1.0 / ppm
    bottom_z = math.floor(min_z * ppm) / ppm - 1.0 / ppm
    top_z = math.ceil(max_z * ppm) / ppm + 1.0 / ppm
    width = max(1, int(round((right_x - left_x) * ppm)) + 1)
    height = max(1, int(round((top_z - bottom_z) * ppm)) + 1)
    px = (vertices[:, 0] - left_x) * ppm
    py = (top_z - vertices[:, 2]) * ppm
    projected = np.round(np.column_stack((px, py)), 6)
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    drawn = 0
    for triangle in triangles:
        points = projected[triangle]
        first, second = points[1] - points[0], points[2] - points[0]
        area = abs(first[0] * second[1] - first[1] * second[0])
        if area <= 0.05:
            continue
        draw.polygon(tuple(map(tuple, points)), fill=255)
        drawn += 1
    return np.asarray(image, dtype=np.uint8), {
        "mask_pixels_per_meter": ppm,
        "mask_left_x": left_x,
        "mask_top_z": top_z,
        "mask_shape": [height, width],
        "mask_triangle_count": drawn,
        "mask_render_seconds": time.perf_counter() - started,
    }


def _class_type_index(asset_file, class_name: str) -> int:
    representatives = {}
    for obj in asset_file.objects.values():
        if obj.type.name == "MonoBehaviour":
            representatives.setdefault(id(obj.serialized_type), obj)
    for obj in representatives.values():
        try:
            if obj.read().m_Script.read().m_ClassName == class_name:
                return obj.serialized_type.script_type_index
        except Exception:
            continue
    raise RuntimeError(f"{class_name} MonoBehaviour type was not found")


def _extract_worker(asset_scenes: str, content: str, target: str, identity: dict, conn) -> None:
    try:
        import UnityPy
        from UnityPy.helpers.MeshHelper import MeshHandler

        started = time.perf_counter()
        environment = UnityPy.load(asset_scenes, content)
        scene = None
        for root in environment.files.values():
            files = getattr(root, "files", None) or {}
            if ASSET_SCENE_FILE in files:
                scene = files[ASSET_SCENE_FILE]
                break
        if scene is None:
            raise RuntimeError(f"{ASSET_SCENE_FILE} was not found in assetscenes.bundle")

        type_index = _class_type_index(scene, "DungeonGridCell")
        arrays: dict[str, np.ndarray] = {}
        templates = []
        template_raster_seconds = 0.0
        transform_memo: dict[int, np.ndarray] = {}
        for component in scene.objects.values():
            if (component.type.name != "MonoBehaviour" or
                    component.serialized_type.script_type_index != type_index):
                continue
            tree = component.read_typetree()
            root_go = scene.objects[tree["m_GameObject"]["m_PathID"]].read()
            path = root_go.m_Name.casefold().replace("\\", "/")
            if not path.startswith(TUNNEL_PREFIX):
                continue
            root_transform = _component(root_go, "Transform")
            root_inverse = np.linalg.inv(_world_matrix(root_transform, transform_memo))
            vertices_parts, triangle_parts = [], []
            vertex_offset = 0
            mesh_count = 0
            for lod_ptr in tree.get("MapRendererLods", []):
                if not lod_ptr.get("m_PathID"):
                    continue
                lod_tree = scene.objects[lod_ptr["m_PathID"]].read_typetree()
                renderer = None
                for state in reversed(lod_tree.get("States", [])):
                    pointer = state.get("renderer", {})
                    if pointer.get("m_PathID"):
                        renderer = scene.objects[pointer["m_PathID"]].read()
                        break
                if renderer is None:
                    continue
                renderer_go = renderer.m_GameObject.read()
                mesh_filter = _component(renderer_go, "MeshFilter")
                renderer_transform = _component(renderer_go, "Transform")
                if mesh_filter is None or not mesh_filter.m_Mesh.path_id:
                    continue
                mesh = mesh_filter.m_Mesh.read()
                handler = MeshHandler(mesh)
                handler.process()
                if not handler.m_Vertices:
                    continue
                local_to_root = root_inverse @ _world_matrix(renderer_transform, transform_memo)
                vertices = np.asarray(handler.m_Vertices, dtype=np.float64)
                homogeneous = np.column_stack((vertices, np.ones(len(vertices))))
                vertices = (local_to_root @ homogeneous.T).T[:, :3].astype(np.float32)
                triangles = [triangle for submesh in handler.get_triangles() for triangle in submesh]
                if not triangles:
                    continue
                indices = np.asarray(triangles, dtype=np.int32) + vertex_offset
                vertices_parts.append(vertices)
                triangle_parts.append(indices)
                vertex_offset += len(vertices)
                mesh_count += 1
            if not vertices_parts:
                continue
            key = f"template_{len(templates):03d}"
            combined_vertices = np.concatenate(vertices_parts)
            combined_triangles = np.concatenate(triangle_parts)
            arrays[key + "_vertices"] = combined_vertices
            arrays[key + "_triangles"] = combined_triangles
            template_mask, mask_metadata = _rasterize_template(
                combined_vertices, combined_triangles
            )
            arrays[key + "_mask"] = template_mask
            template_raster_seconds += mask_metadata["mask_render_seconds"]
            templates.append({"key": key, "prefab_path": path, "mesh_count": mesh_count,
                              "vertex_count": len(combined_vertices),
                              "triangle_count": len(combined_triangles),
                              **mask_metadata})

        destination = Path(target)
        destination.mkdir(parents=True, exist_ok=True)
        templates_directory = destination / "templates"
        templates_directory.mkdir(parents=True, exist_ok=True)
        for template in templates:
            key = template["key"]
            filename = f"{key}__{Path(template['prefab_path']).stem}.png"
            save_png(
                Image.fromarray(arrays.pop(key + "_mask"), mode="L"),
                templates_directory / filename,
            )
            template["mask_file"] = f"templates/{filename}"
        np.savez_compressed(destination / "geometry.npz", **arrays)
        metadata = {"schema_version": CACHE_SCHEMA_VERSION, "identity": identity,
                    "template_count": len(templates), "templates": templates,
                    "template_pixels_per_meter": TEMPLATE_PIXELS_PER_METER,
                    "template_raster_seconds": template_raster_seconds,
                    "extraction_seconds": time.perf_counter() - started}
        (destination / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")
        conn.send({"ok": True, "template_count": len(templates)})
    except Exception as exc:
        conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        conn.close()


def refresh_tunnel_geometry(rust_install_path: str | Path,
                            cache_path: str | Path | None = None) -> Path:
    install = find_rust_install(rust_install_path)
    if install is None:
        raise FileNotFoundError(f"Rust installation not found: {rust_install_path}")
    identity = bundle_identity(install)
    cache_root = Path(cache_path) if cache_path is not None else default_tunnel_cache()
    destination = cache_root / _cache_key(identity)
    bundles = _bundle_paths(install)
    parent, child = mp.get_context("spawn").Pipe(False)
    process = mp.get_context("spawn").Process(
        target=_extract_worker,
        args=(str(bundles["asset_scenes"]), str(bundles["content"]),
              str(destination), identity, child),
    )
    process.start()
    result = parent.recv()
    process.join()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Tunnel geometry extraction failed"))
    return destination


def load_or_refresh_tunnel_geometry(rust_install_path: str | Path,
                                    cache_path: str | Path | None = None) -> Path:
    install = find_rust_install(rust_install_path)
    if install is None:
        raise FileNotFoundError("A matching local Rust client installation was not found")
    identity = bundle_identity(install)
    cache_root = Path(cache_path) if cache_path is not None else default_tunnel_cache()
    destination = cache_root / _cache_key(identity)
    metadata_path, geometry_path = destination / "metadata.json", destination / "geometry.npz"
    if metadata_path.is_file() and geometry_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("identity") == identity and metadata.get("template_count", 0) > 0:
                return destination
        except (OSError, ValueError):
            pass
    return refresh_tunnel_geometry(install, cache_root)


def refresh_tunnel_templates(rust_install_path: str | Path,
                             cache_path: str | Path | None = None) -> Path:
    """Build named PNG tunnel pieces plus fallback geometry in a versioned cache."""
    return refresh_tunnel_geometry(rust_install_path, cache_path)


def install_packaged_tunnel_templates(cache_path: str | Path,
                                      destination: str | Path) -> Path:
    """Copy PNG pieces from a local cache and write sanitized package metadata."""
    cache = Path(cache_path)
    target = Path(destination)
    metadata = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    expected = set()
    templates = []
    for item in metadata["templates"]:
        filename = Path(item["mask_file"]).name
        source = cache / item["mask_file"]
        if not source.is_file():
            raise FileNotFoundError(f"Missing generated tunnel tile: {source}")
        shutil.copy2(source, target / filename)
        expected.add(filename)
        templates.append({
            "key": item["key"], "prefab_path": item["prefab_path"],
            "mask_file": filename,
            "mask_pixels_per_meter": item["mask_pixels_per_meter"],
            "mask_left_x": item["mask_left_x"], "mask_top_z": item["mask_top_z"],
            "mask_shape": item["mask_shape"],
        })
    for old_tile in target.glob("*.png"):
        if old_tile.name not in expected:
            old_tile.unlink()
    identity = metadata.get("identity", {})
    sanitized_bundles = {
        name: {"size": int(value["size"]), "mtime_ns": int(value["mtime_ns"])}
        for name, value in identity.get("bundles", {}).items()
    }
    payload = {
        "schema_version": int(metadata["schema_version"]),
        "identity": {
            "schema_version": int(identity.get("schema_version", metadata["schema_version"])),
            "rust_build_id": identity.get("rust_build_id"),
            "bundles": sanitized_bundles,
        },
        "template_count": len(templates),
        "template_pixels_per_meter": metadata["template_pixels_per_meter"],
        "templates": templates,
    }
    (target / "tiles.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return target
