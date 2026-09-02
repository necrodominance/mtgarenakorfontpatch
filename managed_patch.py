from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil

from backup_manager import (
    FontPatchState,
    analyze_font_state,
    backup_root,
    ensure_pristine_backup,
    reconcile_backup_generation,
    restore_pristine_backup,
    validate_backup,
)
from combined_patch import build_combined_patch
from mtga_font_patcher.runtime import _atomic_copy, discover_installation


def _install_outputs(outputs: dict[str, Path], paths) -> None:
    if paths.sharedassets0 is None:
        raise RuntimeError('sharedassets0.assets disappeared before install')
    _atomic_copy(outputs['resources'], paths.resources)
    _atomic_copy(outputs['sharedassets0'], paths.sharedassets0)
    _atomic_copy(outputs['fonts_bundle'], paths.fonts_bundle)
    _atomic_copy(outputs['card_bundle'], paths.card_bundle)


def apply_managed_patch(
    gamepath: str | Path,
    default_font: str | Path,
    title_font: str | Path,
    *,
    beleren_ascii: bool = False,
    default_scale: float = 1.0,
    title_scale: float = 1.0,
    default_bold_style: float = 0.75,
    title_bold_style: float = 0.75,
) -> dict:
    live = discover_installation(Path(gamepath))
    state_report = analyze_font_state(live)
    source_state = state_report.state.value

    stale = reconcile_backup_generation(live, state_report)
    backup_created = False
    restored_before_patch = False

    if state_report.state is FontPatchState.PRISTINE:
        decision = ensure_pristine_backup(live, state_report)
        backup_created = decision.created
        source = live
    elif state_report.state is FontPatchState.PATCHED:
        root = backup_root(live)
        if not root.is_dir():
            raise RuntimeError(
                '현재 게임에 폰트 패치가 적용되어 있지만 이 릴리즈에서 사용할 수 있는 순정 백업이 없습니다. '
                '구형 릴리즈의 백업은 사용하지 않습니다. Steam에서 MTGA의 설치된 파일 무결성 검사를 실행해 '
                '순정 상태로 복구한 뒤 다시 패치해 주세요.'
            )
        try:
            restore_pristine_backup(live)
        except Exception as exc:
            raise RuntimeError(
                '현행 순정 백업을 안전하게 검증하거나 복구할 수 없습니다. '
                '구형 릴리즈의 백업으로 대체하지 않습니다. Steam에서 MTGA의 설치된 파일 무결성 검사를 실행해 '
                '순정 상태로 복구한 뒤 다시 패치해 주세요.'
            ) from exc
        restored_state = analyze_font_state(live)
        if restored_state.state is not FontPatchState.PRISTINE:
            raise RuntimeError(
                '순정 백업 복구 후에도 폰트 영역이 순정 상태로 확인되지 않았습니다. '
                'Steam에서 MTGA의 설치된 파일 무결성 검사를 실행한 뒤 다시 시도해 주세요.'
            )
        source = live
        restored_before_patch = True
    elif state_report.state is FontPatchState.UNKNOWN:
        raise RuntimeError(
            '현재 MTGA 폰트 영역에 이 릴리즈가 안전하게 복구할 수 없는 수정이 감지되었습니다. '
            '구형 릴리즈의 백업은 사용하지 않습니다. Steam에서 MTGA의 설치된 파일 무결성 검사를 실행해 '
            '순정 상태로 복구한 뒤 다시 패치해 주세요.'
        )
    else:
        raise RuntimeError(
            'The installed MTGA font assets are incompatible with this patcher: '
            + str(state_report.details.get('reason', 'required font assets are unavailable'))
        )

    work_root = Path(__file__).resolve().parent / 'MTGAKoreanFontPatch_Work'
    work_root.mkdir(parents=True, exist_ok=True)
    run_dir = work_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    run_dir.mkdir(parents=True, exist_ok=False)
    success = False
    try:
        outputs = build_combined_patch(
            source,
            default_font,
            title_font,
            run_dir,
            beleren_ascii=beleren_ascii,
            default_scale=default_scale,
            title_scale=title_scale,
            default_bold_style=default_bold_style,
            title_bold_style=title_bold_style,
        )
        _install_outputs(outputs, live)
        after = analyze_font_state(live)
        if after.state is not FontPatchState.PATCHED:
            raise RuntimeError(f'Post-install font state verification failed: {after.state.value}')
        success = True
        return {
            'source_state': source_state,
            'state': after.state.value,
            'backup_created': backup_created,
            'backup_deleted_as_stale': stale.deleted_stale,
            'backup_dir': str(backup_root(live)) if backup_root(live).is_dir() else None,
            'restored_before_patch': restored_before_patch,
            'beleren_ascii': bool(beleren_ascii),
        }
    finally:
        # Successful installs do not leave tens of MB of generated bundles behind.
        # Failed runs are preserved for diagnosis.
        if success:
            shutil.rmtree(run_dir, ignore_errors=True)
            try:
                work_root.rmdir()
            except OSError:
                pass


def restore_managed_backup(gamepath: str | Path) -> dict:
    paths = discover_installation(Path(gamepath))
    restored_from = restore_pristine_backup(paths)
    state = analyze_font_state(paths)
    return {
        'state': state.state.value,
        'backup_dir': str(restored_from),
    }


def inspect_managed_status(gamepath: str | Path) -> dict:
    paths = discover_installation(Path(gamepath))
    report = analyze_font_state(paths)
    reconciled = reconcile_backup_generation(paths, report)
    root = backup_root(paths)
    backup_exists = root.is_dir()
    backup_usable = False
    backup_issue = reconciled.message
    if backup_exists and not backup_issue:
        try:
            validate_backup(paths)
            backup_usable = True
        except Exception as exc:
            backup_issue = f'backup validation failed: {exc}'

    return {
        'state': report.state.value,
        'details': report.details,
        'backup_exists': backup_exists,
        'backup_usable': backup_usable,
        'backup_dir': str(root) if backup_exists else None,
        'backup_deleted_as_stale': reconciled.deleted_stale,
        'message': backup_issue,
        'fonts_bundle': paths.fonts_bundle.name,
        'card_bundle': paths.card_bundle.name,
    }
