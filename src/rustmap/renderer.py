"""Python reconstruction of Rust dedicated server MapImageRenderer."""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .layers import int16_grid, splat_grid, topology_grid, world_height_grid
from .png import save_png

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(*args, **kwargs):
        def decorate(fn): return fn
        return decorate


COLORS = {
    "start": (0.28627452, 0.27058825, 0.24705884, 1.0),
    "water": (0.16941601, 0.31755757, 0.36200002, 1.0),
    "gravel": (0.25, 0.24342105, 0.22039475, 1.0),
    "dirt": (0.6, 0.47959462, 0.33, 1.0),
    "sand": (0.7, 0.65968585, 0.5277487, 1.0),
    "grass": (0.35486364, 0.37, 0.2035, 1.0),
    "forest": (0.24843751, 0.3, 0.0703125, 1.0),
    "rock": (0.4, 0.39379844, 0.37519377, 1.0),
    "snow": (0.86274517, 0.9294118, 0.94117653, 1.0),
    "pebble": (0.13725491, 0.2784314, 0.2761563, 1.0),
    "offshore": (0.04090196, 0.22060032, 0.27450982, 1.0),
}
SPLAT_ORDER = ((7, "gravel"), (6, "pebble"), (3, "rock"), (0, "dirt"),
               (4, "grass"), (5, "forest"), (2, "sand"), (1, "snow"))
SUN = np.asarray((0.95, 2.87, 2.37), dtype=np.float32)
SUN /= np.linalg.norm(SUN)


@njit(cache=True)
def _signed_distance(bitmap: np.ndarray) -> np.ndarray:
    size = bitmap.shape[0]
    padded = size + 2
    near_x = np.full((padded, padded), -1, np.int32)
    near_y = np.full((padded, padded), -1, np.int32)
    dist = np.full((padded, padded), np.inf, np.float32)
    # Literal port of DistanceField.Generate/GenerateJob boundary seeding.
    for y in range(1, size - 2):
        for x in range(1, size - 2):
            land = bitmap[y, x] > 127
            if land and ((bitmap[y, x-1] > 127) != land or
                         (bitmap[y, x+1] > 127) != land or
                         (bitmap[y-1, x] > 127) != land or
                         (bitmap[y+1, x] > 127) != land):
                py, px = y + 1, x + 1
                near_x[py, px] = x + 1
                near_y[py, px] = y + 1
                dist[py, px] = 0.0
    for y in range(1, padded - 1):
        for x in range(1, padded - 1):
            best = dist[y, x]
            for dy, dx, step in ((-1,-1,1.4142135),(-1,0,1.0),(-1,1,1.4142135),(0,-1,1.0)):
                yy, xx = y + dy, x + dx
                if dist[yy, xx] + step < best:
                    nx, ny = near_x[yy, xx], near_y[yy, xx]
                    near_x[y, x], near_y[y, x] = nx, ny
                    best = math.sqrt(float((x-nx)*(x-nx) + (y-ny)*(y-ny)))
                    dist[y, x] = best
    for y in range(padded - 2, 0, -1):
        for x in range(padded - 2, 0, -1):
            best = dist[y, x]
            for dy, dx, step in ((0,1,1.0),(1,-1,1.4142135),(1,0,1.0),(1,1,1.0)):
                yy, xx = y + dy, x + dx
                if dist[yy, xx] + step < best:
                    nx, ny = near_x[yy, xx], near_y[yy, xx]
                    near_x[y, x], near_y[y, x] = nx, ny
                    best = math.sqrt(float((x-nx)*(x-nx) + (y-ny)*(y-ny)))
                    dist[y, x] = best
    out = np.empty((size, size), np.float32)
    for y in range(size):
        for x in range(size):
            value = dist[y+1, x+1]
            out[y, x] = -value if bitmap[y, x] > 127 else value
    return out


@njit(cache=True)
def _gaussian_once(values: np.ndarray) -> np.ndarray:
    size = values.shape[0]
    offsets = (-6, -4, -2, 0, 2, 4, 6)
    weights = (0.03125, 0.109375, 0.21875, 0.28125, 0.21875, 0.109375, 0.03125)
    temp = np.empty_like(values)
    out = np.empty_like(values)
    for y in range(size):
        for x in range(size):
            total = 0.0
            for k in range(7):
                sx = min(max(x + offsets[k], 0), size - 1)
                total += values[y, sx] * weights[k]
            temp[y, x] = total
    for y in range(size):
        for x in range(size):
            total = 0.0
            for k in range(7):
                sy = min(max(y + offsets[k], 0), size - 1)
                total += temp[sy, x] * weights[k]
            out[y, x] = total
    return out


def _bilinear(grid: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    last_y, last_x = grid.shape[-2] - 1, grid.shape[-1] - 1
    px, py = xs * last_x, ys * last_y
    x0raw, y0raw = np.trunc(px).astype(np.int64), np.trunc(py).astype(np.int64)
    tx, ty = np.clip(px - x0raw, 0, 1).astype(np.float32), np.clip(py - y0raw, 0, 1).astype(np.float32)
    x0, y0 = np.clip(x0raw, 0, last_x), np.clip(y0raw, 0, last_y)
    x1, y1 = np.where(px < last_x, x0 + 1, x0), np.where(py < last_y, y0 + 1, y0)
    x1, y1 = np.clip(x1, 0, last_x), np.clip(y1, 0, last_y)
    a, b = grid[..., y0[:,None], x0[None,:]], grid[..., y0[:,None], x1[None,:]]
    c, d = grid[..., y1[:,None], x0[None,:]], grid[..., y1[:,None], x1[None,:]]
    top = a + (b-a) * tx[None,:]
    bottom = c + (d-c) * tx[None,:]
    return top + (bottom-top) * ty[:,None]


def _normal_vertices(normalized_height: np.ndarray, xs: np.ndarray, ys: np.ndarray, norm_y: float) -> np.ndarray:
    last = normalized_height.shape[0] - 1
    xm, xp = np.clip(xs-1,0,last), np.clip(xs+1,0,last)
    ym, yp = np.clip(ys-1,0,last), np.clip(ys+1,0,last)
    dx = (normalized_height[np.ix_(ys, xp)] - normalized_height[np.ix_(ys, xm)]) * np.float32(0.5)
    dz = (normalized_height[np.ix_(yp, xs)] - normalized_height[np.ix_(ym, xs)]) * np.float32(0.5)
    n = np.stack((-dx, np.full_like(dx, norm_y), -dz), axis=-1)
    return n / np.maximum(np.linalg.norm(n,axis=-1,keepdims=True), 1e-20)


def _slerp(a: np.ndarray, b: np.ndarray, t: np.ndarray) -> np.ndarray:
    t = np.clip(t, 0, 1)[...,None].astype(np.float32)
    dot = np.clip(np.sum(a*b,axis=-1,keepdims=True), -1, 1)
    theta = np.arccos(dot)
    sin_theta = np.sin(theta)
    linear = a + (b-a)*t
    result = np.where(np.abs(sin_theta)>1e-6,
                      np.sin((1-t)*theta)/np.maximum(sin_theta,1e-20)*a + np.sin(t*theta)/np.maximum(sin_theta,1e-20)*b,
                      linear)
    return result / np.maximum(np.linalg.norm(result,axis=-1,keepdims=True),1e-20)


def _sample_normals(normalized_height: np.ndarray, xs: np.ndarray, ys: np.ndarray,
                    world_size: int, vertex_normals: np.ndarray | None = None) -> np.ndarray:
    last = normalized_height.shape[0]-1
    px, py = xs*last, ys*last
    x0 = np.clip(np.trunc(px).astype(int),0,last); y0=np.clip(np.trunc(py).astype(int),0,last)
    x1=np.minimum(x0+1,last); y1=np.minimum(y0+1,last)
    tx=np.clip(px-x0,0,1).astype(np.float32); ty=np.clip(py-y0,0,1).astype(np.float32)
    if vertex_normals is None:
        norm_y=np.float32(world_size/1000.0/normalized_height.shape[0])
        n00=_normal_vertices(normalized_height,x0,y0,norm_y); n10=_normal_vertices(normalized_height,x1,y0,norm_y)
        n01=_normal_vertices(normalized_height,x0,y1,norm_y); n11=_normal_vertices(normalized_height,x1,y1,norm_y)
        top=_slerp(n00,n10,np.broadcast_to(tx,(len(ys),len(xs))))
        bottom=_slerp(n01,n11,np.broadcast_to(tx,(len(ys),len(xs))))
    else:
        # Upscaled renders map multiple output rows onto the same height-grid
        # row.  Horizontally interpolate each unique source row once, then
        # gather it for y0/y1.  Each retained interpolation uses the exact same
        # operands and operation order as the former per-output-row version.
        unique_y, inverse = np.unique(np.concatenate((y0, y1)), return_inverse=True)
        left=vertex_normals[unique_y[:,None],x0[None,:]]
        right=vertex_normals[unique_y[:,None],x1[None,:]]
        horizontal=_slerp(left,right,np.broadcast_to(tx,(len(unique_y),len(xs))))
        top=horizontal[inverse[:len(y0)]]
        bottom=horizontal[inverse[len(y0):]]
    return _slerp(top,bottom,np.broadcast_to(ty[:,None],(len(ys),len(xs))))


def _apply_ocean_level(water: np.ndarray, topology: np.ndarray,
                       ocean_level: float = 0.0) -> np.ndarray:
    ocean=(topology & np.uint32(384)) != 0
    return np.where((water < ocean_level) & ocean,
                    np.maximum(water, ocean_level), water)


def _shore_distance(world, shore_size: int = 2048) -> np.ndarray:
    coords=(np.arange(shore_size,dtype=np.float32)+0.5)/shore_size
    terrain=_bilinear(world_height_grid(world),coords,coords)
    water=_bilinear(world_height_grid(world,"water"),coords,coords)
    # WaterLevel.GetWaterLevels only raises serialized negative water heights to
    # the global ocean level where Ocean/Oceanside topology is present. Clamping
    # every sample to zero creates fake lakes in dry below-sea-level terrain
    # such as canyons and the Giant Excavator pit.
    topology=topology_grid(world)
    ix=np.clip(np.trunc(coords*topology.shape[1]).astype(np.int64),0,topology.shape[1]-1)
    iy=np.clip(np.trunc(coords*topology.shape[0]).astype(np.int64),0,topology.shape[0]-1)
    water=_apply_ocean_level(water,topology[np.ix_(iy,ix)])
    bitmap=np.where(np.maximum(water-terrain,0)<=0,255,0).astype(np.uint8)
    return _gaussian_once(_signed_distance(bitmap))


@dataclass
class MapRenderResult:
    image: Image.Image
    width: int
    height: int
    background: tuple[int,int,int]
    timings: dict[str,float]


@dataclass
class MapRenderInputs:
    splat: np.ndarray
    topology: np.ndarray
    normalized_height: np.ndarray
    heights: np.ndarray
    shore: np.ndarray
    vertex_normals: np.ndarray
    prepare_seconds: float


def save_full_map_tiles(image: Image.Image, output_dir: str | Path,
                        tile_size: int = 512) -> dict:
    """Split a full map image into bottom-left-indexed, padded RGBA PNG tiles."""
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    started = time.perf_counter()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    width, height = image.size
    columns = math.ceil(width / tile_size)
    rows = math.ceil(height / tile_size)
    specifications = []
    expected_files = set()
    for tile_y in range(rows):
        map_bottom = tile_y * tile_size
        content_height = min(tile_size, height - map_bottom)
        pil_top = height - map_bottom - content_height
        pil_bottom = height - map_bottom
        for tile_x in range(columns):
            map_left = tile_x * tile_size
            content_width = min(tile_size, width - map_left)
            filename = f"x_{tile_x}_y_{tile_y}.png"
            expected_files.add(filename)
            specifications.append({
                "x": tile_x, "y": tile_y, "filename": filename,
                "content_width": content_width, "content_height": content_height,
                "map_bounds": {
                    "left": map_left, "bottom": map_bottom,
                    "right": map_left + content_width,
                    "top": map_bottom + content_height,
                },
                "image_content_offset": {"x": 0, "y": tile_size - content_height},
                "crop_box": (map_left, pil_top, map_left + content_width, pil_bottom),
            })

    def write_tile(specification: dict) -> int:
        content = image.crop(specification["crop_box"]).convert("RGBA")
        tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
        offset = specification["image_content_offset"]
        tile.paste(content, (offset["x"], offset["y"]))
        path = output / specification["filename"]
        save_png(tile, path)
        return path.stat().st_size

    encode_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        sizes = list(pool.map(write_tile, specifications))
    encode_seconds = time.perf_counter() - encode_started
    for old_tile in output.glob("x_*_y_*.png"):
        if old_tile.name not in expected_files:
            old_tile.unlink()

    tiles = []
    for specification, size in zip(specifications, sizes):
        item = {key: value for key, value in specification.items() if key != "crop_box"}
        item["size_bytes"] = size
        tiles.append(item)
    metadata = {
        "schema_version": 1,
        "map_width": width,
        "map_height": height,
        "tile_size": tile_size,
        "columns": columns,
        "rows": rows,
        "tile_count": len(tiles),
        "coordinate_system": {
            "origin": "bottom-left",
            "x_axis": "right",
            "y_axis": "up",
            "units": "map pixels (one pixel per world metre for the full-size render)",
        },
        "padding": {"mode": "transparent", "rgba": [0, 0, 0, 0]},
        "filename_pattern": "x_{x}_y_{y}.png",
        "tiles": tiles,
        "timings": {"png_encode_seconds": encode_seconds,
                    "total_seconds": time.perf_counter() - started},
    }
    metadata_path = output / "tiles.json"
    metadata["metadata_file"] = metadata_path.name
    metadata["directory_size_bytes"] = sum(sizes)
    for _ in range(4):
        rendered = json.dumps(metadata, indent=2) + "\n"
        total_size = sum(sizes) + len(rendered.encode("utf-8"))
        if metadata["directory_size_bytes"] == total_size:
            break
        metadata["directory_size_bytes"] = total_size
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")
    return metadata


def _prepare_render_inputs(world, normal_chunk_rows: int = 256) -> MapRenderInputs:
    started = time.perf_counter()
    splat = splat_grid(world).astype(np.float32) / 255.0
    topology = topology_grid(world)
    raw_height = int16_grid(world, "height")
    normalized_height = raw_height.astype(np.float32) / np.float32(32766.0)
    heights = world_height_grid(world)
    shore = _shore_distance(world)

    size = normalized_height.shape[0]
    coordinates = np.arange(size, dtype=np.int64)
    norm_y = np.float32(world.size / 1000.0 / size)
    vertex_normals = np.empty((size, size, 3), dtype=np.float32)
    for row0 in range(0, size, normal_chunk_rows):
        row1 = min(row0 + normal_chunk_rows, size)
        ys = coordinates[row0:row1]
        vertex_normals[row0:row1] = _normal_vertices(
            normalized_height, coordinates, ys, norm_y
        )
    return MapRenderInputs(
        splat, topology, normalized_height, heights, shore, vertex_normals,
        time.perf_counter() - started,
    )


def render_map_image(world, scale: float = 0.5, ocean_margin: int = 0,
                     transparent: bool = False, chunk_rows: int = 128,
                     prepared: MapRenderInputs | None = None) -> MapRenderResult:
    started=time.perf_counter(); scale=float(np.clip(scale,0.1,4.0))
    map_res=int(world.size*scale)
    if map_res<=0 or ocean_margin<0: raise ValueError("Invalid map render dimensions")
    width=height=map_res+ocean_margin*2
    if prepared is None:
        prepared = _prepare_render_inputs(world)
        prepare_seconds = prepared.prepare_seconds
    else:
        prepare_seconds = 0.0
    splat=prepared.splat; topo=prepared.topology
    normalized_height=prepared.normalized_height; heights=prepared.heights
    shore=prepared.shore
    timings={"shore_seconds":prepare_seconds}
    channels=4 if transparent else 3
    pixels=np.empty((height,width,channels),np.uint8)
    xs=(np.arange(width,dtype=np.float32)-ocean_margin)/map_res
    for row0 in range(0,height,chunk_rows):
        row1=min(row0+chunk_rows,height)
        # PIL rows are top-down; Unity's SetPixels array is bottom-up.
        ys=(height-1-np.arange(row0,row1,dtype=np.float32)-ocean_margin)/map_res
        color=np.broadcast_to(np.asarray(COLORS["start"],np.float32),(len(ys),width,4)).copy()
        sampled_splat=_bilinear(splat,xs,ys)
        for channel,name in SPLAT_ORDER:
            target=np.asarray(COLORS[name],np.float32)
            t=sampled_splat[channel][...,None]*target[3]
            color += (target-color)*t
        height_values=_bilinear(heights,xs,ys)
        normals=_sample_normals(normalized_height,xs,ys,world.size,prepared.vertex_normals)
        sun=np.maximum(np.sum(normals*SUN,axis=-1),0)
        ix=np.clip(np.trunc(xs*topo.shape[1]).astype(int),0,topo.shape[1]-1)
        iy=np.clip(np.trunc(ys*topo.shape[0]).astype(int),0,topo.shape[0]-1)
        ocean=(topo[np.ix_(iy,ix)] & np.uint32(384))!=0
        shore_dist=_bilinear(shore,xs,ys)*(world.size/shore.shape[0])
        depth=np.zeros_like(height_values,dtype=np.float32)
        wet=shore_dist>0
        depth[wet]=-height_values[wet]
        replace=(depth<=0)|(~ocean)
        depth[wet & replace]=np.maximum(depth[wet & replace],0.1*shore_dist[wet & replace])
        water_mask=depth>0
        if np.any(water_mask):
            wc=np.asarray(COLORS["water"],np.float32); oc=np.asarray(COLORS["offshore"],np.float32)
            if transparent: wc[3]=0.5; oc[:]=0
            t1=np.clip(0.5+depth/5,0,1)[...,None]
            t2=np.clip(depth/(max(abs(float(_bilinear(heights,np.asarray([0],np.float32),np.asarray([0],np.float32))[0,0])),5) if transparent else 50),0,1)[...,None]
            water_color=color+(wc-color)*t1
            water_color=water_color+(oc-water_color)*t2
            color=np.where(water_mask[...,None],water_color,color)
        land=~water_mask
        color[land] += ((sun[land]-0.5)*0.65)[:,None]*color[land]
        color[land]=(color[land]-0.5)*0.94+0.5
        color*=1.05
        color=np.clip(color,0,1)
        converted=np.rint(color*255).astype(np.uint8)
        pixels[row0:row1]=converted[...,:channels]
    image=Image.fromarray(pixels,"RGBA" if transparent else "RGB")
    timings["total_seconds"]=time.perf_counter()-started
    return MapRenderResult(image,width,height,tuple(int(x) for x in pixels[0,0,:3]),timings)


def save_map_render(world, output_dir: str|Path, scale: float=0.5, ocean_margin: int=0,
                    formats=("png","jpg"), debug: bool=False,
                    full_size_png: bool=True, full_size_tiles: bool=False,
                    tile_size: int=512) -> dict:
    if full_size_tiles and not full_size_png:
        raise ValueError("full_size_tiles requires full_size_png=True")
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    output=Path(output_dir); output.mkdir(parents=True,exist_ok=True)
    prepared = _prepare_render_inputs(world)
    # The native render supersedes the convenience scaled render. This keeps
    # full-size exports from doing a second expensive terrain pass merely
    # because TerrainOptions.formats retains its friendly defaults.
    scaled_requested = bool(formats) and not full_size_png
    if scaled_requested:
        result=render_map_image(world,scale,ocean_margin,prepared=prepared)
    else:
        result = None
    full_only_result = (
        render_map_image(world, scale=1.0, ocean_margin=0, prepared=prepared)
        if full_size_png else None
    )
    timing_owner = result if result is not None else full_only_result
    timing_owner.timings["prepare_seconds"] = prepared.prepare_seconds
    artifacts={}
    if scaled_requested and "png" in formats:
        p=output/"map_render.png"; save_png(result.image,p); artifacts[p.name]=p.stat().st_size
    elif (output/"map_render.png").is_file():
        (output/"map_render.png").unlink()
    if scaled_requested and ("jpg" in formats or "jpeg" in formats):
        p=output/"map_render.jpg"; result.image.convert("RGB").save(p,"JPEG",quality=85); artifacts[p.name]=p.stat().st_size
    elif (output/"map_render.jpg").is_file():
        (output/"map_render.jpg").unlink()
    full_size = None
    tile_metadata = None
    if full_size_png:
        full_result = full_only_result
        p=output/"map_render_full.png"
        save_png(full_result.image,p)
        artifacts[p.name]=p.stat().st_size
        full_size={"width":full_result.width,"height":full_result.height,"scale":1.0,
                   "ocean_margin":0,"timings":full_result.timings,"artifact":p.name}
        if full_size_tiles:
            tile_metadata = save_full_map_tiles(
                full_result.image, output / "map_render_tiles", tile_size
            )
            artifacts["map_render_tiles-total"] = tile_metadata["directory_size_bytes"]
    background_owner = result if result is not None else full_result
    metadata={"schema_version":1,"width":result.width if result else None,
              "height":result.height if result else None,
              "scale":float(np.clip(scale,0.1,4)) if result else None,
              "ocean_margin":ocean_margin if result else None,
              "transparent":False,"background_rgb":list(background_owner.background),
              "constants":{"colors":COLORS,"splat_order":[name for _,name in SPLAT_ORDER],"sun_direction":SUN.tolist(),
                           "sun_power":0.65,"brightness":1.05,"contrast":0.94,"max_depth":50.0,
                           "ocean_water_level":0.0,"ocean_topology_mask":384},
              "timings":result.timings if result else {},"full_size_render":full_size,
              "full_size_tiles":tile_metadata,
              "artifacts":artifacts,"validation":{"server_reference":"not_run"}}
    p=output/"map_render_metadata.json"; p.write_text(json.dumps(metadata,indent=2)+"\n",encoding="utf-8",newline="\n")
    return metadata
