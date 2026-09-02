from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import zlib

from default_route_patch import _object_pptr
from mtga_font_patcher.discovery import cab_member, ress_member, find_named_object
from mtga_font_patcher.dynamic_fonts import patch_dynamic_font_from_bytes, patch_font_asset_to_dynamic_facade
from mtga_font_patcher.embedded_schema import unity_font_source_root
from mtga_font_patcher.font_sources import read_font_bytes, inspect_font_bytes
from mtga_font_patcher.runtime import (
    InstallPaths,
    discover_installation,
    _font_schema_from_card,
    _font_fields,
    _existing_fallback_pptr_by_path_id,
    _replace_bundle_members,
    _atomic_copy,
)
from mtga_font_patcher.serialized import parse_serialized_bytes
from mtga_font_patcher.unityfs import expected_crc_from_filename, read_unityfs, write_unityfs
from strict_default_route_patch import _clear_font_routes, _all_weight_refs_zero
from font_style_options import patch_synthetic_bold_strength, validate_bold_style

BASE_DEFAULT_TARGET = 'Font_Default'
UI_DEFAULT_TARGET = 'Font_Default_KR'
DYN_DEFAULT_PRIMARY_TARGET = 'Font_Default_DynamicFallback'
DYN_DEFAULT_TARGET = 'Font_Default_KR_DynamicFallback'


def _patch_kr_dynamic(raw: bytes, font_root, font_bytes: bytes, *, family: str, style: str, scale: float) -> bytes:
    out = patch_dynamic_font_from_bytes(
        raw,
        font_root,
        DYN_DEFAULT_TARGET,
        font_bytes,
        family=family,
        style=style,
        font_scale=scale,
        source_schema_root=unity_font_source_root(),
    )
    # Strict route: no fallback or style-specific typeface may steal Latin digits/punctuation.
    return _clear_font_routes(out, font_root, DYN_DEFAULT_TARGET)


def _patch_resources_kr(resources_raw: bytes, patched_shared: bytes, font_root) -> bytes:
    resources_sf = parse_serialized_bytes(resources_raw)
    shared_sf = parse_serialized_bytes(patched_shared)
    dyn_obj = find_named_object(shared_sf, font_root, DYN_DEFAULT_TARGET)
    kr_fields = _font_fields(resources_sf, font_root, UI_DEFAULT_TARGET)
    route = _existing_fallback_pptr_by_path_id(kr_fields, dyn_obj.path_id)
    out = patch_font_asset_to_dynamic_facade(
        resources_raw,
        font_root,
        UI_DEFAULT_TARGET,
        patched_shared,
        DYN_DEFAULT_TARGET,
        external_file_id=int(route['m_FileID']),
    )
    return _clear_font_routes(out, font_root, UI_DEFAULT_TARGET)


def _patch_card_kr(card_cab: bytes, patched_fonts_cab: bytes, font_root) -> bytes:
    card_sf = parse_serialized_bytes(card_cab)
    fonts_sf = parse_serialized_bytes(patched_fonts_cab)
    dyn_obj = find_named_object(fonts_sf, font_root, DYN_DEFAULT_TARGET)
    kr_fields = _font_fields(card_sf, font_root, UI_DEFAULT_TARGET)
    route = _existing_fallback_pptr_by_path_id(kr_fields, dyn_obj.path_id)
    out = patch_font_asset_to_dynamic_facade(
        card_cab,
        font_root,
        UI_DEFAULT_TARGET,
        patched_fonts_cab,
        DYN_DEFAULT_TARGET,
        external_file_id=int(route['m_FileID']),
    )
    return _clear_font_routes(out, font_root, UI_DEFAULT_TARGET)


def _font_status(raw: bytes, font_root, name: str) -> dict:
    sf = parse_serialized_bytes(raw)
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


def inspect_kr_strict_default_route_status(outputs: dict[str, Path]) -> dict:
    card_bundle = read_unityfs(outputs['card_bundle'])
    font_root, _ = _font_schema_from_card(card_bundle)
    card_cab = cab_member(card_bundle).data
    fonts_cab = cab_member(read_unityfs(outputs['fonts_bundle'])).data
    shared = outputs['sharedassets0'].read_bytes()
    resources = outputs['resources'].read_bytes()

    card_kr = _font_status(card_cab, font_root, UI_DEFAULT_TARGET)
    resources_kr = _font_status(resources, font_root, UI_DEFAULT_TARGET)
    shared_kr = _font_status(shared, font_root, DYN_DEFAULT_TARGET)
    fonts_kr = _font_status(fonts_cab, font_root, DYN_DEFAULT_TARGET)
    card_default = _font_status(card_cab, font_root, BASE_DEFAULT_TARGET)
    shared_default = _font_status(shared, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    fonts_default = _font_status(fonts_cab, font_root, DYN_DEFAULT_PRIMARY_TARGET)

    return {
        'card_kr_fallbacks': card_kr['fallbacks'],
        'resources_kr_fallbacks': resources_kr['fallbacks'],
        'card_kr_all_weights_zero': card_kr['all_weights_zero'],
        'resources_kr_all_weights_zero': resources_kr['all_weights_zero'],
        'shared_kr_all_weights_zero': shared_kr['all_weights_zero'],
        'fonts_kr_all_weights_zero': fonts_kr['all_weights_zero'],
        'card_kr_dynamic_empty': card_kr['population'] == 1 and card_kr['characters'] == 0 and card_kr['glyphs'] == 0,
        'resources_kr_dynamic_empty': resources_kr['population'] == 1 and resources_kr['characters'] == 0 and resources_kr['glyphs'] == 0,
        'shared_kr_family': shared_kr['family'],
        'fonts_kr_family': fonts_kr['family'],
        'card_default_population': card_default['population'],
        'card_default_characters': card_default['characters'],
        'shared_default_family': shared_default['family'],
        'fonts_default_family': fonts_default['family'],
    }


def _rewrite_bundle(bundle, patched_cab: bytes, original_path: Path, output_path: Path) -> None:
    res = ress_member(bundle)
    if res is None:
        raise RuntimeError(f'{original_path.name} has no .resS member')
    crc = expected_crc_from_filename(original_path.name)
    write_unityfs(bundle, _replace_bundle_members(bundle, patched_cab, res.data, crc), output_path)


def build_kr_strict_default_route_patch(
    paths: InstallPaths,
    default_font: str | Path,
    output_dir: Path,
    *,
    default_scale: float = 1.10,
    default_bold_style: float = 0.75,
) -> dict[str, Path]:
    """Patch ONLY the Korean Default route; preserve English/base Default byte-for-byte."""
    output_dir.mkdir(parents=True, exist_ok=True)
    if paths.sharedassets0 is None or not paths.sharedassets0.is_file():
        raise FileNotFoundError('sharedassets0.assets is required')

    default_bold_style = validate_bold_style(default_bold_style)
    font_path = Path(default_font).expanduser().resolve()
    if not font_path.is_file():
        raise FileNotFoundError(font_path)
    font_bytes = read_font_bytes(font_path)
    info = inspect_font_bytes(font_bytes, str(font_path))

    card_bundle = read_unityfs(paths.card_bundle)
    font_root, _ = _font_schema_from_card(card_bundle)
    fonts_bundle = read_unityfs(paths.fonts_bundle)

    original_shared = paths.sharedassets0.read_bytes()
    original_resources = paths.resources.read_bytes()
    original_fonts_cab = cab_member(fonts_bundle).data
    original_card_cab = cab_member(card_bundle).data

    patched_shared = _patch_kr_dynamic(
        original_shared, font_root, font_bytes,
        family=info.family, style=info.style, scale=default_scale,
    )
    patched_fonts_cab = _patch_kr_dynamic(
        original_fonts_cab, font_root, font_bytes,
        family=info.family, style=info.style, scale=default_scale,
    )
    patched_resources = _patch_resources_kr(original_resources, patched_shared, font_root)
    patched_card_cab = _patch_card_kr(original_card_cab, patched_fonts_cab, font_root)

    # TMP synthetic Bold strength. Keep all explicit alternate typefaces cleared;
    # boldStyle controls the SDF dilation used for synthetic Bold on the KR path.
    patched_shared = patch_synthetic_bold_strength(patched_shared, font_root, DYN_DEFAULT_TARGET, default_bold_style)
    patched_fonts_cab = patch_synthetic_bold_strength(patched_fonts_cab, font_root, DYN_DEFAULT_TARGET, default_bold_style)
    patched_resources = patch_synthetic_bold_strength(patched_resources, font_root, UI_DEFAULT_TARGET, default_bold_style)
    patched_card_cab = patch_synthetic_bold_strength(patched_card_cab, font_root, UI_DEFAULT_TARGET, default_bold_style)

    out_resources = output_dir / 'resources.assets'
    out_resources.write_bytes(patched_resources)
    out_shared = output_dir / 'sharedassets0.assets'
    out_shared.write_bytes(patched_shared)
    out_fonts = output_dir / paths.fonts_bundle.name
    _rewrite_bundle(fonts_bundle, patched_fonts_cab, paths.fonts_bundle, out_fonts)
    out_card = output_dir / paths.card_bundle.name
    _rewrite_bundle(card_bundle, patched_card_cab, paths.card_bundle, out_card)

    outputs = {
        'resources': out_resources,
        'sharedassets0': out_shared,
        'fonts_bundle': out_fonts,
        'card_bundle': out_card,
    }
    status = inspect_kr_strict_default_route_status(outputs)
    if not all((
        status['card_kr_fallbacks'] == 0,
        status['resources_kr_fallbacks'] == 0,
        status['card_kr_all_weights_zero'],
        status['resources_kr_all_weights_zero'],
        status['shared_kr_all_weights_zero'],
        status['fonts_kr_all_weights_zero'],
        status['card_kr_dynamic_empty'],
        status['resources_kr_dynamic_empty'],
        status['shared_kr_family'] == info.family,
        status['fonts_kr_family'] == info.family,
    )):
        raise RuntimeError(f'KR strict route structural validation failed: {status}')

    for key in ('fonts_bundle', 'card_bundle'):
        p = outputs[key]
        bundle = read_unityfs(p)
        actual = zlib.crc32(b''.join(m.data for m in bundle.members)) & 0xFFFFFFFF
        expected = expected_crc_from_filename(p.name)
        if actual != expected:
            raise RuntimeError(f'CRC mismatch {p.name}: {actual:08x} != {expected:08x}')

    report = {
        'mode': 'kr-strict-default-route-test',
        'default_font': str(font_path),
        'family': info.family,
        'style': info.style,
        'sha256': hashlib.sha256(font_bytes).hexdigest(),
        'default_scale': default_scale,
        'default_bold_style': default_bold_style,
        'status': status,
        'intent': 'Korean Default only: preserve English/base Font_Default and its static atlas while forcing KR glyphs, including styled digits/punctuation, through the supplied Dynamic font.',
        'files_modified': [
            'MTGA_Data/resources.assets',
            'MTGA_Data/sharedassets0.assets',
            f'MTGA_Data/Downloads/AssetBundle/{paths.fonts_bundle.name}',
            f'MTGA_Data/Downloads/AssetBundle/{paths.card_bundle.name}',
        ],
    }
    report_path = output_dir / 'kr_strict_default_route_report.json'
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


def apply_kr_strict_default_route_patch(
    gamepath: str | Path,
    default_font: str | Path,
    *,
    default_scale: float = 1.10,
    default_bold_style: float = 0.75,
    build_only: bool = False,
) -> dict:
    paths = discover_installation(Path(gamepath))
    work_root = Path(__file__).resolve().parent / 'MTGASelectiveFontPatch_Work'
    run_dir = work_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    outputs = build_kr_strict_default_route_patch(
        paths, default_font, run_dir,
        default_scale=default_scale, default_bold_style=default_bold_style,
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
        status = inspect_kr_strict_default_route_status(installed)
    else:
        status = inspect_kr_strict_default_route_status(outputs)

    return {
        'work_dir': str(run_dir),
        'backup_dir': str(backup) if backup else None,
        'build_only': build_only,
        'status': status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='MTGA Korean-only strict Default route diagnostic patch')
    ap.add_argument('--gamepath', required=True)
    ap.add_argument('--font', required=True)
    ap.add_argument('--scale', type=float, default=1.10)
    ap.add_argument('--bold-style', type=float, default=0.75)
    ap.add_argument('--build-only', action='store_true')
    args = ap.parse_args()
    result = apply_kr_strict_default_route_patch(
        args.gamepath,
        args.font,
        default_scale=args.scale,
        default_bold_style=args.bold_style,
        build_only=args.build_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
