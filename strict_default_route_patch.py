from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import struct
import zlib

from default_route_patch import build_default_route_patch
from mtga_font_patcher.discovery import cab_member, ress_member, find_named_object
from mtga_font_patcher.runtime import (
    InstallPaths,
    discover_installation,
    _font_schema_from_card,
    _font_fields,
    _replace_bundle_members,
    _atomic_copy,
)
from mtga_font_patcher.serialized import parse_serialized_bytes, rebuild_serialized
from mtga_font_patcher.typetree import parse_top_fields
from mtga_font_patcher.unityfs import (
    expected_crc_from_filename,
    read_unityfs,
    write_unityfs,
)

BASE_DEFAULT_TARGET = 'Font_Default'
UI_DEFAULT_TARGET = 'Font_Default_KR'
DYN_DEFAULT_PRIMARY_TARGET = 'Font_Default_DynamicFallback'
DYN_DEFAULT_TARGET = 'Font_Default_KR_DynamicFallback'


def _zero_weight_table(existing: object) -> bytes:
    if not isinstance(existing, list) or len(existing) != 10:
        raise RuntimeError('FontAsset weight table is not the expected 10-entry array')
    raw = bytearray(struct.pack('<i', 10))
    for _ in range(10):
        raw += struct.pack('<iq', 0, 0)  # regularTypeface
        raw += struct.pack('<iq', 0, 0)  # italicTypeface
    return bytes(raw)


def _clear_font_routes(raw: bytes, font_root, target_name: str) -> bytes:
    """Clear every explicit fallback and alternative typeface on one TMP FontAsset."""
    sf = parse_serialized_bytes(raw)
    obj = find_named_object(sf, font_root, target_name)
    fields, slices, order = parse_top_fields(sf.data, obj.start, obj.size, font_root, store=True)

    overrides: dict[str, bytes] = {}
    if 'm_FallbackFontAssetTable' in slices:
        table = fields.get('m_FallbackFontAssetTable')
        if not isinstance(table, list):
            raise RuntimeError(f'{target_name} fallback table is unavailable')
        overrides['m_FallbackFontAssetTable'] = struct.pack('<i', 0)

    for field_name in ('m_FontWeightTable', 'fontWeights'):
        if field_name in slices:
            table = fields.get(field_name)
            if isinstance(table, list) and table:
                overrides[field_name] = _zero_weight_table(table)

    if not overrides:
        raise RuntimeError(f'{target_name} has no fallback/weight routes to clear')

    rebuilt_obj = b''.join(overrides.get(name, slices[name]) for name in order)
    rebuilt = rebuild_serialized(sf, {obj.path_id: rebuilt_obj}, alignment=16)

    verify = parse_serialized_bytes(rebuilt)
    vfields = _font_fields(verify, font_root, target_name)
    if vfields.get('m_FallbackFontAssetTable'):
        raise RuntimeError(f'{target_name} fallback table was not cleared')
    for field_name in ('m_FontWeightTable', 'fontWeights'):
        table = vfields.get(field_name)
        if not isinstance(table, list) or not table:
            continue
        for pair in table:
            for key in ('regularTypeface', 'italicTypeface'):
                p = pair[key]
                if int(p['m_FileID']) != 0 or int(p['m_PathID']) != 0:
                    raise RuntimeError(f'{target_name} {field_name} still has alternative typeface routes')
    return rebuilt


def _all_weight_refs_zero(fields: dict) -> bool:
    found = False
    for field_name in ('m_FontWeightTable', 'fontWeights'):
        table = fields.get(field_name)
        if not isinstance(table, list) or not table:
            continue
        found = True
        for pair in table:
            for key in ('regularTypeface', 'italicTypeface'):
                p = pair[key]
                if int(p['m_FileID']) != 0 or int(p['m_PathID']) != 0:
                    return False
    return found


def _status_one(sf, font_root, name: str) -> dict:
    fields = _font_fields(sf, font_root, name)
    face = fields.get('m_FaceInfo') or {}
    return {
        'family': str(face.get('m_FamilyName', '')),
        'population': int(fields.get('m_AtlasPopulationMode', -1)),
        'characters': len(fields.get('m_CharacterTable') or []),
        'glyphs': len(fields.get('m_GlyphTable') or []),
        'fallbacks': len(fields.get('m_FallbackFontAssetTable') or []),
        'all_weights_zero': _all_weight_refs_zero(fields),
    }


def inspect_strict_default_route_status(outputs: dict[str, Path]) -> dict:
    card_bundle = read_unityfs(outputs['card_bundle'])
    font_root, card_sf = _font_schema_from_card(card_bundle)
    fonts_sf = parse_serialized_bytes(cab_member(read_unityfs(outputs['fonts_bundle'])).data)
    shared_sf = parse_serialized_bytes(outputs['sharedassets0'].read_bytes())
    resources_sf = parse_serialized_bytes(outputs['resources'].read_bytes())

    card_default = _status_one(card_sf, font_root, BASE_DEFAULT_TARGET)
    card_kr = _status_one(card_sf, font_root, UI_DEFAULT_TARGET)
    resources_kr = _status_one(resources_sf, font_root, UI_DEFAULT_TARGET)
    shared_default = _status_one(shared_sf, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    shared_kr = _status_one(shared_sf, font_root, DYN_DEFAULT_TARGET)
    fonts_default = _status_one(fonts_sf, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    fonts_kr = _status_one(fonts_sf, font_root, DYN_DEFAULT_TARGET)

    return {
        'card_default_fallbacks': card_default['fallbacks'],
        'card_kr_fallbacks': card_kr['fallbacks'],
        'resources_kr_fallbacks': resources_kr['fallbacks'],
        'card_default_all_weights_zero': card_default['all_weights_zero'],
        'card_kr_all_weights_zero': card_kr['all_weights_zero'],
        'resources_kr_all_weights_zero': resources_kr['all_weights_zero'],
        'shared_default_all_weights_zero': shared_default['all_weights_zero'],
        'shared_kr_all_weights_zero': shared_kr['all_weights_zero'],
        'fonts_default_all_weights_zero': fonts_default['all_weights_zero'],
        'fonts_kr_all_weights_zero': fonts_kr['all_weights_zero'],
        'card_default_dynamic_empty': card_default['population'] == 1 and card_default['characters'] == 0 and card_default['glyphs'] == 0,
        'card_kr_dynamic_empty': card_kr['population'] == 1 and card_kr['characters'] == 0 and card_kr['glyphs'] == 0,
        'resources_kr_dynamic_empty': resources_kr['population'] == 1 and resources_kr['characters'] == 0 and resources_kr['glyphs'] == 0,
        'shared_default_family': shared_default['family'],
        'shared_kr_family': shared_kr['family'],
        'fonts_default_family': fonts_default['family'],
        'fonts_kr_family': fonts_kr['family'],
    }


def _rewrite_bundle_with_cab(bundle_path: Path, patched_cab: bytes) -> None:
    bundle = read_unityfs(bundle_path)
    res = ress_member(bundle)
    if res is None:
        raise RuntimeError(f'{bundle_path.name} has no .resS member')
    crc = expected_crc_from_filename(bundle_path.name)
    tmp = bundle_path.with_suffix(bundle_path.suffix + '.strict.tmp')
    write_unityfs(bundle, _replace_bundle_members(bundle, patched_cab, res.data, crc), tmp)
    tmp.replace(bundle_path)


def build_strict_default_route_patch(
    paths: InstallPaths,
    default_font: str | Path,
    output_dir: Path,
    *,
    default_scale: float = 1.10,
) -> dict[str, Path]:
    """Build v0.4's working Default route, then remove every remaining escape route.

    This is diagnostic: Pretendard (or the supplied font) becomes the sole Default
    glyph source for the affected FontAssets. No fallback FontAsset or alternative
    weight/italic typeface is permitted on the Default path.
    """
    outputs = build_default_route_patch(paths, default_font, output_dir, default_scale=default_scale)

    card_bundle = read_unityfs(outputs['card_bundle'])
    font_root, _ = _font_schema_from_card(card_bundle)

    # Loose assets.
    shared_raw = outputs['sharedassets0'].read_bytes()
    for name in (DYN_DEFAULT_PRIMARY_TARGET, DYN_DEFAULT_TARGET):
        shared_raw = _clear_font_routes(shared_raw, font_root, name)
    outputs['sharedassets0'].write_bytes(shared_raw)

    resources_raw = outputs['resources'].read_bytes()
    resources_raw = _clear_font_routes(resources_raw, font_root, UI_DEFAULT_TARGET)
    # Font_Default is optional in resources.assets; clear it too if present.
    resources_sf = parse_serialized_bytes(resources_raw)
    try:
        find_named_object(resources_sf, font_root, BASE_DEFAULT_TARGET)
    except RuntimeError:
        pass
    else:
        resources_raw = _clear_font_routes(resources_raw, font_root, BASE_DEFAULT_TARGET)
    outputs['resources'].write_bytes(resources_raw)

    # Fonts bundle.
    fonts_bundle = read_unityfs(outputs['fonts_bundle'])
    fonts_cab = cab_member(fonts_bundle).data
    for name in (DYN_DEFAULT_PRIMARY_TARGET, DYN_DEFAULT_TARGET):
        fonts_cab = _clear_font_routes(fonts_cab, font_root, name)
    _rewrite_bundle_with_cab(outputs['fonts_bundle'], fonts_cab)

    # Card bundle.
    card_bundle = read_unityfs(outputs['card_bundle'])
    card_cab = cab_member(card_bundle).data
    for name in (BASE_DEFAULT_TARGET, UI_DEFAULT_TARGET):
        card_cab = _clear_font_routes(card_cab, font_root, name)
    _rewrite_bundle_with_cab(outputs['card_bundle'], card_cab)

    status = inspect_strict_default_route_status(outputs)
    required = [
        status['card_default_fallbacks'] == 0,
        status['card_kr_fallbacks'] == 0,
        status['resources_kr_fallbacks'] == 0,
        status['card_default_all_weights_zero'],
        status['card_kr_all_weights_zero'],
        status['resources_kr_all_weights_zero'],
        status['shared_default_all_weights_zero'],
        status['shared_kr_all_weights_zero'],
        status['fonts_default_all_weights_zero'],
        status['fonts_kr_all_weights_zero'],
        status['card_default_dynamic_empty'],
        status['card_kr_dynamic_empty'],
        status['resources_kr_dynamic_empty'],
    ]
    if not all(required):
        raise RuntimeError(f'strict Default route structural validation failed: {status}')

    # Fresh CRC validation after strict CAB edits.
    for key in ('fonts_bundle', 'card_bundle'):
        p = outputs[key]
        bundle = read_unityfs(p)
        actual = zlib.crc32(b''.join(m.data for m in bundle.members)) & 0xFFFFFFFF
        expected = expected_crc_from_filename(p.name)
        if actual != expected:
            raise RuntimeError(f'CRC mismatch {p.name}: {actual:08x} != {expected:08x}')

    report = {
        'mode': 'strict-default-route-test',
        'default_font': str(Path(default_font).expanduser().resolve()),
        'default_scale': default_scale,
        'status': status,
        'diagnostic_intent': 'No Default fallback or alternative typeface route remains; all Default glyphs must come from the supplied Dynamic font.',
        'files_modified': [
            'MTGA_Data/resources.assets',
            'MTGA_Data/sharedassets0.assets',
            f'MTGA_Data/Downloads/AssetBundle/{paths.fonts_bundle.name}',
            f'MTGA_Data/Downloads/AssetBundle/{paths.card_bundle.name}',
        ],
    }
    report_path = output_dir / 'strict_default_route_report.json'
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
        manifest.append({
            'relative_path': str(rel).replace('\\', '/'),
            'sha256': hashlib.sha256(src.read_bytes()).hexdigest(),
        })
    (backup_dir / 'backup.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return backup_dir


def apply_strict_default_route_patch(
    gamepath: str | Path,
    default_font: str | Path,
    *,
    default_scale: float = 1.10,
    build_only: bool = False,
) -> dict:
    paths = discover_installation(Path(gamepath))
    work_root = Path(__file__).resolve().parent / 'MTGASelectiveFontPatch_Work'
    run_dir = work_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    outputs = build_strict_default_route_patch(paths, default_font, run_dir, default_scale=default_scale)

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
        status = inspect_strict_default_route_status(installed)
        if not all((
            status['card_default_fallbacks'] == 0,
            status['card_kr_fallbacks'] == 0,
            status['resources_kr_fallbacks'] == 0,
            status['card_default_all_weights_zero'],
            status['card_kr_all_weights_zero'],
            status['resources_kr_all_weights_zero'],
        )):
            raise RuntimeError(f'installed strict structural validation failed: {status}')
    else:
        status = inspect_strict_default_route_status(outputs)

    return {
        'work_dir': str(run_dir),
        'backup_dir': str(backup) if backup else None,
        'build_only': build_only,
        'status': status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='MTGA strict Default route diagnostic patch')
    ap.add_argument('--gamepath', required=True)
    ap.add_argument('--font', required=True)
    ap.add_argument('--scale', type=float, default=1.10)
    ap.add_argument('--build-only', action='store_true')
    args = ap.parse_args()
    result = apply_strict_default_route_patch(
        args.gamepath,
        args.font,
        default_scale=args.scale,
        build_only=args.build_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
