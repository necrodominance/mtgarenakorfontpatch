from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zlib

from mtga_font_patcher.discovery import cab_member, ress_member, find_named_object
from mtga_font_patcher.dynamic_fonts import patch_dynamic_font_from_bytes, patch_font_asset_to_dynamic_facade
from mtga_font_patcher.embedded_schema import unity_font_source_root
from mtga_font_patcher.font_routing import patch_font_weight_refs
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

BASE_DEFAULT_TARGET = 'Font_Default'
UI_DEFAULT_TARGET = 'Font_Default_KR'
DYN_DEFAULT_PRIMARY_TARGET = 'Font_Default_DynamicFallback'
DYN_DEFAULT_TARGET = 'Font_Default_KR_DynamicFallback'


def _object_pptr(serialized, root, name: str) -> dict[str, int]:
    obj = find_named_object(serialized, root, name)
    return {'m_FileID': 0, 'm_PathID': int(obj.path_id)}


def _patch_default_dynamic_container(
    raw: bytes,
    font_root,
    font_bytes: bytes,
    *,
    family: str,
    style: str,
    default_scale: float,
) -> bytes:
    source_root = unity_font_source_root()
    out = patch_dynamic_font_from_bytes(
        raw,
        font_root,
        DYN_DEFAULT_PRIMARY_TARGET,
        font_bytes,
        family=family,
        style=style,
        font_scale=default_scale,
        source_schema_root=source_root,
    )
    out = patch_font_weight_refs(
        out,
        font_root,
        DYN_DEFAULT_PRIMARY_TARGET,
        {'m_FileID': 0, 'm_PathID': 0},
        indices=(4, 7),
        regular=True,
        italic=True,
    )
    out = patch_dynamic_font_from_bytes(
        out,
        font_root,
        DYN_DEFAULT_TARGET,
        font_bytes,
        family=family,
        style=style,
        font_scale=default_scale,
        source_schema_root=source_root,
    )
    out = patch_font_weight_refs(
        out,
        font_root,
        DYN_DEFAULT_TARGET,
        {'m_FileID': 0, 'm_PathID': 0},
        indices=(4, 7),
        regular=True,
        italic=True,
    )
    return out


def _patch_resources_default(resources_raw: bytes, patched_shared: bytes, font_root) -> bytes:
    resources_sf = parse_serialized_bytes(resources_raw)
    shared_sf = parse_serialized_bytes(patched_shared)
    dyn_obj = find_named_object(shared_sf, font_root, DYN_DEFAULT_TARGET)
    ui_fields = _font_fields(resources_sf, font_root, UI_DEFAULT_TARGET)
    route = _existing_fallback_pptr_by_path_id(ui_fields, dyn_obj.path_id)
    shared_file_id = int(route['m_FileID'])

    out = resources_raw
    # Match v1.0.4: Font_Default is optional in resources.assets.
    try:
        find_named_object(resources_sf, font_root, BASE_DEFAULT_TARGET)
    except RuntimeError:
        pass
    else:
        out = patch_font_asset_to_dynamic_facade(
            out,
            font_root,
            BASE_DEFAULT_TARGET,
            patched_shared,
            DYN_DEFAULT_PRIMARY_TARGET,
            external_file_id=shared_file_id,
        )
    out = patch_font_asset_to_dynamic_facade(
        out,
        font_root,
        UI_DEFAULT_TARGET,
        patched_shared,
        DYN_DEFAULT_TARGET,
        external_file_id=shared_file_id,
    )
    return out


def _patch_card_default(card_cab_raw: bytes, patched_fonts_cab: bytes, font_root) -> bytes:
    card_sf = parse_serialized_bytes(card_cab_raw)
    fonts_sf = parse_serialized_bytes(patched_fonts_cab)
    primary_obj = find_named_object(fonts_sf, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    kr_obj = find_named_object(fonts_sf, font_root, DYN_DEFAULT_TARGET)

    base_fields = _font_fields(card_sf, font_root, BASE_DEFAULT_TARGET)
    kr_fields = _font_fields(card_sf, font_root, UI_DEFAULT_TARGET)
    base_route = _existing_fallback_pptr_by_path_id(base_fields, primary_obj.path_id)
    kr_route = _existing_fallback_pptr_by_path_id(kr_fields, kr_obj.path_id)

    out = patch_font_asset_to_dynamic_facade(
        card_cab_raw,
        font_root,
        BASE_DEFAULT_TARGET,
        patched_fonts_cab,
        DYN_DEFAULT_PRIMARY_TARGET,
        external_file_id=int(base_route['m_FileID']),
    )
    out = patch_font_asset_to_dynamic_facade(
        out,
        font_root,
        UI_DEFAULT_TARGET,
        patched_fonts_cab,
        DYN_DEFAULT_TARGET,
        external_file_id=int(kr_route['m_FileID']),
    )
    return out


def _font_status(raw: bytes, font_root, name: str) -> dict:
    sf = parse_serialized_bytes(raw)
    fields = _font_fields(sf, font_root, name)
    face = fields.get('m_FaceInfo') or {}
    return {
        'family': str(face.get('m_FamilyName', '')),
        'style': str(face.get('m_StyleName', '')),
        'population': int(fields.get('m_AtlasPopulationMode', -1)),
        'characters': len(fields.get('m_CharacterTable') or []),
        'glyphs': len(fields.get('m_GlyphTable') or []),
    }


def inspect_default_route_status(outputs: dict[str, Path]) -> dict:
    card_bundle = read_unityfs(outputs['card_bundle'])
    font_root, _ = _font_schema_from_card(card_bundle)
    card_cab_raw = cab_member(card_bundle).data
    fonts_cab_raw = cab_member(read_unityfs(outputs['fonts_bundle'])).data
    shared_raw = outputs['sharedassets0'].read_bytes()
    resources_raw = outputs['resources'].read_bytes()

    card_base = _font_status(card_cab_raw, font_root, BASE_DEFAULT_TARGET)
    card_kr = _font_status(card_cab_raw, font_root, UI_DEFAULT_TARGET)
    resources_kr = _font_status(resources_raw, font_root, UI_DEFAULT_TARGET)
    shared_base = _font_status(shared_raw, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    shared_kr = _font_status(shared_raw, font_root, DYN_DEFAULT_TARGET)
    fonts_base = _font_status(fonts_cab_raw, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    fonts_kr = _font_status(fonts_cab_raw, font_root, DYN_DEFAULT_TARGET)

    return {
        'card_default_router_empty': card_base['population'] == 1 and card_base['characters'] == 0 and card_base['glyphs'] == 0,
        'card_kr_router_empty': card_kr['population'] == 1 and card_kr['characters'] == 0 and card_kr['glyphs'] == 0,
        'resources_kr_router_empty': resources_kr['population'] == 1 and resources_kr['characters'] == 0 and resources_kr['glyphs'] == 0,
        'shared_default_dynamic_family': shared_base['family'],
        'shared_kr_dynamic_family': shared_kr['family'],
        'fonts_default_dynamic_family': fonts_base['family'],
        'fonts_kr_dynamic_family': fonts_kr['family'],
    }


def build_default_route_patch(
    paths: InstallPaths,
    default_font: str | Path,
    output_dir: Path,
    *,
    default_scale: float = 1.10,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if paths.sharedassets0 is None or not paths.sharedassets0.is_file():
        raise FileNotFoundError('sharedassets0.assets is required')

    font_path = Path(default_font).expanduser().resolve()
    if not font_path.is_file():
        raise FileNotFoundError(font_path)
    font_bytes = read_font_bytes(font_path)
    font_info = inspect_font_bytes(font_bytes, str(font_path))

    card_bundle = read_unityfs(paths.card_bundle)
    font_root, _ = _font_schema_from_card(card_bundle)
    card_cab = cab_member(card_bundle)
    card_ress = ress_member(card_bundle)
    if card_ress is None:
        raise RuntimeError('Card font bundle has no .resS member')

    fonts_bundle = read_unityfs(paths.fonts_bundle)
    fonts_cab = cab_member(fonts_bundle)
    fonts_ress = ress_member(fonts_bundle)
    if fonts_ress is None:
        raise RuntimeError('Fonts bundle has no .resS member')

    shared_original = paths.sharedassets0.read_bytes()
    resources_original = paths.resources.read_bytes()

    patched_shared = _patch_default_dynamic_container(
        shared_original,
        font_root,
        font_bytes,
        family=font_info.family,
        style=font_info.style,
        default_scale=default_scale,
    )
    patched_fonts_cab = _patch_default_dynamic_container(
        fonts_cab.data,
        font_root,
        font_bytes,
        family=font_info.family,
        style=font_info.style,
        default_scale=default_scale,
    )
    patched_resources = _patch_resources_default(resources_original, patched_shared, font_root)
    patched_card_cab = _patch_card_default(card_cab.data, patched_fonts_cab, font_root)

    out_resources = output_dir / 'resources.assets'
    out_resources.write_bytes(patched_resources)
    out_shared = output_dir / 'sharedassets0.assets'
    out_shared.write_bytes(patched_shared)

    out_fonts = output_dir / paths.fonts_bundle.name
    fonts_crc = expected_crc_from_filename(paths.fonts_bundle.name)
    write_unityfs(
        fonts_bundle,
        _replace_bundle_members(fonts_bundle, patched_fonts_cab, fonts_ress.data, fonts_crc),
        out_fonts,
    )

    out_card = output_dir / paths.card_bundle.name
    card_crc = expected_crc_from_filename(paths.card_bundle.name)
    write_unityfs(
        card_bundle,
        _replace_bundle_members(card_bundle, patched_card_cab, card_ress.data, card_crc),
        out_card,
    )

    outputs = {
        'resources': out_resources,
        'sharedassets0': out_shared,
        'fonts_bundle': out_fonts,
        'card_bundle': out_card,
    }

    status = inspect_default_route_status(outputs)
    required_status = (
        status['card_default_router_empty']
        and status['card_kr_router_empty']
        and status['resources_kr_router_empty']
    )
    if not required_status:
        raise RuntimeError(f'default route structural validation failed: {status}')
    expected_family = font_info.family
    for key in (
        'shared_default_dynamic_family', 'shared_kr_dynamic_family',
        'fonts_default_dynamic_family', 'fonts_kr_dynamic_family',
    ):
        if status[key] != expected_family:
            raise RuntimeError(f'{key} mismatch: {status[key]!r} != {expected_family!r}')

    for original, generated in ((paths.fonts_bundle, out_fonts), (paths.card_bundle, out_card)):
        bundle = read_unityfs(generated)
        actual = zlib.crc32(b''.join(m.data for m in bundle.members)) & 0xFFFFFFFF
        expected = expected_crc_from_filename(original.name)
        if actual != expected:
            raise RuntimeError(f'CRC mismatch {generated.name}: {actual:08x} != {expected:08x}')

    report = {
        'mode': 'default-route-test',
        'default_font': str(font_path),
        'family': font_info.family,
        'style': font_info.style,
        'sha256': hashlib.sha256(font_bytes).hexdigest(),
        'default_scale': default_scale,
        'status': status,
        'files_modified': [
            'MTGA_Data/resources.assets',
            'MTGA_Data/sharedassets0.assets',
            f'MTGA_Data/Downloads/AssetBundle/{paths.fonts_bundle.name}',
            f'MTGA_Data/Downloads/AssetBundle/{paths.card_bundle.name}',
        ],
        'loose_resS_untouched': [
            'MTGA_Data/resources.assets.resS',
            'MTGA_Data/sharedassets0.assets.resS',
        ],
    }
    report_path = output_dir / 'default_route_report.json'
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


def apply_default_route_patch(
    gamepath: str | Path,
    default_font: str | Path,
    *,
    default_scale: float = 1.10,
    build_only: bool = False,
) -> dict:
    paths = discover_installation(Path(gamepath))
    work_root = Path(__file__).resolve().parent / 'MTGASelectiveFontPatch_Work'
    run_dir = work_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    outputs = build_default_route_patch(paths, default_font, run_dir, default_scale=default_scale)
    backup = None
    if not build_only:
        backup = _backup(paths)
        _atomic_copy(outputs['resources'], paths.resources)
        _atomic_copy(outputs['sharedassets0'], paths.sharedassets0)
        _atomic_copy(outputs['fonts_bundle'], paths.fonts_bundle)
        _atomic_copy(outputs['card_bundle'], paths.card_bundle)
        installed = {
            'resources': paths.resources,
            'sharedassets0': paths.sharedassets0,
            'fonts_bundle': paths.fonts_bundle,
            'card_bundle': paths.card_bundle,
        }
        status = inspect_default_route_status(installed)
        if not (status['card_default_router_empty'] and status['card_kr_router_empty'] and status['resources_kr_router_empty']):
            raise RuntimeError(f'installed structural validation failed: {status}')
    else:
        status = inspect_default_route_status(outputs)

    return {
        'work_dir': str(run_dir),
        'backup_dir': str(backup) if backup else None,
        'build_only': build_only,
        'status': status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='MTGA v1.0.4-compatible Default route-only diagnostic patch')
    ap.add_argument('--gamepath', required=True)
    ap.add_argument('--font', required=True)
    ap.add_argument('--scale', type=float, default=1.10)
    ap.add_argument('--build-only', action='store_true')
    args = ap.parse_args()
    report = apply_default_route_patch(args.gamepath, args.font, default_scale=args.scale, build_only=args.build_only)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
