from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import struct
import zlib

import lz4.block


@dataclass(frozen=True)
class BundleMember:
    name: str
    flags: int
    data: bytes


@dataclass(frozen=True)
class UnityFSBundle:
    signature: str
    version: int
    unity_version: str
    unity_revision: str
    flags: int
    block_info_hash: bytes
    members: tuple[BundleMember, ...]


def _read_cstr(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.index(0, pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def _decompress_block(data: bytes, expected_size: int, compression: int) -> bytes:
    if compression in (2, 3):
        return lz4.block.decompress(data, uncompressed_size=expected_size)
    if compression == 0:
        return data
    raise RuntimeError(f"Unsupported UnityFS compression mode: {compression}")


def read_unityfs(path: str | Path) -> UnityFSBundle:
    buf = Path(path).read_bytes()
    p = 0
    signature, p = _read_cstr(buf, p)
    if signature != "UnityFS":
        raise RuntimeError(f"Not a UnityFS bundle: {path}")
    version = struct.unpack_from(">I", buf, p)[0]
    p += 4
    unity_version, p = _read_cstr(buf, p)
    unity_revision, p = _read_cstr(buf, p)
    _, compressed_info_size, uncompressed_info_size, flags = struct.unpack_from(">QIII", buf, p)
    p += 20

    if flags & 0x200:
        p = (p + 15) & ~15

    comp_info = buf[p : p + compressed_info_size]
    p += compressed_info_size
    info = _decompress_block(comp_info, uncompressed_info_size, flags & 0x3F)

    if flags & 0x200:
        p = (p + 15) & ~15

    q = 0
    block_info_hash = info[q : q + 16]
    q += 16
    block_count = struct.unpack_from(">I", info, q)[0]
    q += 4
    blocks: list[tuple[int, int, int]] = []
    for _ in range(block_count):
        uncompressed_size, compressed_size, block_flags = struct.unpack_from(">IIH", info, q)
        q += 10
        blocks.append((uncompressed_size, compressed_size, block_flags))

    node_count = struct.unpack_from(">I", info, q)[0]
    q += 4
    nodes: list[tuple[int, int, int, str]] = []
    for _ in range(node_count):
        offset, size, node_flags = struct.unpack_from(">QQI", info, q)
        q += 20
        name, q = _read_cstr(info, q)
        nodes.append((offset, size, node_flags, name))

    payload = bytearray()
    for uncompressed_size, compressed_size, block_flags in blocks:
        chunk = buf[p : p + compressed_size]
        p += compressed_size
        plain = _decompress_block(chunk, uncompressed_size, block_flags & 0x3F)
        if len(plain) != uncompressed_size:
            raise RuntimeError("UnityFS block decompression size mismatch")
        payload += plain

    members = []
    for offset, size, node_flags, name in nodes:
        members.append(BundleMember(name=name, flags=node_flags, data=bytes(payload[offset : offset + size])))

    return UnityFSBundle(
        signature=signature,
        version=version,
        unity_version=unity_version,
        unity_revision=unity_revision,
        flags=flags,
        block_info_hash=block_info_hash,
        members=tuple(members),
    )


def _compress_lz4hc(data: bytes) -> bytes:
    return lz4.block.compress(data, mode="high_compression", compression=12, store_size=False)


def write_unityfs(bundle: UnityFSBundle, members: tuple[BundleMember, ...] | list[BundleMember], out_path: str | Path) -> bytes:
    members = tuple(members)
    payload = b"".join(member.data for member in members)

    chunk_size = 128 * 1024
    blocks: list[tuple[bytes, bytes, int]] = []
    for offset in range(0, len(payload), chunk_size):
        plain = payload[offset : offset + chunk_size]
        compressed = _compress_lz4hc(plain)
        blocks.append((plain, compressed, 3))

    nodes: list[tuple[int, int, int, str]] = []
    offset = 0
    for member in members:
        nodes.append((offset, len(member.data), member.flags, member.name))
        offset += len(member.data)

    info = bytearray(bundle.block_info_hash)
    info += struct.pack(">I", len(blocks))
    for plain, compressed, block_flags in blocks:
        info += struct.pack(">IIH", len(plain), len(compressed), block_flags)
    info += struct.pack(">I", len(nodes))
    for node_offset, size, node_flags, name in nodes:
        info += struct.pack(">QQI", node_offset, size, node_flags)
        info += name.encode("utf-8") + b"\0"

    comp_info = _compress_lz4hc(bytes(info))
    prefix = (
        bundle.signature.encode("utf-8")
        + b"\0"
        + struct.pack(">I", bundle.version)
        + bundle.unity_version.encode("utf-8")
        + b"\0"
        + bundle.unity_revision.encode("utf-8")
        + b"\0"
    )

    def make_header(total_size: int) -> bytes:
        header = prefix + struct.pack(">QIII", total_size, len(comp_info), len(info), bundle.flags)
        if bundle.flags & 0x200:
            header += b"\0" * ((16 - len(header) % 16) % 16)
        header += comp_info
        if bundle.flags & 0x200:
            header += b"\0" * ((16 - len(header) % 16) % 16)
        return header

    body = b"".join(compressed for _, compressed, _ in blocks)
    provisional = make_header(0)
    final = make_header(len(provisional) + len(body)) + body
    Path(out_path).write_bytes(final)
    return final


def expected_crc_from_filename(filename: str) -> int:
    match = re.search(r"_([0-9A-Fa-f]{8})-[0-9A-Fa-f]{32}\.mtga$", Path(filename).name)
    if not match:
        raise ValueError(f"Cannot read MTGA bundle CRC from filename: {filename}")
    return int(match.group(1), 16)


def solve_crc_patch(prefix: bytes, suffix: bytes, target_crc: int) -> bytes:
    prefix_crc = zlib.crc32(prefix)

    def crc_with(patch: bytes) -> int:
        return zlib.crc32(suffix, zlib.crc32(patch, prefix_crc)) & 0xFFFFFFFF

    baseline = crc_with(b"\0" * 4)
    remainder = target_crc ^ baseline
    pivots: dict[int, tuple[int, int]] = {}

    for bit_index in range(32):
        vector = crc_with((1 << bit_index).to_bytes(4, "little")) ^ baseline
        patch_bits = 1 << bit_index
        while vector:
            leading = vector.bit_length() - 1
            if leading not in pivots:
                pivots[leading] = (vector, patch_bits)
                break
            existing_vector, existing_bits = pivots[leading]
            vector ^= existing_vector
            patch_bits ^= existing_bits

    solution = 0
    while remainder:
        leading = remainder.bit_length() - 1
        if leading not in pivots:
            raise RuntimeError("CRC target is unreachable")
        vector, bits = pivots[leading]
        remainder ^= vector
        solution ^= bits

    return solution.to_bytes(4, "little")


def restore_filename_crc(members: tuple[BundleMember, ...] | list[BundleMember], target_crc: int) -> tuple[BundleMember, ...]:
    members = list(members)
    res_index = next((i for i in range(len(members) - 1, -1, -1) if members[i].name.endswith(".resS")), None)
    if res_index is None:
        raise RuntimeError("No .resS member available for CRC correction")

    prefix = b"".join(member.data for member in members[:res_index]) + members[res_index].data
    suffix = b"".join(member.data for member in members[res_index + 1 :])
    patch = solve_crc_patch(prefix, suffix, target_crc)
    members[res_index] = BundleMember(members[res_index].name, members[res_index].flags, members[res_index].data + patch)

    payload = b"".join(member.data for member in members)
    if zlib.crc32(payload) & 0xFFFFFFFF != target_crc:
        raise RuntimeError("Failed to restore requested AssetBundle CRC")
    return tuple(members)
