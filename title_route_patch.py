from __future__ import annotations

import argparse
from datetime import datetime
from io import BytesIO
import hashlib
import json
from pathlib import Path
import shutil
import struct
import zlib

from fontTools import subset
from fontTools.ttLib import TTFont

from mtga_font_patcher.discovery import cab_member, find_named_object, ress_member
from mtga_font_patcher.dynamic_fonts import patch_dynamic_font_from_bytes
from mtga_font_patcher.embedded_schema import unity_font_source_root
from mtga_font_patcher.font_routing import patch_font_weight_refs, _serialize_pptr_array
from mtga_font_patcher.font_sources import inspect_font_bytes, read_font_bytes
from mtga_font_patcher.runtime import (
    InstallPaths,
    _atomic_copy,
    _existing_fallback_pptr_by_path_id,
    _font_codepoints,
    _font_fields,
    _font_schema_from_card,
    _local_font_pptr,
    _replace_bundle_members,
    discover_installation,
)
from mtga_font_patcher.serialized import parse_serialized_bytes, rebuild_serialized
from mtga_font_patcher.typetree import parse_top_fields
from mtga_font_patcher.unityfs import expected_crc_from_filename, read_unityfs, write_unityfs
from font_style_options import patch_synthetic_bold_strength, validate_bold_style

UI_TITLE_TARGET = 'Font_Title_KR'
DYN_TITLE_TARGET = 'Font_Title_KR_DynamicFallback'
BELEREN_TITLE_NAME = 'Font_Title_DynamicFallback'
CARD_BELEREN_NAME = 'Font_Title'
ZERO_PPTR = {'m_FileID': 0, 'm_PathID': 0}


def _font_bytes(font: TTFont) -> bytes:
    out = BytesIO()
    font.save(out)
    return out.getvalue()


def _remove_beleren_codepoints(font_bytes: bytes, beleren_codepoints: set[int]) -> bytes:
    """Keep only glyphs Beleren cannot supply. No pipe exception in this mode."""
    font = TTFont(BytesIO(font_bytes), recalcBBoxes=False, recalcTimestamp=False)
    try:
        cmap = set((font.getBestCmap() or {}).keys())
        keep = cmap - set(int(cp) for cp in beleren_codepoints)
        options = subset.Options()
        options.layout_features = ['*']
        options.name_IDs = ['*']
        options.name_legacy = True
        options.name_languages = ['*']
        options.notdef_glyph = True
        options.notdef_outline = True
        sub = subset.Subsetter(options=options)
        sub.populate(unicodes=keep)
        sub.subset(font)
        return _font_bytes(font)
    finally:
        font.close()


def _all_weights_zero(fields: dict) -> bool:
    found = False
    for field_name in ('m_FontWeightTable', 'fontWeights'):
        table = fields.get(field_name)
        if not isinstance(table, list) or not table:
            continue
        found = True
        for pair in table:
            for key in ('regularTypeface', 'italicTypeface'):
                p = pair[key]
                if int(p['m_FileID']) or int(p['m_PathID']):
                    return False
    return found


def _zero_weight_table(table: object) -> bytes:
    if not isinstance(table, list) or len(table) != 10:
        raise RuntimeError('FontAsset weight table is not the expected 10-entry array')
    raw = bytearray(struct.pack('<i', len(table)))
    for _ in table:
        raw += struct.pack('<iq', 0, 0)
        raw += struct.pack('<iq', 0, 0)
    return bytes(raw)


def _patch_exact_title_router(raw: bytes, font_root, target_name: str, fallbacks: list[dict[str, int]]) -> bytes:
    sf = parse_serialized_bytes(raw)
    obj = find_named_object(sf, font_root, target_name)
    fields, slices, order = parse_top_fields(sf.data, obj.start, obj.size, font_root, store=True)
    overrides: dict[str, bytes] = {
        'm_CharacterTable': struct.pack('<i', 0),
        'm_GlyphTable': struct.pack('<i', 0),
        'm_FallbackFontAssetTable': _serialize_pptr_array(fallbacks),
    }
    for field_name in ('m_FontWeightTable', 'fontWeights'):
        table = fields.get(field_name)
        if isinstance(table, list) and table:
            overrides[field_name] = _zero_weight_table(table)
    patched = b''.join(overrides.get(name, slices[name]) for name in order)
    rebuilt = rebuild_serialized(sf, {obj.path_id: patched}, alignment=16)

    verify = parse_serialized_bytes(rebuilt)
    vobj = find_named_object(verify, font_root, target_name)
    vfields, _, _ = parse_top_fields(verify.data, vobj.start, vobj.size, font_root, store=True)
    if vfields.get('m_CharacterTable') or vfields.get('m_GlyphTable'):
        raise RuntimeError(f'{target_name} still has direct character/glyph data')
    got = [
        (int(p['m_FileID']), int(p['m_PathID']))
        for p in (vfields.get('m_FallbackFontAssetTable') or [])
    ]
    expected = [(int(p['m_FileID']), int(p['m_PathID'])) for p in fallbacks]
    if got != expected:
        raise RuntimeError(f'{target_name} exact fallback verification failed: {got} != {expected}')
    if not _all_weights_zero(vfields):
        raise RuntimeError(f'{target_name} alternative weight refs are not all zero')
    return rebuilt


def _patch_title_dynamic(raw: bytes, font_root, runtime_bytes: bytes, *, family: str, style: str, scale: float) -> bytes:
    # Keep the custom Dynamic Title asset self-contained. Beleren fallback is
    # attached to the outer localized router so it can point at the original
    # pre-baked Static Font_Title atlas instead of the Dynamic Beleren facade.
    out = patch_dynamic_font_from_bytes(
        raw,
        font_root,
        DYN_TITLE_TARGET,
        runtime_bytes,
        family=family,
        style=style,
        font_scale=scale,
        source_schema_root=unity_font_source_root(),
    )
    # Exact empty fallback table is important when upgrading over an older
    # test build that may have stored Dynamic Beleren here. All Beleren fallback
    # now lives on the outer localized router and points to Static Font_Title.
    return _patch_exact_title_router(out, font_root, DYN_TITLE_TARGET, [])


def _patch_resources_title(resources_raw: bytes, patched_shared: bytes, font_root) -> bytes:
    resources_sf = parse_serialized_bytes(resources_raw)
    shared_sf = parse_serialized_bytes(patched_shared)
    title_obj = find_named_object(shared_sf, font_root, DYN_TITLE_TARGET)
    static_beleren_obj = find_named_object(shared_sf, font_root, CARD_BELEREN_NAME)
    title_fields = _font_fields(resources_sf, font_root, UI_TITLE_TARGET)
    title_route = _existing_fallback_pptr_by_path_id(title_fields, title_obj.path_id)
    static_beleren_route = {
        'm_FileID': int(title_route['m_FileID']),
        'm_PathID': int(static_beleren_obj.path_id),
    }
    return _patch_exact_title_router(
        resources_raw,
        font_root,
        UI_TITLE_TARGET,
        [title_route, static_beleren_route],
    )


def _patch_card_title(card_raw: bytes, patched_fonts_cab: bytes, font_root) -> bytes:
    card_sf = parse_serialized_bytes(card_raw)
    fonts_sf = parse_serialized_bytes(patched_fonts_cab)
    title_dyn = find_named_object(fonts_sf, font_root, DYN_TITLE_TARGET)
    title_fields = _font_fields(card_sf, font_root, UI_TITLE_TARGET)
    card_title_dyn = _existing_fallback_pptr_by_path_id(title_fields, title_dyn.path_id)
    card_beleren = _local_font_pptr(card_sf, CARD_BELEREN_NAME, font_root)
    return _patch_exact_title_router(
        card_raw,
        font_root,
        UI_TITLE_TARGET,
        [card_title_dyn, card_beleren],
    )


def _font_status(raw: bytes, font_root, name: str) -> dict:
    sf = parse_serialized_bytes(raw)
    fields = _font_fields(sf, font_root, name)
    face = fields.get('m_FaceInfo') or {}
    return {
        'family': str(face.get('m_FamilyName', '')),
        'characters': len(fields.get('m_CharacterTable') or []),
        'glyphs': len(fields.get('m_GlyphTable') or []),
        'fallbacks': len(fields.get('m_FallbackFontAssetTable') or []),
        'all_weights_zero': _all_weights_zero(fields),
    }


def inspect_title_route_status(outputs: dict[str, Path], *, beleren_ascii: bool) -> dict:
    card_bundle = read_unityfs(outputs['card_bundle'])
    font_root, _ = _font_schema_from_card(card_bundle)
    card_raw = cab_member(card_bundle).data
    fonts_raw = cab_member(read_unityfs(outputs['fonts_bundle'])).data
    shared_raw = outputs['sharedassets0'].read_bytes()
    resources_raw = outputs['resources'].read_bytes()

    shared_title = _font_status(shared_raw, font_root, DYN_TITLE_TARGET)
    fonts_title = _font_status(fonts_raw, font_root, DYN_TITLE_TARGET)
    resources_title = _font_status(resources_raw, font_root, UI_TITLE_TARGET)
    card_title = _font_status(card_raw, font_root, UI_TITLE_TARGET)
    return {
        'beleren_ascii': bool(beleren_ascii),
        'shared_title_family': shared_title['family'],
        'fonts_title_family': fonts_title['family'],
        'shared_title_all_weights_zero': shared_title['all_weights_zero'],
        'fonts_title_all_weights_zero': fonts_title['all_weights_zero'],
        'resources_title_all_weights_zero': resources_title['all_weights_zero'],
        'card_title_all_weights_zero': card_title['all_weights_zero'],
        'resources_title_router_empty': resources_title['characters'] == 0 and resources_title['glyphs'] == 0,
        'card_title_router_empty': card_title['characters'] == 0 and card_title['glyphs'] == 0,
        'resources_title_fallback_count': resources_title['fallbacks'],
        'card_title_fallback_count': card_title['fallbacks'],
    }


def _rewrite_bundle(bundle, patched_cab: bytes, original_path: Path, output_path: Path) -> None:
    res = ress_member(bundle)
    if res is None:
        raise RuntimeError(f'{original_path.name} has no .resS member')
    crc = expected_crc_from_filename(original_path.name)
    write_unityfs(bundle, _replace_bundle_members(bundle, patched_cab, res.data, crc), output_path)


def build_title_route_patch(
    paths: InstallPaths,
    title_font: str | Path,
    output_dir: Path,
    *,
    beleren_ascii: bool = False,
    title_scale: float = 1.10,
    title_bold_style: float = 0.75,
) -> dict[str, Path]:
    """Patch only the Korean Title route, optionally preferring Beleren-supported glyphs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if paths.sharedassets0 is None or not paths.sharedassets0.is_file():
        raise FileNotFoundError('sharedassets0.assets is required')

    title_bold_style = validate_bold_style(title_bold_style)
    font_path = Path(title_font).expanduser().resolve()
    if not font_path.is_file():
        raise FileNotFoundError(font_path)
    title_bytes = read_font_bytes(font_path)
    info = inspect_font_bytes(title_bytes, str(font_path))

    card_bundle = read_unityfs(paths.card_bundle)
    font_root, card_sf = _font_schema_from_card(card_bundle)
    fonts_bundle = read_unityfs(paths.fonts_bundle)
    beleren_cps = _font_codepoints(card_sf, font_root, CARD_BELEREN_NAME)
    runtime_bytes = _remove_beleren_codepoints(title_bytes, beleren_cps) if beleren_ascii else title_bytes

    original_shared = paths.sharedassets0.read_bytes()
    original_resources = paths.resources.read_bytes()
    original_fonts = cab_member(fonts_bundle).data
    original_card = cab_member(card_bundle).data

    patched_shared = _patch_title_dynamic(
        original_shared, font_root, runtime_bytes,
        family=info.family, style=info.style, scale=title_scale,
    )
    patched_fonts = _patch_title_dynamic(
        original_fonts, font_root, runtime_bytes,
        family=info.family, style=info.style, scale=title_scale,
    )
    patched_resources = _patch_resources_title(original_resources, patched_shared, font_root)
    patched_card = _patch_card_title(original_card, patched_fonts, font_root)

    patched_shared = patch_synthetic_bold_strength(patched_shared, font_root, DYN_TITLE_TARGET, title_bold_style)
    patched_fonts = patch_synthetic_bold_strength(patched_fonts, font_root, DYN_TITLE_TARGET, title_bold_style)
    patched_resources = patch_synthetic_bold_strength(patched_resources, font_root, UI_TITLE_TARGET, title_bold_style)
    patched_card = patch_synthetic_bold_strength(patched_card, font_root, UI_TITLE_TARGET, title_bold_style)

    out_resources = output_dir / 'resources.assets'
    out_resources.write_bytes(patched_resources)
    out_shared = output_dir / 'sharedassets0.assets'
    out_shared.write_bytes(patched_shared)
    out_fonts = output_dir / paths.fonts_bundle.name
    _rewrite_bundle(fonts_bundle, patched_fonts, paths.fonts_bundle, out_fonts)
    out_card = output_dir / paths.card_bundle.name
    _rewrite_bundle(card_bundle, patched_card, paths.card_bundle, out_card)

    outputs = {
        'resources': out_resources,
        'sharedassets0': out_shared,
        'fonts_bundle': out_fonts,
        'card_bundle': out_card,
    }
    status = inspect_title_route_status(outputs, beleren_ascii=beleren_ascii)
    required = all((
        status['resources_title_router_empty'],
        status['card_title_router_empty'],
        status['resources_title_all_weights_zero'],
        status['card_title_all_weights_zero'],
        status['shared_title_all_weights_zero'],
        status['fonts_title_all_weights_zero'],
        status['resources_title_fallback_count'] == 2,
        status['card_title_fallback_count'] == 2,
        status['shared_title_family'] == info.family,
        status['fonts_title_family'] == info.family,
    ))
    if not required:
        raise RuntimeError(f'Title route structural validation failed: {status}')

    for key in ('fonts_bundle', 'card_bundle'):
        p = outputs[key]
        bundle = read_unityfs(p)
        actual = zlib.crc32(b''.join(m.data for m in bundle.members)) & 0xFFFFFFFF
        expected = expected_crc_from_filename(p.name)
        if actual != expected:
            raise RuntimeError(f'CRC mismatch {p.name}: {actual:08x} != {expected:08x}')

    report = {
        'mode': 'title-route-test',
        'title_font': str(font_path),
        'family': info.family,
        'style': info.style,
        'sha256': hashlib.sha256(title_bytes).hexdigest(),
        'runtime_sha256': hashlib.sha256(runtime_bytes).hexdigest(),
        'title_scale': title_scale,
        'title_bold_style': title_bold_style,
        'beleren_ascii': bool(beleren_ascii),
        'beleren_codepoints': len(beleren_cps),
        'status': status,
        'intent': (
            'Korean Title only. OFF: custom Title handles all glyphs it supports and Beleren is missing-glyph fallback. '
            'ON: all glyphs supported by original Beleren are removed from the custom runtime font so Beleren handles them.'
        ),
    }
    report_path = output_dir / 'title_route_report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    outputs['report'] = report_path
    return outputs


def _backup(paths: InstallPaths) -> Path:
    backup_root = paths.root / 'MTGA_Data' / 'MTGASelectiveFontPatch_Backups'
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup_dir = backup_root / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    targets = [paths.resources, paths.fonts_bundle, paths.card_bundle]
    if paths.sharedassets0 is not None:
        targets.append(paths.sharedassets0)
    manifest = []
    for src in targets:
        rel = src.relative_to(paths.root)
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest.append({'relative_path': str(rel).replace('\\', '/'), 'sha256': hashlib.sha256(src.read_bytes()).hexdigest()})
    (backup_dir / 'backup.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return backup_dir


def apply_title_route_patch(
    gamepath: str | Path,
    title_font: str | Path,
    *,
    beleren_ascii: bool = False,
    title_scale: float = 1.10,
    title_bold_style: float = 0.75,
    build_only: bool = False,
) -> dict:
    paths = discover_installation(Path(gamepath))
    work_root = Path(__file__).resolve().parent / 'MTGASelectiveFontPatch_Work'
    run_dir = work_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    outputs = build_title_route_patch(
        paths, title_font, run_dir,
        beleren_ascii=beleren_ascii,
        title_scale=title_scale,
        title_bold_style=title_bold_style,
    )

    backup = None
    if not build_only:
        backup = _backup(paths)
        _atomic_copy(outputs['resources'], paths.resources)
        if paths.sharedassets0 is None:
            raise RuntimeError('sharedassets0.assets disappeared before install')
        _atomic_copy(outputs['sharedassets0'], paths.sharedassets0)
        _atomic_copy(outputs['fonts_bundle'], paths.fonts_bundle)
        _atomic_copy(outputs['card_bundle'], paths.card_bundle)
        installed = {
            'resources': paths.resources,
            'sharedassets0': paths.sharedassets0,
            'fonts_bundle': paths.fonts_bundle,
            'card_bundle': paths.card_bundle,
        }
        status = inspect_title_route_status(installed, beleren_ascii=beleren_ascii)
    else:
        status = inspect_title_route_status(outputs, beleren_ascii=beleren_ascii)

    return {
        'work_dir': str(run_dir),
        'backup_dir': str(backup) if backup else None,
        'build_only': build_only,
        'beleren_ascii': bool(beleren_ascii),
        'status': status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='MTGA Korean Title route diagnostic patch')
    ap.add_argument('--gamepath', required=True)
    ap.add_argument('--font', required=True)
    ap.add_argument('--scale', type=float, default=1.10)
    ap.add_argument('--bold-style', type=float, default=0.75)
    ap.add_argument('--beleren-ascii', action='store_true', help='Prefer original Beleren for every glyph Beleren supports')
    ap.add_argument('--build-only', action='store_true')
    args = ap.parse_args()
    result = apply_title_route_patch(
        args.gamepath,
        args.font,
        beleren_ascii=args.beleren_ascii,
        title_scale=args.scale,
        title_bold_style=args.bold_style,
        build_only=args.build_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
