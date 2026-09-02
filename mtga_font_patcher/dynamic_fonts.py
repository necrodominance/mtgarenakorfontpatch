from __future__ import annotations

from io import BytesIO
import struct

from fontTools.ttLib import TTFont

from .discovery import find_named_object
from .serialized import parse_serialized_bytes, rebuild_serialized
from .typetree import TypeNode, parse_top_fields, root_for_object
from .font_routing import (
    _fallback_with_prefix,
    _node_child_slices,
    _serialize_byte_array,
    _serialize_pptr_array,
    patch_font_weight_refs,
)


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


_DYNAMIC_FACADE_COPY_FIELDS = (
    'm_SourceFontFileGUID',
    'm_CreationSettings',
    'm_SourceFontFilePath',
    'm_AtlasPopulationMode',
    'InternalDynamicOS',
    'm_GlyphTable',
    'm_CharacterTable',
    'm_AtlasTextureIndex',
    'm_IsMultiAtlasTexturesEnabled',
    'm_GetFontFeatures',
    'm_ClearDynamicDataOnBuild',
    'm_AtlasWidth',
    'm_AtlasHeight',
    'm_AtlasPadding',
    'm_AtlasRenderMode',
    'm_UsedGlyphRects',
    'm_FreeGlyphRects',
    'm_FontFeatureTable',
    'm_ShouldReimportFontFeatures',
    'm_fontInfo',
    'm_glyphInfoList',
    'm_KerningTable',
    'fallbackFontAssets',
    'atlas',
)


def _replace_after_aligned_key(raw: bytes, key: str, value: bytes) -> bytes:
    needle = _aligned_string(key)
    matches = []
    start = 0
    while True:
        pos = raw.find(needle, start)
        if pos < 0:
            break
        matches.append(pos)
        start = pos + 1
    if len(matches) != 1:
        raise RuntimeError(f'Material property {key!r} occurrence count is {len(matches)}, expected 1')
    pos = matches[0] + len(needle)
    if pos + len(value) > len(raw):
        raise RuntimeError(f'Material property {key!r} runs past object boundary')
    return raw[:pos] + value + raw[pos + len(value):]


_MATERIAL_RUNTIME_FLOAT_KEYS = (
    '_GradientScale',
    '_TextureWidth',
    '_TextureHeight',
    '_ScaleRatioA',
    '_ScaleRatioB',
    '_ScaleRatioC',
    '_WeightNormal',
    '_WeightBold',
)


def _material_float_values_from_raw(raw: bytes) -> dict[str, float]:
    values: dict[str, float] = {}
    for key in _MATERIAL_RUNTIME_FLOAT_KEYS:
        needle = _aligned_string(key)
        matches: list[int] = []
        start = 0
        while True:
            pos = raw.find(needle, start)
            if pos < 0:
                break
            matches.append(pos)
            start = pos + 1
        if not matches:
            continue
        if len(matches) != 1:
            raise RuntimeError(
                f'Material property {key!r} occurrence count is {len(matches)}, expected 1'
            )
        value_pos = matches[0] + len(needle)
        if value_pos + 4 > len(raw):
            raise RuntimeError(f'Material property {key!r} runs past object boundary')
        values[key] = struct.unpack_from('<f', raw, value_pos)[0]
    return values


def _material_runtime_values(serialized, font_fields: dict) -> tuple[dict[str, int], dict[str, float]]:
    material_pid = int(font_fields['m_Material']['m_PathID'])
    if not material_pid:
        raise RuntimeError('FontAsset has no local material')
    material_obj = serialized.object_by_pid(material_pid)
    raw = serialized.data[material_obj.start:material_obj.start + material_obj.size]
    try:
        material_root = root_for_object(serialized, material_obj)
        material_fields, _, _ = parse_top_fields(
            serialized.data, material_obj.start, material_obj.size, material_root, store=True
        )
    except RuntimeError as exc:
        if 'does not contain a type tree for this type' not in str(exc):
            raise
        # Some MTGA SerializedFiles (notably sharedassets0.assets) omit the
        # Material type tree. TMP material float properties are serialized as
        # aligned string/float pairs, so the values needed for the Dynamic
        # facade can still be read safely from the object bytes.
        return {}, _material_float_values_from_raw(raw)

    saved = material_fields.get('m_SavedProperties') or {}
    tex_envs = saved.get('m_TexEnvs') or []
    floats = saved.get('m_Floats') or []
    textures = {
        str(item['size']): {
            'm_FileID': int(item['data']['m_Texture']['m_FileID']),
            'm_PathID': int(item['data']['m_Texture']['m_PathID']),
        }
        for item in tex_envs
    }
    float_values = {str(item['size']): float(item['data']) for item in floats}
    return textures, float_values


def _mono_behaviour_name_and_data_offset(raw: bytes) -> tuple[str, int] | None:
    # Unity MonoBehaviour: GameObject PPtr (12), enabled/padding (4),
    # MonoScript PPtr (12), then m_Name as an aligned string.
    if len(raw) < 32:
        return None
    name_len = struct.unpack_from('<i', raw, 28)[0]
    if name_len < 0 or 32 + name_len > len(raw):
        return None
    try:
        name = raw[32:32 + name_len].decode('utf-8')
    except UnicodeDecodeError:
        return None
    off = (32 + name_len + 3) // 4 * 4
    return name, off


def _material_map_entries(serialized, map_name: str, expected_font_pid: int) -> list[int]:
    """Read localized TMP material preset PathIDs without a MonoBehaviour type tree."""
    for obj in serialized.objects:
        if serialized.class_id(obj) != 114:
            continue
        raw = serialized.object_bytes(obj)
        parsed = _mono_behaviour_name_and_data_offset(raw)
        if parsed is None or parsed[0] != map_name:
            continue
        off = parsed[1]
        if off + 16 > len(raw):
            raise RuntimeError(f'{map_name} is truncated')
        font_file_id, font_path_id = struct.unpack_from('<iq', raw, off)
        off += 12
        if int(font_file_id) != 0 or int(font_path_id) != int(expected_font_pid):
            raise RuntimeError(f'{map_name} points to an unexpected FontAsset')
        count = struct.unpack_from('<i', raw, off)[0]
        off += 4
        if count < 0 or off + count * 12 > len(raw):
            raise RuntimeError(f'{map_name} material list is invalid')
        result: list[int] = []
        for _ in range(count):
            file_id, path_id = struct.unpack_from('<iq', raw, off)
            off += 12
            if int(file_id) != 0 or not int(path_id):
                continue
            result.append(int(path_id))
        return result
    return []


def _patch_localized_material_presets(
    target,
    *,
    target_name: str,
    target_font_pid: int,
    main_texture: dict[str, int],
    template_float_values: dict[str, float],
) -> dict[int, bytes]:
    """Update atlas-dependent values on localized TMP material presets.

    MaterialMap presets can outlive a Static->Dynamic FontAsset conversion.
    Keep their visual effect parameters (ScaleRatio/outline/underlay) intact,
    but make the atlas reference, dimensions, gradient, and weights agree with
    the Dynamic font that now owns the glyph texture.
    """
    replacements: dict[int, bytes] = {}
    for pid in _material_map_entries(target, f'{target_name}_MaterialMap', target_font_pid):
        obj = target.object_by_pid(pid)
        if target.class_id(obj) != 21:
            raise RuntimeError(f'{target_name}_MaterialMap references non-Material PathID {pid}')
        raw = target.object_bytes(obj)
        raw = _replace_after_aligned_key(
            raw, '_MainTex',
            struct.pack('<iq', int(main_texture['m_FileID']), int(main_texture['m_PathID'])),
        )
        for key in ('_GradientScale', '_TextureWidth', '_TextureHeight', '_WeightNormal', '_WeightBold'):
            if key not in template_float_values:
                continue
            try:
                raw = _replace_after_aligned_key(raw, key, struct.pack('<f', template_float_values[key]))
            except RuntimeError as exc:
                if 'occurrence count is 0' not in str(exc):
                    raise
        replacements[pid] = raw
    return replacements


def _patch_local_font_material(
    target,
    target_font_fields: dict,
    *,
    main_texture: dict[str, int],
    template_float_values: dict[str, float],
) -> tuple[int, bytes]:
    material_pid = int(target_font_fields['m_Material']['m_PathID'])
    if not material_pid or int(target_font_fields['m_Material']['m_FileID']) != 0:
        raise RuntimeError('Dynamic facade requires a local FontAsset material')
    material_obj = target.object_by_pid(material_pid)
    raw = target.data[material_obj.start:material_obj.start + material_obj.size]
    raw = _replace_after_aligned_key(
        raw,
        '_MainTex',
        struct.pack('<iq', int(main_texture['m_FileID']), int(main_texture['m_PathID'])),
    )
    # These properties depend on the SDF atlas generation settings. Keep all
    # visual styling on the target material, but align the atlas-dependent values
    # with the Dynamic template that owns the runtime texture.
    for key in (
        '_GradientScale',
        '_TextureWidth',
        '_TextureHeight',
        '_ScaleRatioA',
        '_ScaleRatioB',
        '_ScaleRatioC',
        '_WeightNormal',
        '_WeightBold',
    ):
        if key in template_float_values:
            try:
                raw = _replace_after_aligned_key(raw, key, struct.pack('<f', template_float_values[key]))
            except RuntimeError as exc:
                # Some TMP material presets omit optional properties. Only ignore
                # the genuinely absent-key case; duplicate/corrupt layouts remain fatal.
                if 'occurrence count is 0' not in str(exc):
                    raise
    return material_pid, raw


def patch_font_asset_to_dynamic_facade(
    target_cab: bytes,
    schema_root: TypeNode,
    target_name: str,
    template_cab: bytes,
    template_name: str,
    *,
    external_file_id: int,
) -> bytes:
    """Turn an existing Static TMP FontAsset into a primary Dynamic FontAsset.

    The target keeps its PathID and local Material so prefab references remain
    untouched. Its source Font and atlas Texture are borrowed from an existing
    Dynamic template in another SerializedFile. This avoids the fallback path,
    allowing TMP Bold/Italic synthesis to operate on the primary font asset.
    """
    target = parse_serialized_bytes(target_cab)
    template = parse_serialized_bytes(template_cab)

    target_obj = find_named_object(target, schema_root, target_name)
    target_fields, target_slices, target_order = parse_top_fields(
        target.data, target_obj.start, target_obj.size, schema_root, store=True
    )
    template_obj = find_named_object(template, schema_root, template_name)
    template_fields, template_slices, _ = parse_top_fields(
        template.data, template_obj.start, template_obj.size, schema_root, store=True
    )
    if int(template_fields.get('m_AtlasPopulationMode', -1)) != 1:
        raise RuntimeError(f'{template_name} is not a Dynamic TMP FontAsset')
    source = template_fields.get('m_SourceFontFile') or {}
    source_pid = int(source.get('m_PathID', 0))
    if not source_pid:
        raise RuntimeError(f'{template_name} has no source Font')
    template_atlases = template_fields.get('m_AtlasTextures') or []
    if not template_atlases:
        raise RuntimeError(f'{template_name} has no atlas Texture')

    external_source = {'m_FileID': int(external_file_id), 'm_PathID': source_pid}

    # Keep a private atlas Texture for the primary asset. Sharing the template's
    # runtime atlas between two TMP_FontAssets is unsafe because each asset owns
    # an independent glyph-packing state and could overwrite the other's rects.
    target_atlases = target_fields.get('m_AtlasTextures') or []
    if not target_atlases:
        raise RuntimeError(f'{target_name} has no local atlas Texture to repurpose')
    target_atlas = target_atlases[0]
    if int(target_atlas['m_FileID']) != 0 or not int(target_atlas['m_PathID']):
        raise RuntimeError(f'{target_name} primary atlas is not a local Texture')
    target_atlas_pid = int(target_atlas['m_PathID'])
    template_atlas_pid = int(template_atlases[0]['m_PathID'])
    if int(template_atlases[0]['m_FileID']) != 0 or not template_atlas_pid:
        raise RuntimeError(f'{template_name} Dynamic atlas is not local to its SerializedFile')
    template_atlas_obj = template.object_by_pid(template_atlas_pid)
    template_atlas_raw = template.data[
        template_atlas_obj.start:template_atlas_obj.start + template_atlas_obj.size
    ]
    local_atlases = [{'m_FileID': 0, 'm_PathID': target_atlas_pid}]

    overrides: dict[str, bytes] = {'m_FaceInfo': template_slices['m_FaceInfo']}
    for field_name in _DYNAMIC_FACADE_COPY_FIELDS:
        if field_name in target_slices and field_name in template_slices:
            overrides[field_name] = template_slices[field_name]
    overrides['m_SourceFontFile'] = struct.pack(
        '<iq', external_source['m_FileID'], external_source['m_PathID']
    )
    overrides['m_AtlasTextures'] = _serialize_pptr_array(local_atlases)

    patched_font = b''.join(overrides.get(name, target_slices[name]) for name in target_order)

    _, template_floats = _material_runtime_values(template, template_fields)
    material_pid, patched_material = _patch_local_font_material(
        target,
        target_fields,
        main_texture=local_atlases[0],
        template_float_values=template_floats,
    )
    replacements = {
        target_obj.path_id: patched_font,
        material_pid: patched_material,
        target_atlas_pid: template_atlas_raw,
    }
    replacements.update(_patch_localized_material_presets(
        target,
        target_name=target_name,
        target_font_pid=target_obj.path_id,
        main_texture=local_atlases[0],
        template_float_values=template_floats,
    ))
    rebuilt = rebuild_serialized(
        target,
        replacements,
        alignment=16,
    )
    # No alternative typeface is wanted here: Bold and Italic must be synthesized
    # on this primary Dynamic FontAsset instead of escaping to Gotham assets.
    rebuilt = patch_font_weight_refs(
        rebuilt,
        schema_root,
        target_name,
        {'m_FileID': 0, 'm_PathID': 0},
        indices=(4, 7),
        regular=True,
        italic=True,
    )

    verify = parse_serialized_bytes(rebuilt)
    vfields = parse_top_fields(
        verify.data,
        find_named_object(verify, schema_root, target_name).start,
        find_named_object(verify, schema_root, target_name).size,
        schema_root,
        store=True,
    )[0]
    if int(vfields.get('m_AtlasPopulationMode', -1)) != 1:
        raise RuntimeError(f'{target_name} Dynamic facade verification failed')
    if vfields.get('m_CharacterTable') or vfields.get('m_GlyphTable'):
        raise RuntimeError(f'{target_name} retained Static glyph data')
    if vfields.get('m_SourceFontFile') != external_source:
        raise RuntimeError(f'{target_name} source Font route verification failed')
    if (vfields.get('m_AtlasTextures') or [])[:1] != local_atlases:
        raise RuntimeError(f'{target_name} local atlas route verification failed')
    return rebuilt
