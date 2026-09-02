from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from mtga_font_patcher.discovery import (
    active_bundle_names_from_manifest,
    cab_member,
    find_named_object,
)
from mtga_font_patcher.runtime import InstallPaths, _atomic_copy, _font_fields, _font_schema_from_card
from mtga_font_patcher.serialized import parse_serialized_bytes
from mtga_font_patcher.unityfs import read_unityfs


class FontPatchState(Enum):
    PRISTINE = 'pristine'
    PATCHED = 'patched'
    UNKNOWN = 'unknown'
    INCOMPATIBLE = 'incompatible'


@dataclass(frozen=True)
class FontStateReport:
    state: FontPatchState
    details: dict[str, Any]


@dataclass(frozen=True)
class BackupDecision:
    backup_dir: Path | None
    created: bool = False
    deleted_stale: bool = False
    message: str = ''


BACKUP_DIRNAME = 'MTGAKoreanFontPatch_Backup'
MANIFEST_NAME = 'backup_manifest.json'
MANIFEST_SCHEMA = 2


def backup_root(paths: InstallPaths) -> Path:
    return paths.root / 'MTGA_Data' / BACKUP_DIRNAME


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _object_bytes(raw: bytes, schema_root, name: str) -> bytes:
    sf = parse_serialized_bytes(raw)
    obj = find_named_object(sf, schema_root, name)
    return sf.data[obj.start:obj.start + obj.size]


def generation_identity(paths: InstallPaths) -> dict[str, Any]:
    """Fingerprint the game generation without hashing whole patched files.

    The signature uses active bundle names / latest manifest name plus font assets
    that this patch deliberately never modifies.  It therefore stays stable
    before/after our patch, while changing when MTGA ships a new font/bundle
    generation.
    """
    if paths.sharedassets0 is None or not paths.sharedassets0.is_file():
        raise RuntimeError('sharedassets0.assets is required for generation detection')

    card_bundle = read_unityfs(paths.card_bundle)
    fonts_bundle = read_unityfs(paths.fonts_bundle)
    root, _ = _font_schema_from_card(card_bundle)
    card_raw = cab_member(card_bundle).data
    fonts_raw = cab_member(fonts_bundle).data
    shared_raw = paths.sharedassets0.read_bytes()

    anchors = {
        'card:Font_Default': _sha256_bytes(_object_bytes(card_raw, root, 'Font_Default')),
        'card:Font_Title': _sha256_bytes(_object_bytes(card_raw, root, 'Font_Title')),
        'fonts:Font_Default_DynamicFallback': _sha256_bytes(
            _object_bytes(fonts_raw, root, 'Font_Default_DynamicFallback')
        ),
        'fonts:Font_Title_DynamicFallback': _sha256_bytes(
            _object_bytes(fonts_raw, root, 'Font_Title_DynamicFallback')
        ),
        'shared:Font_Default_DynamicFallback': _sha256_bytes(
            _object_bytes(shared_raw, root, 'Font_Default_DynamicFallback')
        ),
        'shared:Font_Title_DynamicFallback': _sha256_bytes(
            _object_bytes(shared_raw, root, 'Font_Title_DynamicFallback')
        ),
        'shared:Font_Title': _sha256_bytes(_object_bytes(shared_raw, root, 'Font_Title')),
    }

    downloads = paths.root / 'MTGA_Data' / 'Downloads'
    _fonts_name, _card_name, manifest_path = active_bundle_names_from_manifest(downloads)
    manifest_name = manifest_path.name if manifest_path else None
    payload = {
        'manifest_name': manifest_name,
        'fonts_bundle_name': paths.fonts_bundle.name,
        'card_bundle_name': paths.card_bundle.name,
        'anchors': anchors,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {**payload, 'signature': _sha256_bytes(canonical)}


def _weights_zero(fields: dict) -> bool:
    found = False
    for key in ('m_FontWeightTable', 'fontWeights'):
        table = fields.get(key)
        if not isinstance(table, list) or not table:
            continue
        found = True
        for pair in table:
            for face_key in ('regularTypeface', 'italicTypeface'):
                pptr = pair.get(face_key) or {}
                if int(pptr.get('m_FileID', 0)) or int(pptr.get('m_PathID', 0)):
                    return False
    return found


def _font_shape(raw: bytes, root, name: str) -> dict[str, Any]:
    sf = parse_serialized_bytes(raw)
    fields = _font_fields(sf, root, name)
    face = fields.get('m_FaceInfo') or {}
    source = fields.get('m_SourceFontFile') or {}
    return {
        'family': str(face.get('m_FamilyName', '')),
        'population': int(fields.get('m_AtlasPopulationMode', -1)),
        'characters': len(fields.get('m_CharacterTable') or []),
        'glyphs': len(fields.get('m_GlyphTable') or []),
        'fallbacks': len(fields.get('m_FallbackFontAssetTable') or []),
        'weights_zero': _weights_zero(fields),
        'source_path_id': int(source.get('m_PathID', 0)),
    }


def analyze_font_state(paths: InstallPaths) -> FontStateReport:
    try:
        if paths.sharedassets0 is None or not paths.sharedassets0.is_file():
            return FontStateReport(FontPatchState.INCOMPATIBLE, {'reason': 'sharedassets0.assets missing'})
        card_bundle = read_unityfs(paths.card_bundle)
        root, _ = _font_schema_from_card(card_bundle)
        raws = {
            'card': cab_member(card_bundle).data,
            'fonts': cab_member(read_unityfs(paths.fonts_bundle)).data,
            'resources': paths.resources.read_bytes(),
            'shared': paths.sharedassets0.read_bytes(),
        }
        shapes = {
            'card_default_kr': _font_shape(raws['card'], root, 'Font_Default_KR'),
            'resources_default_kr': _font_shape(raws['resources'], root, 'Font_Default_KR'),
            'shared_default_dyn': _font_shape(raws['shared'], root, 'Font_Default_KR_DynamicFallback'),
            'fonts_default_dyn': _font_shape(raws['fonts'], root, 'Font_Default_KR_DynamicFallback'),
            'card_title_kr': _font_shape(raws['card'], root, 'Font_Title_KR'),
            'resources_title_kr': _font_shape(raws['resources'], root, 'Font_Title_KR'),
            'shared_title_dyn': _font_shape(raws['shared'], root, 'Font_Title_KR_DynamicFallback'),
            'fonts_title_dyn': _font_shape(raws['fonts'], root, 'Font_Title_KR_DynamicFallback'),
        }
    except Exception as exc:
        return FontStateReport(
            FontPatchState.INCOMPATIBLE,
            {'reason': f'{type(exc).__name__}: {exc}'},
        )

    def static_localized(s: dict) -> bool:
        return (
            s['population'] == 0
            and s['characters'] > 0
            and s['glyphs'] > 0
            and s['fallbacks'] > 0
        )

    def empty_dynamic(s: dict) -> bool:
        return (
            s['population'] == 1
            and s['characters'] == 0
            and s['glyphs'] == 0
            and s['source_path_id'] != 0
        )

    pristine = all((
        static_localized(shapes['card_default_kr']),
        static_localized(shapes['resources_default_kr']),
        static_localized(shapes['card_title_kr']),
        static_localized(shapes['resources_title_kr']),
        empty_dynamic(shapes['shared_default_dyn']),
        empty_dynamic(shapes['fonts_default_dyn']),
        empty_dynamic(shapes['shared_title_dyn']),
        empty_dynamic(shapes['fonts_title_dyn']),
    ))
    if pristine:
        return FontStateReport(FontPatchState.PRISTINE, shapes)

    def patched_default_ui(s: dict) -> bool:
        return (
            s['population'] == 1
            and s['characters'] == 0
            and s['glyphs'] == 0
            and s['fallbacks'] == 0
            and s['weights_zero']
            and s['source_path_id'] != 0
        )

    def patched_title_ui(s: dict) -> bool:
        return (
            s['characters'] == 0
            and s['glyphs'] == 0
            and s['fallbacks'] == 2
            and s['weights_zero']
        )

    def patched_dynamic(s: dict) -> bool:
        return (
            s['population'] == 1
            and s['characters'] == 0
            and s['glyphs'] == 0
            and s['fallbacks'] == 0
            and s['weights_zero']
            and s['source_path_id'] != 0
        )

    patched = all((
        patched_default_ui(shapes['card_default_kr']),
        patched_default_ui(shapes['resources_default_kr']),
        patched_dynamic(shapes['shared_default_dyn']),
        patched_dynamic(shapes['fonts_default_dyn']),
        patched_title_ui(shapes['card_title_kr']),
        patched_title_ui(shapes['resources_title_kr']),
        patched_dynamic(shapes['shared_title_dyn']),
        patched_dynamic(shapes['fonts_title_dyn']),
    ))
    if patched:
        return FontStateReport(FontPatchState.PATCHED, shapes)

    return FontStateReport(FontPatchState.UNKNOWN, shapes)


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(path)
    data = json.loads(path.read_text(encoding='utf-8'))
    if int(data.get('schema_version', -1)) != MANIFEST_SCHEMA:
        raise RuntimeError('Unsupported backup manifest schema')
    return data


def _delete_backup(root: Path) -> None:
    if root.is_dir():
        shutil.rmtree(root)


def reconcile_backup_generation(
    paths: InstallPaths,
    state_report: FontStateReport,
    *,
    force_generation_mismatch_for_test: bool = False,
) -> BackupDecision:
    root = backup_root(paths)
    if not root.is_dir():
        return BackupDecision(None)
    try:
        manifest = _load_manifest(root)
        current = generation_identity(paths)
        mismatch = force_generation_mismatch_for_test or (
            manifest.get('generation', {}).get('signature') != current['signature']
        )
    except Exception as exc:
        # Once the live game is genuinely pristine, an unreadable/legacy backup
        # is no longer needed for recovery and must not block creation of a fresh
        # current-format backup. While the game is modified, keep it untouched.
        if state_report.state is FontPatchState.PRISTINE:
            _delete_backup(root)
            return BackupDecision(
                None,
                deleted_stale=True,
                message=f'legacy or invalid backup removed after pristine recovery: {exc}',
            )
        return BackupDecision(root, message=f'backup metadata unavailable: {exc}')

    if not mismatch:
        return BackupDecision(root)

    if state_report.state in (FontPatchState.PRISTINE, FontPatchState.PATCHED):
        _delete_backup(root)
        return BackupDecision(None, deleted_stale=True, message='stale backup removed after game generation change')

    return BackupDecision(root, message='generation differs but font state is unknown; backup kept for safety')


def _backup_targets(paths: InstallPaths) -> list[Path]:
    if paths.sharedassets0 is None:
        raise RuntimeError('sharedassets0.assets missing')
    return [paths.resources, paths.sharedassets0, paths.fonts_bundle, paths.card_bundle]


def ensure_pristine_backup(paths: InstallPaths, state_report: FontStateReport) -> BackupDecision:
    if state_report.state is not FontPatchState.PRISTINE:
        raise RuntimeError('A pristine font state is required before creating a backup')

    reconciled = reconcile_backup_generation(paths, state_report)
    root = backup_root(paths)
    if root.is_dir():
        # Existing same-generation backup is immutable.
        _load_manifest(root)
        return BackupDecision(root, created=False, deleted_stale=reconciled.deleted_stale)

    root.mkdir(parents=True, exist_ok=False)
    generation = generation_identity(paths)
    files: list[dict[str, str]] = []
    try:
        for src in _backup_targets(paths):
            rel = src.relative_to(paths.root)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files.append({
                'relative_path': str(rel).replace('\\', '/'),
                'sha256': _sha256_file(dst),
            })
        manifest = {
            'schema_version': MANIFEST_SCHEMA,
            'created_at_utc': datetime.now(timezone.utc).isoformat(),
            'generation': generation,
            'files': files,
        }
        (root / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
        )
    except Exception:
        _delete_backup(root)
        raise
    return BackupDecision(root, created=True, deleted_stale=reconciled.deleted_stale)


def validate_backup(paths: InstallPaths) -> tuple[Path, dict[str, Any]]:
    root = backup_root(paths)
    manifest = _load_manifest(root)
    for item in manifest.get('files', []):
        rel = Path(item['relative_path'])
        source = root / rel
        if not source.is_file():
            raise RuntimeError(f'Backup file missing: {rel}')
        if _sha256_file(source) != item['sha256']:
            raise RuntimeError(f'Backup file hash mismatch: {rel}')
    return root, manifest


def install_paths_from_backup(live: InstallPaths, backup_dir: Path) -> InstallPaths:
    def backed(path: Path) -> Path:
        return backup_dir / path.relative_to(live.root)

    return InstallPaths(
        root=live.root,
        resources=backed(live.resources),
        resources_ress=live.resources_ress,
        fonts_bundle=backed(live.fonts_bundle),
        card_bundle=backed(live.card_bundle),
        sharedassets0=backed(live.sharedassets0) if live.sharedassets0 else None,
        sharedassets0_ress=live.sharedassets0_ress,
    )


def restore_pristine_backup(paths: InstallPaths) -> Path:
    state = analyze_font_state(paths)
    root, manifest = validate_backup(paths)
    current_generation = generation_identity(paths)
    backup_generation = manifest.get('generation', {})
    if backup_generation.get('signature') != current_generation['signature']:
        if state.state in (FontPatchState.PRISTINE, FontPatchState.PATCHED):
            _delete_backup(root)
        raise RuntimeError('Backup is stale for the current game generation and was removed')

    for item in manifest.get('files', []):
        rel = Path(item['relative_path'])
        source = root / rel
        destination = paths.root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(source, destination)

    restored = analyze_font_state(paths)
    if restored.state is not FontPatchState.PRISTINE:
        raise RuntimeError(f'Restore completed but font state is not pristine: {restored.state.value}')
    return root
