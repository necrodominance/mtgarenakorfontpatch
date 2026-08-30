from __future__ import annotations

from dataclasses import dataclass
import struct

from .serialized import SerializedFile, TypeInfo, ObjectInfo


COMMON_TYPES = {
    49: "Array",
    76: "bool",
    81: "char",
    161: "float",
    222: "int",
    263: "MonoBehaviour",
    543: "TypelessData",
    564: "PPtr<GameObject>",
    616: "PPtr<MonoScript>",
    814: "SInt64",
    840: "string",
    894: "TypelessData",
    921: "UInt64",
    928: "bool",
    934: "unsigned int",
    981: "vector",
}
COMMON_NAMES = {
    49: "Array",
    55: "data",
    106: "data",
    155: "size",
    374: "m_GameObject",
    387: "index",
    427: "m_Name",
    490: "m_Script",
    526: "m_EditorClassIdentifier",
    778: "data",
    795: "size",
}


def _resolve(buffer: bytes, offset: int, is_type: bool) -> str:
    if offset & 0x80000000:
        key = offset & 0x7FFFFFFF
        return (COMMON_TYPES if is_type else COMMON_NAMES).get(key, f"COMMON:{key}")
    end = buffer.find(b"\0", offset)
    if end < 0:
        raise RuntimeError("Invalid type tree string offset")
    return buffer[offset:end].decode("utf-8", "replace")


@dataclass
class TypeNode:
    level: int
    type_name: str
    name: str
    byte_size: int
    meta_flags: int
    children: list["TypeNode"]


def build_tree(type_info: TypeInfo) -> TypeNode:
    if not type_info.nodes:
        raise RuntimeError("Target SerializedFile does not contain a type tree for this type")
    flat = []
    for raw in type_info.nodes:
        _, level, _, type_offset, name_offset, byte_size, _, meta_flags, _ = raw
        flat.append(
            TypeNode(
                level=level,
                type_name=_resolve(type_info.string_buffer, type_offset, True),
                name=_resolve(type_info.string_buffer, name_offset, False),
                byte_size=byte_size,
                meta_flags=meta_flags,
                children=[],
            )
        )
    root = flat[0]
    stack = [root]
    for node in flat[1:]:
        while len(stack) > node.level:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)
    return root


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def read(self, size: int) -> bytes:
        result = self.data[self.pos : self.pos + size]
        if len(result) != size:
            raise RuntimeError("TypeTree parse ran past the object boundary")
        self.pos += size
        return result

    def align(self, alignment: int = 4) -> None:
        self.pos = (self.pos + alignment - 1) // alignment * alignment

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]


def _parse_node(node: TypeNode, reader: Reader, store: bool):
    if node.type_name == "string":
        length = reader.i32()
        if length < 0 or length > 100_000_000:
            raise RuntimeError(f"Invalid string length {length} for {node.name}")
        raw = reader.read(length)
        value = raw.decode("utf-8", "replace") if store else None
        reader.align(4)
    elif node.children and node.children[0].type_name == "Array":
        array = node.children[0]
        count = reader.i32()
        if count < 0 or count > 100_000_000:
            raise RuntimeError(f"Invalid array count {count} for {node.name}")
        element = array.children[-1]
        if element.type_name in ("char", "UInt8", "SInt8") and not element.children and element.byte_size in (1, -1):
            raw = reader.read(count)
            value = raw if store and count < 2_000_000 else {"count": count}
        else:
            values = [] if store and count < 100_000 else None
            for _ in range(count):
                parsed = _parse_node(element, reader, store)
                if values is not None:
                    values.append(parsed)
            value = values if values is not None else {"count": count}
        if array.meta_flags & 0x4000:
            reader.align(4)
    elif not node.children:
        t = node.type_name
        if t == "int":
            value = reader.i32()
        elif t == "unsigned int":
            value = reader.u32()
        elif t == "float":
            value = reader.f32()
        elif t == "SInt64":
            value = reader.i64()
        elif t == "UInt64":
            value = reader.u64()
        elif t == "bool":
            value = bool(reader.read(1)[0])
        elif t in ("char", "UInt8", "SInt8"):
            value = reader.read(1)[0]
        elif node.byte_size == 8 and node.name == "offset":
            value = reader.u64()
        elif node.byte_size > 0:
            value = reader.read(node.byte_size)
        else:
            raise RuntimeError(f"Unsupported TypeTree leaf: {t} {node.name} size={node.byte_size}")
    else:
        mapping = {} if store else None
        for child in node.children:
            value_child = _parse_node(child, reader, store)
            if mapping is not None:
                mapping[child.name] = value_child
        value = mapping

    if node.meta_flags & 0x4000:
        reader.align(4)
    return value


def parse_top_fields(data: bytes, start: int, size: int, root: TypeNode, store: bool = True):
    reader = Reader(data, start)
    fields: dict[str, object] = {}
    slices: dict[str, bytes] = {}
    order: list[str] = []
    for child in root.children:
        field_start = reader.pos
        value = _parse_node(child, reader, store)
        field_end = reader.pos
        order.append(child.name)
        fields[child.name] = value
        slices[child.name] = data[field_start:field_end]
    consumed = reader.pos - start
    if consumed != size:
        raise RuntimeError(f"TypeTree object size mismatch: parsed {consumed}, expected {size}")
    return fields, slices, order


def root_for_object(serialized: SerializedFile, obj: ObjectInfo) -> TypeNode:
    return build_tree(serialized.types[obj.type_index])


def root_for_class(serialized: SerializedFile, class_id: int) -> TypeNode:
    for type_info in serialized.types:
        if type_info.class_id == class_id and type_info.nodes:
            return build_tree(type_info)
    raise RuntimeError(f"No type tree found for Unity class ID {class_id}")
