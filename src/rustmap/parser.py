"""Dependency-light parser for Facepunch Rust ``.map`` world files.

The only external dependency is ``lz4``. Protobuf wire decoding is implemented
locally so the parser does not need generated ``.proto`` classes.
"""

from __future__ import annotations

import json
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import lz4.block


class RustMapError(ValueError):
    """Raised when a map is truncated, corrupt, or unsupported."""


@dataclass(slots=True)
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass(slots=True)
class MapLayer:
    name: str
    data: memoryview


@dataclass(slots=True)
class Prefab:
    category: str = ""
    prefab_id: int = 0
    position: Vector3 | None = None
    rotation: Vector3 | None = None
    scale: Vector3 | None = None


@dataclass(slots=True)
class MapPath:
    name: str = ""
    spline: bool = False
    start: bool = False
    end: bool = False
    width: float = 0.0
    inner_padding: float = 0.0
    outer_padding: float = 0.0
    inner_fade: float = 0.0
    outer_fade: float = 0.0
    random_scale: float = 0.0
    mesh_offset: float = 0.0
    terrain_offset: float = 0.0
    splat: int = 0
    topology: int = 0
    nodes: list[Vector3] = field(default_factory=list)


@dataclass(slots=True)
class RustMap:
    serialization_version: int
    timestamp: int
    size: int
    layers: list[MapLayer]
    prefabs: list[Prefab]
    paths: list[MapPath]
    _protobuf_buffer: bytearray = field(repr=False)

    def layer(self, name: str) -> MapLayer:
        for layer in self.layers:
            if layer.name.casefold() == name.casefold():
                return layer
        raise KeyError(name)

    def summary(self) -> dict:
        categories = Counter(p.category for p in self.prefabs)
        prefab_ids = Counter(p.prefab_id for p in self.prefabs)
        return {
            "serialization_version": self.serialization_version,
            "timestamp": self.timestamp,
            "world_size": self.size,
            "layers": [{"name": x.name, "bytes": len(x.data)} for x in self.layers],
            "prefab_count": len(self.prefabs),
            "path_count": len(self.paths),
            "prefab_categories": dict(categories.most_common()),
            "most_common_prefab_ids": [
                {"id": prefab_id, "count": count}
                for prefab_id, count in prefab_ids.most_common(100)
            ],
        }


def _read_varint(data: memoryview, offset: int, limit: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < limit and shift < 70:
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
    raise RustMapError("Invalid or truncated varint")


def _fields(data: memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    offset = 0
    limit = len(data)
    while offset < limit:
        tag, offset = _read_varint(data, offset, limit)
        field_number, wire_type = tag >> 3, tag & 7
        if field_number == 0:
            raise RustMapError("Invalid protobuf field number 0")
        if wire_type == 0:
            value, offset = _read_varint(data, offset, limit)
            yield field_number, wire_type, value
        elif wire_type == 1:
            if offset + 8 > limit:
                raise RustMapError("Truncated protobuf fixed64")
            value = struct.unpack_from("<Q", data, offset)[0]
            offset += 8
            yield field_number, wire_type, value
        elif wire_type == 2:
            length, offset = _read_varint(data, offset, limit)
            end = offset + length
            if end > limit:
                raise RustMapError("Truncated protobuf length-delimited field")
            yield field_number, wire_type, data[offset:end]
            offset = end
        elif wire_type == 5:
            if offset + 4 > limit:
                raise RustMapError("Truncated protobuf fixed32")
            value = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            yield field_number, wire_type, value
        else:
            raise RustMapError(f"Unsupported protobuf wire type {wire_type}")


def _text(value: int | memoryview) -> str:
    if not isinstance(value, memoryview):
        raise RustMapError("Expected a protobuf string")
    return bytes(value).decode("utf-8", errors="replace")


def _float32(value: int | memoryview) -> float:
    if not isinstance(value, int):
        raise RustMapError("Expected a protobuf fixed32")
    return struct.unpack("<f", struct.pack("<I", value))[0]


def _vector(data: memoryview) -> Vector3:
    result = Vector3()
    for number, wire, value in _fields(data):
        if wire != 5:
            continue
        if number == 1:
            result.x = _float32(value)
        elif number == 2:
            result.y = _float32(value)
        elif number == 3:
            result.z = _float32(value)
    return result


def _map_layer(data: memoryview) -> MapLayer:
    name = ""
    payload = memoryview(b"")
    for number, wire, value in _fields(data):
        if number == 1 and wire == 2:
            name = _text(value)
        elif number == 2 and wire == 2 and isinstance(value, memoryview):
            payload = value
    return MapLayer(name, payload)


def _prefab(data: memoryview) -> Prefab:
    result = Prefab()
    for number, wire, value in _fields(data):
        if number == 1 and wire == 2:
            result.category = _text(value)
        elif number == 2 and wire == 0:
            result.prefab_id = int(value)
        elif wire == 2 and isinstance(value, memoryview):
            if number == 3:
                result.position = _vector(value)
            elif number == 4:
                result.rotation = _vector(value)
            elif number == 5:
                result.scale = _vector(value)
    return result


def _path(data: memoryview) -> MapPath:
    result = MapPath()
    float_fields = {
        5: "width", 6: "inner_padding", 7: "outer_padding", 8: "inner_fade",
        9: "outer_fade", 10: "random_scale", 11: "mesh_offset", 12: "terrain_offset",
    }
    for number, wire, value in _fields(data):
        if number == 1 and wire == 2:
            result.name = _text(value)
        elif number in (2, 3, 4) and wire == 0:
            setattr(result, {2: "spline", 3: "start", 4: "end"}[number], bool(value))
        elif number in float_fields and wire == 5:
            setattr(result, float_fields[number], _float32(value))
        elif number == 13 and wire == 0:
            result.splat = int(value)
        elif number == 14 and wire == 0:
            result.topology = int(value)
        elif number == 15 and wire == 2 and isinstance(value, memoryview):
            result.nodes.append(_vector(value))
    return result


def _read_stream_varint(stream, *, allow_eof: bool = False) -> int | None:
    result = 0
    shift = 0
    for index in range(10):
        raw = stream.read(1)
        if not raw:
            if allow_eof and index == 0:
                return None
            raise RustMapError("Truncated legacy LZ4 chunk header")
        byte = raw[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result
        shift += 7
    raise RustMapError("Invalid legacy LZ4 chunk varint")


def _decompress_legacy_lz4(stream) -> bytearray:
    output = bytearray()
    while True:
        flags = _read_stream_varint(stream, allow_eof=True)
        if flags is None:
            break
        original_length = _read_stream_varint(stream)
        assert original_length is not None
        is_compressed = bool(flags & 1)
        if flags >> 2:
            raise RustMapError("Multi-pass legacy LZ4 chunks are unsupported")
        compressed_length = _read_stream_varint(stream) if is_compressed else original_length
        assert compressed_length is not None
        if compressed_length > original_length:
            raise RustMapError("Legacy LZ4 chunk length is invalid")
        block = stream.read(compressed_length)
        if len(block) != compressed_length:
            raise RustMapError("Truncated legacy LZ4 chunk")
        if is_compressed:
            decoded = lz4.block.decompress(block, uncompressed_size=original_length)
            if len(decoded) != original_length:
                raise RustMapError("Legacy LZ4 block decoded to the wrong size")
            output.extend(decoded)
        else:
            output.extend(block)
    return output


def load_map(path: str | Path) -> RustMap:
    """Load and decode a Rust world map.

    Layer byte arrays are zero-copy ``memoryview`` objects backed by the map's
    decompressed protobuf buffer. Keep the returned ``RustMap`` alive while
    using them.
    """
    path = Path(path)
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise RustMapError("File is too short to be a Rust map")
        version, timestamp = struct.unpack("<IQ", header)
        protobuf_buffer = _decompress_legacy_lz4(stream)

    view = memoryview(protobuf_buffer)
    size = 0
    layers: list[MapLayer] = []
    prefabs: list[Prefab] = []
    paths: list[MapPath] = []
    for number, wire, value in _fields(view):
        if number == 1 and wire == 0:
            size = int(value)
        elif wire == 2 and isinstance(value, memoryview):
            if number == 2:
                layers.append(_map_layer(value))
            elif number == 3:
                prefabs.append(_prefab(value))
            elif number == 4:
                paths.append(_path(value))
    return RustMap(version, timestamp, size, layers, prefabs, paths, protobuf_buffer)

