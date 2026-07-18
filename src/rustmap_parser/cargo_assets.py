"""Maintainer extraction of sanitized cargo harbor approach paths."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .no_build_assets import _root_game_object
from .tunnel_assets import _component, _world_matrix, bundle_identity
from .png import save_png


SCHEMA_VERSION = 1
HARBOR_PREFIX = "assets/bundled/prefabs/autospawn/monument/harbor/"
ICEBERG_PREFIX = "assets/bundled/prefabs/autospawn/decor/iceberg/"
COASTAL_LARGE_PREFIXES = (
    "assets/bundled/prefabs/autospawn/decor/coastal_rocks_large/",
    "assets/bundled/prefabs/autospawn/decor/coastal_rocks_large_arctic/",
)
COLLISION_SCHEMA_VERSION = 1
COLLISION_PIXELS_PER_METRE = 8.0
SPHERE_RADIUS_METRES = 3.0
CARGO_PHYSICS_MASK = 1084293377


def extract_cargo_harbor_paths(rust_install_path: str | Path) -> dict:
    """Extract root-relative BasePath nodes used by CargoNotifier harbor routes."""
    import UnityPy

    install = Path(rust_install_path)
    environment = UnityPy.load(
        str(install / "Bundles" / "shared" / "assetscenes.bundle"),
        str(install / "Bundles" / "shared" / "content.bundle"),
    )
    definitions: dict[str, dict] = {}
    scene_count = 0
    for root in environment.files.values():
        for name, asset_file in (getattr(root, "files", None) or {}).items():
            if not (name.startswith("BuildPlayer-AssetScene-monument.") and
                    not name.endswith("sharedAssets")):
                continue
            scene_count += 1
            transform_memo: dict[int, np.ndarray] = {}
            for obj in asset_file.objects.values():
                if obj.type.name != "MonoBehaviour":
                    continue
                try:
                    value = obj.read()
                    if value.m_Script.read().m_ClassName != "BasePath":
                        continue
                    tree = obj.read_typetree()
                    game_object = asset_file.objects[tree["m_GameObject"]["m_PathID"]].read()
                    if game_object.m_Name.casefold() != "cargoship":
                        continue
                    prefab_root = _root_game_object(game_object)
                    path = prefab_root.m_Name.casefold().replace("\\", "/")
                    if not path.startswith(HARBOR_PREFIX):
                        continue
                    root_matrix = _world_matrix(
                        _component(prefab_root, "Transform"), transform_memo
                    )
                    inverse_root = np.linalg.inv(root_matrix)
                    nodes = []
                    for node_pointer in tree.get("nodes", []):
                        node_obj = asset_file.objects[node_pointer["m_PathID"]]
                        node_tree = node_obj.read_typetree()
                        node_game_object = asset_file.objects[
                            node_tree["m_GameObject"]["m_PathID"]
                        ].read()
                        local = inverse_root @ _world_matrix(
                            _component(node_game_object, "Transform"), transform_memo
                        )
                        position = local[:3, 3]
                        nodes.append({
                            "name": str(node_game_object.m_Name),
                            "position": {
                                "x": float(position[0]), "y": float(position[1]),
                                "z": float(position[2]),
                            },
                            "max_velocity_on_approach": float(
                                node_tree.get("maxVelocityOnApproach", 0.0)
                            ),
                        })
                    if nodes:
                        definitions[path] = {
                            "prefab_path": path,
                            "nodes": nodes,
                        }
                except Exception:
                    continue

    identity = bundle_identity(install)
    for bundle in identity.get("bundles", {}).values():
        bundle.pop("path", None)
    prefabs = [definitions[path] for path in sorted(definitions)]
    return {
        "schema_version": SCHEMA_VERSION,
        "source": identity,
        "extraction": {
            "method": "CargoShip-named BasePath and BasePathNode transform scan",
            "asset_scene_count": scene_count,
            "coordinate_space": "prefab-root-relative Unity X/Y/Z metres",
        },
        "prefab_count": len(prefabs),
        "prefabs": prefabs,
    }


def refresh_cargo_harbor_paths(rust_install_path: str | Path,
                               output_path: str | Path | None = None) -> Path:
    target = (
        Path(output_path) if output_path is not None
        else Path(__file__).with_name("data") / "cargo_harbor_paths.json"
    )
    payload = extract_cargo_harbor_paths(rust_install_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target


def _sanitize_identity(identity: dict) -> dict:
    result = json.loads(json.dumps(identity))
    for bundle in result.get("bundles", {}).values():
        bundle.pop("path", None)
    return result


def _rasterize_prefab_collision(root_game_object, pixels_per_metre: float) -> tuple[Image.Image, dict] | None:
    """Rasterize server LOD0 geometry intersecting the cargo sphere's Y slab."""
    from UnityPy.helpers.MeshHelper import MeshHandler

    root_transform = _component(root_game_object, "Transform")
    transform_memo: dict[int, np.ndarray] = {}
    root_inverse = np.linalg.inv(_world_matrix(root_transform, transform_memo))
    vertices_parts: list[np.ndarray] = []
    triangles_parts: list[np.ndarray] = []
    offset = 0
    stack = [root_transform]
    mesh_count = 0
    seen_meshes: set[tuple[int, bytes]] = set()
    while stack:
        transform = stack.pop()
        stack.extend(child.read() for child in transform.m_Children)
        game_object = transform.m_GameObject.read()
        mesh_filter = _component(game_object, "MeshFilter")
        if mesh_filter is None or not mesh_filter.m_Mesh.path_id:
            continue
        # Server-side static colliders use the LOD0 mesh. Avoid auxiliary
        # height/shadow meshes and all visual-only lower LODs.
        name = str(game_object.m_Name).casefold()
        layer = int(game_object.m_Layer)
        has_collider = _component(game_object, "MeshCollider") is not None
        if not (CARGO_PHYSICS_MASK & (1 << layer)):
            continue
        if "lod0" not in name and not has_collider:
            continue
        mesh = mesh_filter.m_Mesh.read()
        matrix = root_inverse @ _world_matrix(transform, transform_memo)
        mesh_key = (
            int(mesh.object_reader.path_id),
            np.round(matrix, decimals=7).astype(np.float64).tobytes(),
        )
        if mesh_key in seen_meshes:
            continue
        seen_meshes.add(mesh_key)
        handler = MeshHandler(mesh)
        handler.process()
        if not handler.m_Vertices:
            continue
        vertices = np.asarray(handler.m_Vertices, dtype=np.float64)
        vertices = (
            matrix @ np.column_stack((vertices, np.ones(len(vertices)))).T
        ).T[:, :3]
        triangles = np.asarray(
            [triangle for submesh in handler.get_triangles() for triangle in submesh],
            dtype=np.int32,
        )
        if not len(triangles):
            continue
        triangle_vertices = vertices[triangles]
        keep = (
            np.min(triangle_vertices[:, :, 1], axis=1) <= SPHERE_RADIUS_METRES
        ) & (
            np.max(triangle_vertices[:, :, 1], axis=1) >= -SPHERE_RADIUS_METRES
        )
        triangles = triangles[keep]
        if not len(triangles):
            continue
        vertices_parts.append(vertices)
        triangles_parts.append(triangles + offset)
        offset += len(vertices)
        mesh_count += 1

    if not vertices_parts:
        return None
    vertices = np.concatenate(vertices_parts)
    triangles = np.concatenate(triangles_parts)
    used = vertices[np.unique(triangles)]
    padding = SPHERE_RADIUS_METRES + 1.0 / pixels_per_metre
    left = math.floor((float(used[:, 0].min()) - padding) * pixels_per_metre) / pixels_per_metre
    right = math.ceil((float(used[:, 0].max()) + padding) * pixels_per_metre) / pixels_per_metre
    bottom = math.floor((float(used[:, 2].min()) - padding) * pixels_per_metre) / pixels_per_metre
    top = math.ceil((float(used[:, 2].max()) + padding) * pixels_per_metre) / pixels_per_metre
    width = int(round((right - left) * pixels_per_metre)) + 1
    height = int(round((top - bottom) * pixels_per_metre)) + 1
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)
    projected = np.column_stack((
        (vertices[:, 0] - left) * pixels_per_metre,
        (top - vertices[:, 2]) * pixels_per_metre,
    ))
    for triangle in triangles:
        points = projected[triangle]
        draw.polygon(tuple(map(tuple, points)), fill=255)
    dilation_pixels = int(round(SPHERE_RADIUS_METRES * pixels_per_metre))
    image = image.filter(ImageFilter.MaxFilter(dilation_pixels * 2 + 1))
    return image, {
        "pixels_per_metre": pixels_per_metre,
        "left_x_m": left,
        "top_z_m": top,
        "width": width,
        "height": height,
        "mesh_count": mesh_count,
        "triangle_count": int(len(triangles)),
    }


def extract_cargo_collision_tiles(rust_install_path: str | Path,
                                  output_directory: str | Path) -> Path:
    """Build sanitized PNG collision footprints used by the startup cargo route."""
    import UnityPy

    install = Path(rust_install_path)
    output = Path(output_directory)
    content = install / "Bundles" / "shared" / "content.bundle"
    asset_scenes = install / "Bundles" / "shared" / "assetscenes.bundle"
    environment = UnityPy.load(str(asset_scenes), str(content))
    world_file = None
    for root in environment.files.values():
        files = getattr(root, "files", None) or {}
        if "BuildPlayer-AssetScene-world" in files:
            world_file = files["BuildPlayer-AssetScene-world"]
            break
    if world_file is None:
        raise RuntimeError("AssetScene-world was not found in assetscenes.bundle")

    roots = []
    selected_prefixes = (ICEBERG_PREFIX, *COASTAL_LARGE_PREFIXES)
    for obj in world_file.objects.values():
        if obj.type.name != "GameObject":
            continue
        game_object = obj.read()
        path = str(game_object.m_Name).casefold().replace("\\", "/")
        if path.startswith(selected_prefixes):
            roots.append((path, game_object))
    roots.sort(key=lambda item: item[0])

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "__init__.py").write_text(
        '"""Sanitized cargo-route collision footprint resources."""\n',
        encoding="utf-8", newline="\n",
    )
    templates = []
    for index, (path, game_object) in enumerate(roots):
        rendered = _rasterize_prefab_collision(
            game_object, COLLISION_PIXELS_PER_METRE
        )
        if rendered is None:
            continue
        image, values = rendered
        filename = f"collision_{index:03d}__{Path(path).stem}.png"
        save_png(image, output / filename)
        templates.append({
            "prefab_path": path, "mask_file": filename, **values,
        })

    metadata = {
        "schema_version": COLLISION_SCHEMA_VERSION,
        "source": _sanitize_identity(bundle_identity(install)),
        "selection": {
            "world_setup_stage": True,
            "prefab_prefixes": [ICEBERG_PREFIX, *COASTAL_LARGE_PREFIXES],
            "geometry": (
                "AssetScene-world LOD0 collider-source triangles intersecting "
                "Y=-3..3m"
            ),
        },
        "sphere_radius_preexpanded_m": SPHERE_RADIUS_METRES,
        "template_count": len(templates),
        "templates": templates,
    }
    (output / "tiles.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return output


def refresh_cargo_collision_tiles(rust_install_path: str | Path,
                                  output_directory: str | Path | None = None) -> Path:
    target = (
        Path(output_directory) if output_directory is not None
        else Path(__file__).with_name("data") / "cargo_collision_tiles"
    )
    return extract_cargo_collision_tiles(rust_install_path, target)
