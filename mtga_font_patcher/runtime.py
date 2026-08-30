from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import shutil
import zlib

from .discovery import (
    active_bundle_names_from_manifest,
    cab_member,
    find_bundle_by_prefix,
    find_named_object,
    locate_mtga_root,
)
from .serialized import parse_serialized_bytes
from .typetree import parse_top_fields, root_for_object
from .unityfs import BundleMember, UnityFSBundle, restore_filename_crc

UI_DEFAULT_TARGET = 'Font_Default_KR'
UI_TITLE_TARGET = 'Font_Title_KR'
DYN_DEFAULT_TARGET = 'Font_Default_KR_DynamicFallback'
DYN_TITLE_TARGET = 'Font_Title_KR_DynamicFallback'


@dataclass(frozen=True)
class InstallPaths:
    root: Path
    resources: Path
    resources_ress: Path
    fonts_bundle: Path
    card_bundle: Path
    sharedassets0: Path | None = None
    sharedassets0_ress: Path | None = None


def discover_installation(root: Path | None) -> InstallPaths:
    mtga_root = locate_mtga_root(root)
    data = mtga_root / 'MTGA_Data'
    downloads_dir = data / 'Downloads'
    assetbundle_dir = downloads_dir / 'AssetBundle'
    fonts_name, card_name, _manifest = active_bundle_names_from_manifest(downloads_dir)
    fonts_bundle = assetbundle_dir / fonts_name if fonts_name else find_bundle_by_prefix(assetbundle_dir, 'Fonts_')
    card_bundle = assetbundle_dir / card_name if card_name else find_bundle_by_prefix(assetbundle_dir, 'Bucket_Card.FieldFont_0_')
    sharedassets0 = data / 'sharedassets0.assets'
    sharedassets0_ress = data / 'sharedassets0.assets.resS'
    paths = InstallPaths(
        root=mtga_root,
        resources=data / 'resources.assets',
        resources_ress=data / 'resources.assets.resS',
        fonts_bundle=fonts_bundle,
        card_bundle=card_bundle,
        sharedassets0=sharedassets0 if sharedassets0.is_file() else None,
        sharedassets0_ress=sharedassets0_ress if sharedassets0_ress.is_file() else None,
    )
    for path in (paths.resources, paths.resources_ress, paths.fonts_bundle, paths.card_bundle):
        if not path.is_file():
            raise FileNotFoundError(path)
    return paths


def _font_schema_from_card(card_bundle: UnityFSBundle):
    card_sf = parse_serialized_bytes(cab_member(card_bundle).data)
    obj = find_named_object(card_sf, None, UI_DEFAULT_TARGET, require_type_tree=True)
    return root_for_object(card_sf, obj), card_sf


def _font_fields(serialized, schema_root, name: str) -> dict:
    obj = find_named_object(serialized, schema_root, name, require_type_tree=schema_root is None)
    root = schema_root if schema_root is not None else root_for_object(serialized, obj)
    return parse_top_fields(serialized.data, obj.start, obj.size, root, store=True)[0]


def _font_codepoints(serialized, schema_root, name: str) -> set[int]:
    fields = _font_fields(serialized, schema_root, name)
    chars = fields.get('m_CharacterTable')
    if not isinstance(chars, list):
        return set()
    return {int(item['m_Unicode']) for item in chars}


def _external_filename(serialized, file_id: int) -> str | None:
    if file_id <= 0 or file_id > len(serialized.externals):
        return None
    raw = serialized.externals[file_id - 1].path.replace('\\', '/')
    return raw.rsplit('/', 1)[-1]


def _fields_for_external_pptr(resources_sf, shared_sf, font_root, pptr: dict) -> dict | None:
    file_id = int(pptr['m_FileID'])
    if _external_filename(resources_sf, file_id) != 'sharedassets0.assets':
        return None
    try:
        obj = shared_sf.object_by_pid(int(pptr['m_PathID']))
        return parse_top_fields(shared_sf.data, obj.start, obj.size, font_root, store=True)[0]
    except Exception:
        return None


def _resolve_shared_ui_routes(resources_sf, shared_sf, font_root) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    owners = [
        (UI_TITLE_TARGET, {'beleren', 'title_dynamic'}),
        (UI_DEFAULT_TARGET, {'default_dynamic'}),
    ]
    for owner_name, wanted in owners:
        fields = _font_fields(resources_sf, font_root, owner_name)
        table = fields.get('m_FallbackFontAssetTable')
        if not isinstance(table, list):
            raise RuntimeError(f'{owner_name} fallback table is unavailable')
        for item in table:
            candidate = {'m_FileID': int(item['m_FileID']), 'm_PathID': int(item['m_PathID'])}
            target = _fields_for_external_pptr(resources_sf, shared_sf, font_root, candidate)
            if target is None:
                continue
            name = target.get('m_Name')
            family = (target.get('m_FaceInfo') or {}).get('m_FamilyName')
            if 'beleren' in wanted and (name == 'Font_Title_DynamicFallback' or family == 'Beleren2016'):
                result.setdefault('beleren', candidate)
            if 'title_dynamic' in wanted and name == DYN_TITLE_TARGET:
                result.setdefault('title_dynamic', candidate)
            if 'default_dynamic' in wanted and name == DYN_DEFAULT_TARGET:
                result.setdefault('default_dynamic', candidate)
    missing = {'beleren', 'title_dynamic', 'default_dynamic'} - result.keys()
    if missing:
        raise RuntimeError(
            'Could not resolve sharedassets0 UI font routes by name/family: '
            + ', '.join(sorted(missing))
        )
    return result


def _existing_fallback_pptr_by_path_id(fields: dict, path_id: int) -> dict[str, int]:
    table = fields.get('m_FallbackFontAssetTable')
    if not isinstance(table, list):
        raise RuntimeError('FontAsset fallback table unavailable')
    for item in table:
        if int(item['m_PathID']) == int(path_id):
            return {'m_FileID': int(item['m_FileID']), 'm_PathID': int(item['m_PathID'])}
    raise RuntimeError(f'Could not find existing fallback PPtr for PathID {path_id}')


def _local_font_pptr(serialized, name: str, schema_root=None):
    obj = find_named_object(serialized, schema_root, name, require_type_tree=schema_root is None)
    return {'m_FileID': 0, 'm_PathID': obj.path_id}


def _replace_bundle_members(
    bundle: UnityFSBundle,
    cab_data: bytes,
    ress_data: bytes,
    target_crc: int,
) -> tuple[BundleMember, ...]:
    members = []
    for member in bundle.members:
        if member.name.endswith('.resS'):
            members.append(BundleMember(member.name, member.flags, ress_data))
        else:
            members.append(BundleMember(member.name, member.flags, cab_data))
    fixed = restore_filename_crc(tuple(members), target_crc)
    payload = b''.join(m.data for m in fixed)
    if zlib.crc32(payload) & 0xFFFFFFFF != target_crc:
        raise RuntimeError('Final AssetBundle CRC verification failed')
    return fixed


def _backup_rel(paths: InstallPaths, path: Path) -> Path:
    return path.relative_to(paths.root)


def create_backup(paths: InstallPaths) -> Path:
    backup_root = paths.root / 'MTGA_Data' / 'MTGAFontPatcher_Backups'
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    targets = [paths.resources, paths.resources_ress, paths.fonts_bundle, paths.card_bundle]
    if paths.sharedassets0 is not None:
        targets.append(paths.sharedassets0)
    if paths.sharedassets0_ress is not None:
        targets.append(paths.sharedassets0_ress)
    manifest = []
    for source in targets:
        relative = _backup_rel(paths, source)
        destination = backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest.append({
            'relative_path': str(relative).replace('\\', '/'),
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        })
    (backup_dir / 'backup.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    (backup_root / 'LATEST').write_text(timestamp, encoding='ascii')
    return backup_dir


def _atomic_copy(source: Path, destination: Path) -> None:
    temp = destination.with_name(destination.name + '.mtgafontpatcher.tmp')
    shutil.copy2(source, temp)
    os.replace(temp, destination)


def latest_backup(paths: InstallPaths) -> Path:
    backup_root = paths.root / 'MTGA_Data' / 'MTGAFontPatcher_Backups'
    latest_file = backup_root / 'LATEST'
    if not latest_file.is_file():
        raise FileNotFoundError('No MTGAFontPatcher backup exists')
    backup = backup_root / latest_file.read_text(encoding='ascii').strip()
    if not backup.is_dir():
        raise FileNotFoundError(f'Backup directory not found: {backup}')
    return backup


def restore_latest(paths: InstallPaths) -> Path:
    backup = latest_backup(paths)
    manifest = json.loads((backup / 'backup.json').read_text(encoding='utf-8'))
    for item in manifest:
        relative = Path(item['relative_path'])
        source = backup / relative
        destination = paths.root / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(source, destination)
    return backup
