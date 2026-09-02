from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import hashlib
import json
import shutil
import tempfile
import zlib

from .discovery import cab_member, ress_member, find_named_object
from .dynamic_fonts import patch_dynamic_font_from_bytes, patch_font_asset_to_dynamic_facade
from .embedded_schema import unity_font_source_root
from .font_sources import (
    FontSelection,
    FontInfo,
    inspect_font,
    inspect_font_bytes,
    make_title_runtime_bytes,
    read_font_bytes,
    resolve_title_bold_bytes,
)
from .runtime import (
    InstallPaths,
    create_backup,
    _atomic_copy,
    _font_schema_from_card,
    _font_codepoints,
    _font_fields,
    _local_font_pptr,
    _existing_fallback_pptr_by_path_id,
    _replace_bundle_members,
    _resolve_shared_ui_routes,
)
from .serialized import parse_serialized_bytes
from .font_routing import (
    patch_font_weight_refs,
    patch_static_font_router,
)
from .typetree import TypeNode
from .unityfs import expected_crc_from_filename, read_unityfs, write_unityfs

VERSION = '1.0.4'

UI_DEFAULT_TARGET = 'Font_Default_KR'
UI_TITLE_TARGET = 'Font_Title_KR'
DYN_DEFAULT_TARGET = 'Font_Default_KR_DynamicFallback'
DYN_DEFAULT_PRIMARY_TARGET = 'Font_Default_DynamicFallback'
BASE_DEFAULT_TARGET = 'Font_Default'
DYN_TITLE_TARGET = 'Font_Title_KR_DynamicFallback'
BOLD_TITLE_TARGET = 'Font_Title_RU_DynamicFallback'
BELEREN_TITLE_NAME = 'Font_Title_DynamicFallback'


@dataclass(frozen=True)
class PreparedFonts:
    default_bytes: bytes
    default_info: FontInfo
    title_bytes: bytes
    title_info: FontInfo
    title_runtime_bytes: bytes
    title_bold_bytes: bytes
    title_bold_runtime_bytes: bytes
    title_bold_info: FontInfo
    title_bold_source: str


@dataclass(frozen=True)
class FinalStatus:
    default_face: tuple[str, str]
    title_face: tuple[str, str]
    title_bold_face: tuple[str, str]
    default_router_empty: bool
    title_router_empty: bool
    default_italic_routes_clear: bool
    title_bold_route_ok: bool

    @property
    def structurally_patched(self) -> bool:
        return all((
            self.default_router_empty,
            self.title_router_empty,
            self.default_italic_routes_clear,
            self.title_bold_route_ok,
        ))


def _progress(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


def prepare_fonts(
    selection: FontSelection,
    beleren_codepoints: set[int],
    *,
    progress: Callable[[str], None] | None = None,
) -> PreparedFonts:
    _progress(progress, 'Default 폰트 검사 중…')
    default_bytes = read_font_bytes(selection.default_path)
    default_info = inspect_font(selection.default_path)

    _progress(progress, 'Title 폰트 검사 및 Beleren 라우팅 준비 중…')
    title_bytes = read_font_bytes(selection.title_path)
    title_info = inspect_font(selection.title_path)
    title_runtime = make_title_runtime_bytes(title_bytes, beleren_codepoints)

    _progress(progress, 'Title Bold 준비 중…')
    explicit = read_font_bytes(selection.title_bold_path) if selection.title_bold_path else None
    bold_bytes, bold_source = resolve_title_bold_bytes(title_bytes, explicit)
    bold_info = inspect_font_bytes(bold_bytes, '<title-bold>')
    bold_runtime = make_title_runtime_bytes(bold_bytes, beleren_codepoints)
    return PreparedFonts(
        default_bytes=default_bytes,
        default_info=default_info,
        title_bytes=title_bytes,
        title_info=title_info,
        title_runtime_bytes=title_runtime,
        title_bold_bytes=bold_bytes,
        title_bold_runtime_bytes=bold_runtime,
        title_bold_info=bold_info,
        title_bold_source=bold_source,
    )


def _pptr(serialized, root: TypeNode, name: str) -> dict[str, int]:
    obj = find_named_object(serialized, root, name)
    return {'m_FileID': 0, 'm_PathID': int(obj.path_id)}


def patch_dynamic_container(
    raw: bytes,
    font_root: TypeNode,
    prepared: PreparedFonts,
    *,
    default_scale: float,
    title_scale: float,
) -> bytes:
    """Patch the Dynamic font set inside Fonts CAB or sharedassets0.assets."""
    source_root = unity_font_source_root()
    sf = parse_serialized_bytes(raw)
    title_pptr = _pptr(sf, font_root, DYN_TITLE_TARGET)
    bold_pptr = _pptr(sf, font_root, BOLD_TITLE_TARGET)
    beleren_pptr = _pptr(sf, font_root, BELEREN_TITLE_NAME)

    # Patch both Default Dynamic slots. Font_Default itself will be converted
    # into a primary Dynamic asset backed by the generic slot, while
    # Font_Default_KR uses the KR slot. Keeping separate runtime atlases avoids
    # two primary FontAssets competing for the same glyph packing state.
    out = patch_dynamic_font_from_bytes(
        raw, font_root, DYN_DEFAULT_PRIMARY_TARGET, prepared.default_bytes,
        family=prepared.default_info.family, style=prepared.default_info.style,
        font_scale=default_scale, source_schema_root=source_root,
    )
    out = patch_font_weight_refs(
        out, font_root, DYN_DEFAULT_PRIMARY_TARGET, {'m_FileID': 0, 'm_PathID': 0},
        indices=(4, 7), regular=True, italic=True,
    )
    out = patch_dynamic_font_from_bytes(
        out, font_root, DYN_DEFAULT_TARGET, prepared.default_bytes,
        family=prepared.default_info.family, style=prepared.default_info.style,
        font_scale=default_scale, source_schema_root=source_root,
    )
    out = patch_font_weight_refs(
        out, font_root, DYN_DEFAULT_TARGET, {'m_FileID': 0, 'm_PathID': 0},
        indices=(4, 7), regular=True, italic=True,
    )

    out = patch_dynamic_font_from_bytes(
        out, font_root, DYN_TITLE_TARGET, prepared.title_runtime_bytes,
        family=prepared.title_info.family, style=prepared.title_info.style,
        font_scale=title_scale, fallback_pptrs=[beleren_pptr],
        source_schema_root=source_root,
    )
    out = patch_dynamic_font_from_bytes(
        out, font_root, BOLD_TITLE_TARGET, prepared.title_bold_runtime_bytes,
        family=prepared.title_bold_info.family, style=prepared.title_bold_info.style,
        font_scale=title_scale, fallback_pptrs=[beleren_pptr],
        source_schema_root=source_root,
    )
    out = patch_font_weight_refs(
        out, font_root, DYN_TITLE_TARGET, bold_pptr,
        indices=(7,), regular=True, italic=True,
    )
    return out


def patch_resources_container(
    resources_bytes: bytes,
    shared_patched: bytes,
    font_root: TypeNode,
) -> bytes:
    resources_sf = parse_serialized_bytes(resources_bytes)
    shared_sf = parse_serialized_bytes(shared_patched)
    routes = _resolve_shared_ui_routes(resources_sf, shared_sf, font_root)
    shared_file_id = int(routes['default_dynamic']['m_FileID'])

    out = resources_bytes
    # Some builds expose Font_Default in resources.assets while others only
    # reference the card FontAsset. Convert it when present without making its
    # absence fatal.
    try:
        find_named_object(resources_sf, font_root, BASE_DEFAULT_TARGET)
    except RuntimeError:
        pass
    else:
        out = patch_font_asset_to_dynamic_facade(
            out, font_root, BASE_DEFAULT_TARGET, shared_patched,
            DYN_DEFAULT_PRIMARY_TARGET, external_file_id=shared_file_id,
        )
    out = patch_font_asset_to_dynamic_facade(
        out, font_root, UI_DEFAULT_TARGET, shared_patched,
        DYN_DEFAULT_TARGET, external_file_id=shared_file_id,
    )

    # Title keeps the existing routing design: custom title Dynamic first and
    # Beleren for the glyphs removed from the custom runtime subset.
    shared_sf = parse_serialized_bytes(shared_patched)
    bold_obj = find_named_object(shared_sf, font_root, BOLD_TITLE_TARGET)
    bold = {'m_FileID': shared_file_id, 'm_PathID': int(bold_obj.path_id)}
    out = patch_static_font_router(
        out, font_root, UI_TITLE_TARGET,
        [routes['title_dynamic'], routes['beleren']],
        font_weight_pptr=bold,
    )
    return out


def patch_card_container(
    card_cab: bytes,
    fonts_patched_cab: bytes,
    font_root: TypeNode,
) -> bytes:
    card_sf = parse_serialized_bytes(card_cab)
    fonts_sf = parse_serialized_bytes(fonts_patched_cab)
    fonts_primary_obj = find_named_object(fonts_sf, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    fonts_default_obj = find_named_object(fonts_sf, font_root, DYN_DEFAULT_TARGET)
    fonts_title_obj = find_named_object(fonts_sf, font_root, DYN_TITLE_TARGET)
    fonts_bold_obj = find_named_object(fonts_sf, font_root, BOLD_TITLE_TARGET)

    base_default_fields = _font_fields(card_sf, font_root, BASE_DEFAULT_TARGET)
    default_fields = _font_fields(card_sf, font_root, UI_DEFAULT_TARGET)
    title_fields = _font_fields(card_sf, font_root, UI_TITLE_TARGET)
    card_primary = _existing_fallback_pptr_by_path_id(
        base_default_fields, fonts_primary_obj.path_id
    )
    card_dyn_default = _existing_fallback_pptr_by_path_id(
        default_fields, fonts_default_obj.path_id
    )
    card_dyn_title = _existing_fallback_pptr_by_path_id(title_fields, fonts_title_obj.path_id)
    card_bold = {'m_FileID': int(card_dyn_title['m_FileID']), 'm_PathID': int(fonts_bold_obj.path_id)}
    card_beleren = _local_font_pptr(card_sf, 'Font_Title', font_root)

    out = patch_font_asset_to_dynamic_facade(
        card_cab, font_root, BASE_DEFAULT_TARGET, fonts_patched_cab,
        DYN_DEFAULT_PRIMARY_TARGET, external_file_id=int(card_primary['m_FileID']),
    )
    out = patch_font_asset_to_dynamic_facade(
        out, font_root, UI_DEFAULT_TARGET, fonts_patched_cab,
        DYN_DEFAULT_TARGET, external_file_id=int(card_dyn_default['m_FileID']),
    )
    out = patch_static_font_router(
        out, font_root, UI_TITLE_TARGET,
        [card_dyn_title, card_beleren],
        font_weight_pptr=card_bold,
    )
    return out


def _face(serialized, root: TypeNode, name: str) -> tuple[str, str]:
    fields = _font_fields(serialized, root, name)
    face = fields['m_FaceInfo']
    return str(face['m_FamilyName']), str(face['m_StyleName'])


def _weight_points_to(fields: dict, pptr: dict[str, int], index: int, *, italic_only: bool = False) -> bool:
    target = (int(pptr['m_FileID']), int(pptr['m_PathID']))
    for field_name in ('m_FontWeightTable', 'fontWeights'):
        table = fields.get(field_name)
        if not isinstance(table, list) or len(table) == 0:
            continue
        if len(table) <= index:
            return False
        keys = ('italicTypeface',) if italic_only else ('regularTypeface', 'italicTypeface')
        for key in keys:
            got = table[index][key]
            if (int(got['m_FileID']), int(got['m_PathID'])) != target:
                return False
    return True


def inspect_final_status(paths: InstallPaths) -> FinalStatus:
    card_bundle = read_unityfs(paths.card_bundle)
    font_root, card_sf = _font_schema_from_card(card_bundle)
    fonts_bundle = read_unityfs(paths.fonts_bundle)
    fonts_sf = parse_serialized_bytes(cab_member(fonts_bundle).data)

    bold_obj = find_named_object(fonts_sf, font_root, BOLD_TITLE_TARGET)
    bold_pptr = {'m_FileID': 0, 'm_PathID': int(bold_obj.path_id)}
    zero_pptr = {'m_FileID': 0, 'm_PathID': 0}

    primary_fields = _font_fields(fonts_sf, font_root, DYN_DEFAULT_PRIMARY_TARGET)
    default_fields = _font_fields(fonts_sf, font_root, DYN_DEFAULT_TARGET)
    title_fields = _font_fields(fonts_sf, font_root, DYN_TITLE_TARGET)
    card_base_default_fields = _font_fields(card_sf, font_root, BASE_DEFAULT_TARGET)
    card_default_fields = _font_fields(card_sf, font_root, UI_DEFAULT_TARGET)
    card_title_fields = _font_fields(card_sf, font_root, UI_TITLE_TARGET)

    return FinalStatus(
        default_face=_face(card_sf, font_root, BASE_DEFAULT_TARGET),
        title_face=_face(fonts_sf, font_root, DYN_TITLE_TARGET),
        title_bold_face=_face(fonts_sf, font_root, BOLD_TITLE_TARGET),
        default_router_empty=(
            int(card_base_default_fields.get('m_AtlasPopulationMode', -1)) == 1
            and int(card_default_fields.get('m_AtlasPopulationMode', -1)) == 1
            and not bool(card_base_default_fields.get('m_CharacterTable'))
            and not bool(card_base_default_fields.get('m_GlyphTable'))
            and not bool(card_default_fields.get('m_CharacterTable'))
            and not bool(card_default_fields.get('m_GlyphTable'))
        ),
        title_router_empty=not bool(card_title_fields.get('m_CharacterTable')),
        default_italic_routes_clear=(
            _weight_points_to(primary_fields, zero_pptr, 4)
            and _weight_points_to(primary_fields, zero_pptr, 7)
            and _weight_points_to(default_fields, zero_pptr, 4)
            and _weight_points_to(default_fields, zero_pptr, 7)
            and _weight_points_to(card_base_default_fields, zero_pptr, 4)
            and _weight_points_to(card_base_default_fields, zero_pptr, 7)
            and _weight_points_to(card_default_fields, zero_pptr, 4)
            and _weight_points_to(card_default_fields, zero_pptr, 7)
        ),
        title_bold_route_ok=_weight_points_to(title_fields, bold_pptr, 7),
    )


def build_final_patch(
    paths: InstallPaths,
    selection: FontSelection,
    output_dir: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if paths.sharedassets0 is None or not paths.sharedassets0.is_file():
        raise FileNotFoundError('sharedassets0.assets is required')

    card_bundle = read_unityfs(paths.card_bundle)
    font_root, card_sf = _font_schema_from_card(card_bundle)
    card_cab_member = cab_member(card_bundle)
    card_ress = ress_member(card_bundle)
    if card_ress is None:
        raise RuntimeError('Card font bundle has no .resS member')
    beleren_cps = _font_codepoints(card_sf, font_root, 'Font_Title')
    prepared = prepare_fonts(selection, beleren_cps, progress=progress)

    fonts_bundle = read_unityfs(paths.fonts_bundle)
    fonts_cab_member = cab_member(fonts_bundle)
    fonts_ress = ress_member(fonts_bundle)
    if fonts_ress is None:
        raise RuntimeError('Fonts bundle has no .resS member')
    fonts_original_cab = fonts_cab_member.data

    _progress(progress, 'sharedassets0 읽는 중…')
    shared_original = paths.sharedassets0.read_bytes()
    resources_original = paths.resources.read_bytes()

    _progress(progress, 'Default Dynamic 폰트 주입 중…')

    patched_shared = patch_dynamic_container(
        shared_original, font_root, prepared,
        default_scale=selection.default_scale, title_scale=selection.title_scale,
    )
    _progress(progress, 'Fonts 번들 Dynamic 폰트 주입 중…')
    patched_fonts_cab = patch_dynamic_container(
        fonts_original_cab, font_root, prepared,
        default_scale=selection.default_scale, title_scale=selection.title_scale,
    )
    _progress(progress, 'Static 폰트 라우터 수정 중…')
    patched_resources = patch_resources_container(
        resources_original, patched_shared, font_root,
    )
    _progress(progress, '카드 폰트 라우터 수정 중…')
    patched_card_cab = patch_card_container(
        card_cab_member.data, patched_fonts_cab, font_root,
    )

    _progress(progress, '패치 파일 작성 중…')
    out_resources = output_dir / 'resources.assets'
    out_resources.write_bytes(patched_resources)
    out_resources_ress = output_dir / 'resources.assets.resS'
    shutil.copy2(paths.resources_ress, out_resources_ress)

    out_shared = output_dir / 'sharedassets0.assets'
    out_shared.write_bytes(patched_shared)
    out_shared_ress = output_dir / 'sharedassets0.assets.resS'
    if paths.sharedassets0_ress and paths.sharedassets0_ress.is_file():
        shutil.copy2(paths.sharedassets0_ress, out_shared_ress)
    else:
        out_shared_ress.write_bytes(b'')

    _progress(progress, 'UnityFS 번들 재패킹 및 CRC 보정 중…')
    fonts_crc = expected_crc_from_filename(paths.fonts_bundle.name)
    out_fonts = output_dir / paths.fonts_bundle.name
    write_unityfs(
        fonts_bundle,
        _replace_bundle_members(fonts_bundle, patched_fonts_cab, fonts_ress.data, fonts_crc),
        out_fonts,
    )
    card_crc = expected_crc_from_filename(paths.card_bundle.name)
    out_card = output_dir / paths.card_bundle.name
    write_unityfs(
        card_bundle,
        _replace_bundle_members(card_bundle, patched_card_cab, card_ress.data, card_crc),
        out_card,
    )

    synthetic = InstallPaths(
        root=paths.root,
        resources=out_resources,
        resources_ress=out_resources_ress,
        fonts_bundle=out_fonts,
        card_bundle=out_card,
        sharedassets0=out_shared,
        sharedassets0_ress=out_shared_ress if out_shared_ress.stat().st_size else None,
    )
    _progress(progress, '생성 파일 구조 검증 중…')
    status = inspect_final_status(synthetic)
    if not status.structurally_patched:
        raise RuntimeError(f'Post-patch structural validation failed: {status}')

    for original, generated in ((paths.fonts_bundle, out_fonts), (paths.card_bundle, out_card)):
        bundle = read_unityfs(generated)
        actual = zlib.crc32(b''.join(member.data for member in bundle.members)) & 0xFFFFFFFF
        expected = expected_crc_from_filename(original.name)
        if actual != expected:
            raise RuntimeError(f'CRC mismatch {generated.name}: {actual:08x} != {expected:08x}')

    report = {
        'version': VERSION,
        'default_font': {'family': prepared.default_info.family, 'style': prepared.default_info.style,
                         'sha256': hashlib.sha256(prepared.default_bytes).hexdigest(),
                         'korean_codepoints': prepared.default_info.korean_codepoint_count},
        'title_font': {'family': prepared.title_info.family, 'style': prepared.title_info.style,
                       'sha256': hashlib.sha256(prepared.title_bytes).hexdigest(),
                       'korean_codepoints': prepared.title_info.korean_codepoint_count},
        'title_bold_source': prepared.title_bold_source,
        'default_scale': selection.default_scale,
        'title_scale': selection.title_scale,
        'status': status.__dict__,
    }
    report_path = output_dir / 'patch_report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return {
        'resources': out_resources,
        'resources_ress': out_resources_ress,
        'fonts_bundle': out_fonts,
        'card_bundle': out_card,
        'sharedassets0': out_shared,
        'sharedassets0_ress': out_shared_ress,
        'report': report_path,
    }


def apply_final_patch(
    paths: InstallPaths,
    selection: FontSelection,
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, FinalStatus]:
    with tempfile.TemporaryDirectory(prefix='mtga-font-patcher-final-') as tmp:
        outputs = build_final_patch(paths, selection, Path(tmp), progress=progress)
        _progress(progress, '원본 파일 백업 중…')
        backup = create_backup(paths)
        _progress(progress, '패치 파일 설치 중…')
        _atomic_copy(outputs['resources'], paths.resources)
        _atomic_copy(outputs['resources_ress'], paths.resources_ress)
        _atomic_copy(outputs['fonts_bundle'], paths.fonts_bundle)
        _atomic_copy(outputs['card_bundle'], paths.card_bundle)
        if paths.sharedassets0 is None:
            raise RuntimeError('sharedassets0 path disappeared before install')
        _atomic_copy(outputs['sharedassets0'], paths.sharedassets0)
        if paths.sharedassets0_ress and outputs['sharedassets0_ress'].stat().st_size:
            _atomic_copy(outputs['sharedassets0_ress'], paths.sharedassets0_ress)
    _progress(progress, '설치 결과 최종 검증 중…')
    status = inspect_final_status(paths)
    if not status.structurally_patched:
        raise RuntimeError('Installed files failed final structural validation')
    return backup, status
