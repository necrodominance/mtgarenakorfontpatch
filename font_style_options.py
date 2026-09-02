from __future__ import annotations

import struct

from mtga_font_patcher.discovery import find_named_object
from mtga_font_patcher.runtime import _font_fields
from mtga_font_patcher.serialized import parse_serialized_bytes, rebuild_serialized
from mtga_font_patcher.typetree import parse_top_fields


def validate_bold_style(value: float) -> float:
    value = float(value)
    if not (0.0 <= value <= 1.0):
        raise ValueError(f'boldStyle must be between 0.0 and 1.0, got {value}')
    return value


def patch_bold_style(raw: bytes, font_root, target_name: str, value: float) -> bytes:
    value = validate_bold_style(value)
    sf = parse_serialized_bytes(raw)
    obj = find_named_object(sf, font_root, target_name)
    fields, slices, order = parse_top_fields(sf.data, obj.start, obj.size, font_root, store=True)
    if 'boldStyle' not in slices:
        raise RuntimeError(f'{target_name} has no boldStyle field')
    overrides = {'boldStyle': struct.pack('<f', value)}
    patched_obj = b''.join(overrides.get(name, slices[name]) for name in order)
    rebuilt = rebuild_serialized(sf, {obj.path_id: patched_obj}, alignment=16)
    verify = parse_serialized_bytes(rebuilt)
    got = float(_font_fields(verify, font_root, target_name).get('boldStyle'))
    if abs(got - value) > 1e-6:
        raise RuntimeError(f'{target_name} boldStyle verification failed: {got} != {value}')
    return rebuilt


def patch_bold_render_weight(raw: bytes, font_root, target_name: str, value: float) -> bytes:
    """Synchronize TMP synthetic-bold rendering weight with FontAsset.boldStyle.

    Patches the FontAsset's local Material _WeightBold and any localized
    MaterialMap presets attached to that FontAsset. This mirrors TMP's own
    CreateFontAsset behavior where boldStyle seeds the material WeightBold.
    """
    from mtga_font_patcher.dynamic_fonts import (
        _material_map_entries,
        _replace_after_aligned_key,
        _material_runtime_values,
        _material_float_values_from_raw,
    )

    value = validate_bold_style(value)
    sf = parse_serialized_bytes(raw)
    font_obj = find_named_object(sf, font_root, target_name)
    fields = _font_fields(sf, font_root, target_name)
    replacements: dict[int, bytes] = {}

    material = fields.get('m_Material') or {}
    material_file_id = int(material.get('m_FileID', 0))
    material_pid = int(material.get('m_PathID', 0))
    if material_file_id != 0 or not material_pid:
        raise RuntimeError(f'{target_name} has no local material to patch')

    mat_obj = sf.object_by_pid(material_pid)
    mat_raw = sf.object_bytes(mat_obj)
    mat_raw = _replace_after_aligned_key(mat_raw, '_WeightBold', struct.pack('<f', value))
    replacements[material_pid] = mat_raw

    for pid in _material_map_entries(sf, f'{target_name}_MaterialMap', int(font_obj.path_id)):
        obj = sf.object_by_pid(pid)
        preset_raw = sf.object_bytes(obj)
        try:
            preset_raw = _replace_after_aligned_key(
                preset_raw, '_WeightBold', struct.pack('<f', value)
            )
        except RuntimeError as exc:
            if 'occurrence count is 0' in str(exc):
                continue
            raise
        replacements[pid] = preset_raw

    rebuilt = rebuild_serialized(sf, replacements, alignment=16)
    verify = parse_serialized_bytes(rebuilt)
    verify_fields = _font_fields(verify, font_root, target_name)
    _, local_floats = _material_runtime_values(verify, verify_fields)
    got = float(local_floats.get('_WeightBold', float('nan')))
    if abs(got - value) > 1e-6:
        raise RuntimeError(f'{target_name} material _WeightBold verification failed: {got} != {value}')

    for pid in _material_map_entries(verify, f'{target_name}_MaterialMap', int(font_obj.path_id)):
        preset = verify.object_by_pid(pid)
        vals = _material_float_values_from_raw(verify.object_bytes(preset))
        if '_WeightBold' in vals and abs(float(vals['_WeightBold']) - value) > 1e-6:
            raise RuntimeError(
                f'{target_name} preset {pid} _WeightBold verification failed: '
                f"{vals['_WeightBold']} != {value}"
            )
    return rebuilt


def patch_synthetic_bold_strength(raw: bytes, font_root, target_name: str, value: float) -> bytes:
    raw = patch_bold_style(raw, font_root, target_name, value)
    return patch_bold_render_weight(raw, font_root, target_name, value)
