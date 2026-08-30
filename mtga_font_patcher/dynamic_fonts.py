from __future__ import annotations

from io import BytesIO
import struct

from fontTools.ttLib import TTFont

from .discovery import find_named_object
from .serialized import parse_serialized_bytes, rebuild_serialized
from .typetree import TypeNode, parse_top_fields, root_for_object
from .font_routing import _fallback_with_prefix, _node_child_slices, _serialize_byte_array


def _aligned_string(value: str) -> bytes:
    data = value.encode('utf-8')
    raw = bytearray(struct.pack('<i', len(data)))
    raw += data
    while len(raw) % 4:
        raw.append(0)
    return bytes(raw)


def _string_array(values: list[str]) -> bytes:
    raw = bytearray(struct.pack('<i', len(values)))
    for value in values:
        raw += _aligned_string(value)
    return bytes(raw)


def _font_metrics(font_bytes: bytes) -> tuple[int, float, float, float]:
    font = TTFont(BytesIO(font_bytes), recalcBBoxes=False, recalcTimestamp=False)
    try:
        upm = int(font['head'].unitsPerEm) if 'head' in font else 1000
        hhea = font['hhea'] if 'hhea' in font else None
        if hhea is None:
            asc, desc, gap = upm * 0.8, -upm * 0.2, 0
        else:
            asc, desc, gap = hhea.ascent, hhea.descent, hhea.lineGap
        return upm, float(asc), float(desc), float(gap)
    finally:
        font.close()


def _face_identity_slice(
    target_face_raw: bytes,
    font_asset_root: TypeNode,
    *,
    family: str,
    style: str,
    units_per_em: int,
    scale: float,
) -> bytes:
    face_node = next((child for child in font_asset_root.children if child.name == 'm_FaceInfo'), None)
    if face_node is None:
        raise RuntimeError('FontAsset has no m_FaceInfo field')
    parts = _node_child_slices(target_face_raw, face_node)
    out: list[bytes] = []
    for child in face_node.children:
        if child.name == 'm_FamilyName':
            out.append(_aligned_string(family))
        elif child.name == 'm_StyleName':
            out.append(_aligned_string(style))
        elif child.name == 'm_UnitsPerEM':
            out.append(struct.pack('<i', int(units_per_em)))
        elif child.name == 'm_Scale':
            out.append(struct.pack('<f', float(scale)))
        else:
            out.append(parts[child.name])
    return b''.join(out)


def patch_dynamic_font_from_bytes(
    target_cab: bytes,
    schema_root: TypeNode,
    target_name: str,
    font_bytes: bytes,
    *,
    family: str,
    style: str,
    font_scale: float = 1.10,
    fallback_pptrs: list[dict[str, int]] | None = None,
    source_schema_root: TypeNode | None = None,
) -> bytes:
    """Replace an existing Dynamic TMP slot's source font without a donor bundle.

    PathIDs, material, atlas texture, and source-Font PPtr remain stable. Only
    the embedded source font bytes, source identity/metrics, TMP face identity,
    configured face scale, and optionally fallback routing are changed.
    """
    # Validate before touching Unity serialization.
    upm, asc_units, desc_units, gap_units = _font_metrics(font_bytes)

    target = parse_serialized_bytes(target_cab)
    obj = find_named_object(target, schema_root, target_name)
    fields, slices, order = parse_top_fields(
        target.data, obj.start, obj.size, schema_root, store=True
    )
    if int(fields.get('m_AtlasPopulationMode', -1)) != 1:
        raise RuntimeError(f'{target_name} is not a Dynamic TMP FontAsset')

    overrides: dict[str, bytes] = {
        'm_FaceInfo': _face_identity_slice(
            slices['m_FaceInfo'], schema_root,
            family=family, style=style, units_per_em=upm, scale=font_scale,
        )
    }
    if fallback_pptrs is not None:
        overrides['m_FallbackFontAssetTable'] = _fallback_with_prefix(
            fields.get('m_FallbackFontAssetTable'), fallback_pptrs
        )
    patched_asset = b''.join(overrides.get(name, slices[name]) for name in order)

    source_pid = int(fields['m_SourceFontFile']['m_PathID'])
    if not source_pid:
        raise RuntimeError(f'{target_name} has no source Font PPtr')
    source_obj = target.object_by_pid(source_pid)
    source_root = source_schema_root if source_schema_root is not None else root_for_object(target, source_obj)
    source_fields, source_slices, source_order = parse_top_fields(
        target.data, source_obj.start, source_obj.size, source_root, store=True
    )
    font_size = float(source_fields.get('m_FontSize', 16.0) or 16.0)
    ascent = font_size * asc_units / upm
    descent = font_size * desc_units / upm
    line_spacing = font_size * (asc_units - desc_units + gap_units) / upm
    source_name = f'{family}-{style}'.replace(' ', '-')
    source_overrides = {
        'm_Name': _aligned_string(source_name),
        'm_FontNames': _string_array([family]),
        'm_FontData': _serialize_byte_array(font_bytes),
        'm_Ascent': struct.pack('<f', float(ascent)),
        'm_Descent': struct.pack('<f', float(descent)),
        'm_LineSpacing': struct.pack('<f', float(line_spacing)),
    }
    patched_source = b''.join(
        source_overrides.get(name, source_slices[name]) for name in source_order
    )

    rebuilt = rebuild_serialized(
        target,
        {obj.path_id: patched_asset, source_obj.path_id: patched_source},
        alignment=16,
    )

    verify = parse_serialized_bytes(rebuilt)
    vobj = find_named_object(verify, schema_root, target_name)
    vfields, _, _ = parse_top_fields(
        verify.data, vobj.start, vobj.size, schema_root, store=True
    )
    if int(vfields['m_SourceFontFile']['m_PathID']) != source_pid:
        raise RuntimeError(f'{target_name} source Font PPtr changed unexpectedly')
    if str(vfields['m_FaceInfo']['m_FamilyName']) != family:
        raise RuntimeError(f'{target_name} family verification failed')
    if str(vfields['m_FaceInfo']['m_StyleName']) != style:
        raise RuntimeError(f'{target_name} style verification failed')
    vsource = verify.object_by_pid(source_pid)
    vroot = source_schema_root if source_schema_root is not None else root_for_object(verify, vsource)
    _, vslices, _ = parse_top_fields(
        verify.data, vsource.start, vsource.size, vroot, store=True
    )
    raw = vslices['m_FontData']
    count = struct.unpack_from('<i', raw, 0)[0]
    if raw[4:4+count] != font_bytes:
        raise RuntimeError(f'{target_name} embedded font verification failed')
    return rebuilt
