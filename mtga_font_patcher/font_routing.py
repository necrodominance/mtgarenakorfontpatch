from __future__ import annotations

import struct

from .discovery import find_named_object
from .serialized import parse_serialized_bytes, rebuild_serialized
from .typetree import TypeNode, Reader, _parse_node, parse_top_fields


def is_korean_codepoint(codepoint: int) -> bool:
    return any(
        start <= codepoint <= end
        for start, end in (
            (0x1100, 0x11FF),
            (0x3130, 0x318F),
            (0xA960, 0xA97F),
            (0xAC00, 0xD7A3),
            (0xD7B0, 0xD7FF),
            (0xFFA0, 0xFFDC),
        )
    )


def _node_child_slices(raw: bytes, node: TypeNode) -> dict[str, bytes]:
    reader = Reader(raw, 0)
    result: dict[str, bytes] = {}
    for child in node.children:
        start = reader.pos
        _parse_node(child, reader, False)
        result[child.name] = raw[start:reader.pos]
    if reader.pos != len(raw):
        raise RuntimeError('Nested TypeTree slice did not consume the full field')
    return result


def _serialize_pptr_array(entries: list[dict[str, int]]) -> bytes:
    raw = bytearray(struct.pack('<i', len(entries)))
    for entry in entries:
        raw += struct.pack('<iq', int(entry['m_FileID']), int(entry['m_PathID']))
    return bytes(raw)


def _fallback_with_prefix(
    existing: object,
    prefix: list[dict[str, int]],
    exclude: list[dict[str, int]] | None = None,
) -> bytes:
    if not isinstance(existing, list):
        raise RuntimeError('FontAsset fallback table is not a list')
    normalized: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    excluded = {
        (int(item['m_FileID']), int(item['m_PathID']))
        for item in (exclude or [])
    }
    for item in prefix:
        entry = {'m_FileID': int(item['m_FileID']), 'm_PathID': int(item['m_PathID'])}
        key = (entry['m_FileID'], entry['m_PathID'])
        if key not in seen and key not in excluded:
            normalized.append(entry)
            seen.add(key)
    for item in existing:
        entry = {'m_FileID': int(item['m_FileID']), 'm_PathID': int(item['m_PathID'])}
        key = (entry['m_FileID'], entry['m_PathID'])
        if key not in seen and key not in excluded:
            normalized.append(entry)
            seen.add(key)
    return _serialize_pptr_array(normalized)


def _serialize_byte_array(data: bytes) -> bytes:
    raw = bytearray(struct.pack('<i', len(data)))
    raw += data
    while len(raw) % 4:
        raw.append(0)
    return bytes(raw)


def _font_weight_with_asset(existing: object, asset: dict[str, int], *, index: int = 7) -> bytes:
    if not isinstance(existing, list) or len(existing) != 10:
        raise RuntimeError('FontAsset weight table is not the expected 10-entry array')
    if not (0 <= int(index) < len(existing)):
        raise RuntimeError(f'FontAsset weight index out of range: {index}')

    normalized = {
        'm_FileID': int(asset['m_FileID']),
        'm_PathID': int(asset['m_PathID']),
    }
    pairs: list[tuple[dict[str, int], dict[str, int]]] = []
    for i, pair in enumerate(existing):
        if not isinstance(pair, dict):
            raise RuntimeError('FontAsset weight table contains a non-object entry')
        regular = pair.get('regularTypeface')
        italic = pair.get('italicTypeface')
        if not isinstance(regular, dict) or not isinstance(italic, dict):
            raise RuntimeError('FontAsset weight pair has an unexpected layout')
        if i == index:
            pairs.append((normalized, normalized))
        else:
            pairs.append((
                {'m_FileID': int(regular['m_FileID']), 'm_PathID': int(regular['m_PathID'])},
                {'m_FileID': int(italic['m_FileID']), 'm_PathID': int(italic['m_PathID'])},
            ))

    raw = bytearray(struct.pack('<i', len(pairs)))
    for regular, italic in pairs:
        raw += struct.pack('<iq', regular['m_FileID'], regular['m_PathID'])
        raw += struct.pack('<iq', italic['m_FileID'], italic['m_PathID'])
    return bytes(raw)


def patch_static_font_router(
    target_cab: bytes,
    schema_root: TypeNode,
    target_name: str,
    fallback_pptrs: list[dict[str, int]],
    *,
    font_weight_pptr: dict[str, int] | None = None,
    font_weight_index: int = 7,
) -> bytes:
    target = parse_serialized_bytes(target_cab)
    obj = find_named_object(target, schema_root, target_name)
    fields, slices, order = parse_top_fields(
        target.data, obj.start, obj.size, schema_root, store=True
    )
    overrides: dict[str, bytes] = {
        'm_CharacterTable': struct.pack('<i', 0),
        'm_FallbackFontAssetTable': _fallback_with_prefix(
            fields.get('m_FallbackFontAssetTable'), fallback_pptrs
        ),
    }
    if font_weight_pptr is not None:
        for field_name in ('m_FontWeightTable', 'fontWeights'):
            table = fields.get(field_name)
            if isinstance(table, list) and len(table) > int(font_weight_index):
                overrides[field_name] = _font_weight_with_asset(
                    table, font_weight_pptr, index=int(font_weight_index)
                )
    raw = b''.join(overrides.get(name, slices[name]) for name in order)
    rebuilt = rebuild_serialized(target, {obj.path_id: raw}, alignment=16)

    verify = parse_serialized_bytes(rebuilt)
    vobj = find_named_object(verify, schema_root, target_name)
    vfields, _, _ = parse_top_fields(
        verify.data, vobj.start, vobj.size, schema_root, store=True
    )
    if vfields.get('m_CharacterTable'):
        raise RuntimeError(f'{target_name} router still has direct characters')
    expected = [
        (int(p['m_FileID']), int(p['m_PathID'])) for p in fallback_pptrs
    ]
    got = [
        (int(p['m_FileID']), int(p['m_PathID']))
        for p in (vfields.get('m_FallbackFontAssetTable') or [])[:len(expected)]
    ]
    if got != expected:
        raise RuntimeError(f'{target_name} router fallback verification failed')
    return rebuilt


def patch_font_weight_refs(
    target_cab: bytes,
    schema_root: TypeNode,
    target_name: str,
    asset_pptr: dict[str, int],
    *,
    indices: tuple[int, ...] = (7,),
    regular: bool = True,
    italic: bool = True,
) -> bytes:
    target = parse_serialized_bytes(target_cab)
    obj = find_named_object(target, schema_root, target_name)
    fields, slices, order = parse_top_fields(
        target.data, obj.start, obj.size, schema_root, store=True
    )
    normalized = {'m_FileID': int(asset_pptr['m_FileID']), 'm_PathID': int(asset_pptr['m_PathID'])}
    overrides: dict[str, bytes] = {}
    for field_name in ('m_FontWeightTable', 'fontWeights'):
        table = fields.get(field_name)
        if not isinstance(table, list) or len(table) == 0:
            continue
        if len(table) != 10:
            raise RuntimeError(f'Unexpected {field_name} size for {target_name}')
        entries = []
        for i, pair in enumerate(table):
            pair = dict(pair)
            if i in indices:
                if regular:
                    pair['regularTypeface'] = dict(normalized)
                if italic:
                    pair['italicTypeface'] = dict(normalized)
            entries.append(pair)
        raw = bytearray(struct.pack('<i', len(entries)))
        for pair in entries:
            for key in ('regularTypeface', 'italicTypeface'):
                p = pair[key]
                raw += struct.pack('<iq', int(p['m_FileID']), int(p['m_PathID']))
        overrides[field_name] = bytes(raw)
    if not overrides:
        raise RuntimeError(f'{target_name} has no usable font-weight table')
    return rebuild_serialized(
        target,
        {obj.path_id: b''.join(overrides.get(name, slices[name]) for name in order)},
        alignment=16,
    )
