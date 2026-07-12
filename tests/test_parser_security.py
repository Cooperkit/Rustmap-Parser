from io import BytesIO

import pytest

from rustmap.parser import (
    MAX_LZ4_CHUNK_BYTES,
    MAX_LZ4_CHUNKS,
    RustMapError,
    _decompress_legacy_lz4,
)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def test_legacy_lz4_rejects_attacker_controlled_oversized_chunk() -> None:
    stream = BytesIO(_varint(0) + _varint(MAX_LZ4_CHUNK_BYTES + 1))
    with pytest.raises(RustMapError, match="chunk exceeds"):
        _decompress_legacy_lz4(stream)


def test_legacy_lz4_rejects_pathological_chunk_count() -> None:
    zero_length_chunk = _varint(0) + _varint(0)
    stream = BytesIO(zero_length_chunk * (MAX_LZ4_CHUNKS + 1))
    with pytest.raises(RustMapError, match="too many chunks"):
        _decompress_legacy_lz4(stream)
