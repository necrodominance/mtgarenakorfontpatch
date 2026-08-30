from __future__ import annotations

from dataclasses import dataclass
import struct
from pathlib import Path


@dataclass(frozen=True)
class TypeInfo:
    class_id: int
    script_type_index: int
    nodes: tuple[tuple, ...]
    string_buffer: bytes


@dataclass(frozen=True)
class ObjectInfo:
    path_id: int
    relative_offset: int
    start: int
    size: int
    type_index: int
    record_offset: int


@dataclass(frozen=True)
class ExternalInfo:
    guid: bytes
    type: int
    path: str


@dataclass(frozen=True)
class SerializedFile:
    data: bytes
    version: int
    metadata_size: int
    file_size: int
    data_offset: int
    endian: str
    unity_version: str
    platform: int
    enable_type_tree: bool
    types: tuple[TypeInfo, ...]
    objects: tuple[ObjectInfo, ...]
    externals: tuple[ExternalInfo, ...]

    def class_id(self, obj: ObjectInfo) -> int:
        return self.types[obj.type_index].class_id

    def object_by_pid(self, path_id: int) -> ObjectInfo:
        for obj in self.objects:
            if obj.path_id == path_id:
                return obj
        raise KeyError(path_id)

    def object_bytes(self, obj: ObjectInfo | int) -> bytes:
        if isinstance(obj, int):
            obj = self.object_by_pid(obj)
        return self.data[obj.start : obj.start + obj.size]


class _Reader:
    def __init__(self, data: bytes, endian: str = ">"):
        self.data = data
        self.pos = 0
        self.endian = endian

    def read(self, size: int) -> bytes:
        result = self.data[self.pos : self.pos + size]
        self.pos += size
        return result

    def u8(self) -> int:
        return self.read(1)[0]

    def i16(self) -> int:
        return struct.unpack(self.endian + "h", self.read(2))[0]

    def i32(self) -> int:
        return struct.unpack(self.endian + "i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack(self.endian + "I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack(self.endian + "q", self.read(8))[0]

    def cstr(self) -> str:
        end = self.data.index(0, self.pos)
        value = self.data[self.pos : end].decode("utf-8", "replace")
        self.pos = end + 1
        return value

    def align(self, alignment: int = 4) -> None:
        self.pos = (self.pos + alignment - 1) // alignment * alignment


def parse_serialized_bytes(data: bytes) -> SerializedFile:
    r = _Reader(data, ">")
    r.u32()  # old metadata size
    r.u32()  # old file size
    version = r.u32()
    r.u32()  # old data offset
    endian_flag = r.u8()
    r.read(3)
    if version < 22:
        raise RuntimeError(f"Unsupported Unity SerializedFile version: {version}; expected >= 22")

    metadata_size = r.u32()
    file_size = r.i64()
    data_offset = r.i64()
    r.i64()  # unknown/reserved

    r.endian = ">" if endian_flag else "<"
    unity_version = r.cstr()
    platform = r.i32()
    enable_type_tree = bool(r.u8())
    type_count = r.i32()
    types: list[TypeInfo] = []

    for _ in range(type_count):
        class_id = r.i32()
        r.u8()  # stripped
        script_type_index = r.i16()
        if class_id == 114:
            r.read(16)  # script id
        r.read(16)  # old type hash

        nodes: tuple[tuple, ...] = ()
        string_buffer = b""
        if enable_type_tree:
            node_count = r.i32()
            string_size = r.i32()
            raw = r.read(node_count * 32)
            string_buffer = r.read(string_size)
            parsed_nodes = []
            for index in range(node_count):
                parsed_nodes.append(struct.unpack(r.endian + "hBBIIiiiQ", raw[index * 32 : (index + 1) * 32]))
            nodes = tuple(parsed_nodes)

        if enable_type_tree:
            dependency_count = r.i32()
            r.read(dependency_count * 4)
        types.append(TypeInfo(class_id, script_type_index, nodes, string_buffer))

    object_count = r.i32()
    objects: list[ObjectInfo] = []
    for _ in range(object_count):
        r.align(4)
        record_offset = r.pos
        path_id = r.i64()
        relative_offset = r.i64()
        size = r.u32()
        type_index = r.i32()
        objects.append(ObjectInfo(path_id, relative_offset, data_offset + relative_offset, size, type_index, record_offset))

    # Script type references are followed by the SerializedFile external table.
    # PPtr m_FileID values are 1-based indexes into this table.
    script_type_count = r.i32()
    for _ in range(script_type_count):
        r.i32()
        r.i64()

    external_count = r.i32()
    externals: list[ExternalInfo] = []
    for _ in range(external_count):
        r.cstr()  # legacy/unused external name
        guid = r.read(16)
        external_type = r.i32()
        path = r.cstr()
        externals.append(ExternalInfo(guid=guid, type=external_type, path=path))

    if file_size != len(data):
        raise RuntimeError(f"SerializedFile header size mismatch: header={file_size}, actual={len(data)}")

    return SerializedFile(
        data=data,
        version=version,
        metadata_size=metadata_size,
        file_size=file_size,
        data_offset=data_offset,
        endian=r.endian,
        unity_version=unity_version,
        platform=platform,
        enable_type_tree=enable_type_tree,
        types=tuple(types),
        objects=tuple(objects),
        externals=tuple(externals),
    )


def parse_serialized(path: str | Path) -> SerializedFile:
    return parse_serialized_bytes(Path(path).read_bytes())


def rebuild_serialized(original: SerializedFile, replacements: dict[int, bytes], alignment: int = 16) -> bytes:
    ordered = sorted(original.objects, key=lambda obj: obj.start)
    new_data = bytearray()
    positions: dict[int, tuple[int, int]] = {}

    for obj in ordered:
        while len(new_data) % alignment:
            new_data.append(0)
        relative_offset = len(new_data)
        raw = replacements.get(obj.path_id, original.object_bytes(obj))
        positions[obj.path_id] = (relative_offset, len(raw))
        new_data += raw

    out = bytearray(original.data[: original.data_offset])
    for obj in original.objects:
        relative_offset, size = positions[obj.path_id]
        struct.pack_into(original.endian + "q", out, obj.record_offset + 8, relative_offset)
        struct.pack_into(original.endian + "I", out, obj.record_offset + 16, size)

    out += new_data
    struct.pack_into(">q", out, 24, len(out))

    rebuilt = parse_serialized_bytes(bytes(out))
    for obj in rebuilt.objects:
        expected_offset, expected_size = positions[obj.path_id]
        if obj.relative_offset != expected_offset or obj.size != expected_size:
            raise RuntimeError(f"SerializedFile rebuild verification failed for PathID {obj.path_id}")
    return bytes(out)
