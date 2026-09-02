from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil

from kr_strict_default_route_patch import (
    build_kr_strict_default_route_patch,
    inspect_kr_strict_default_route_status,
)
from title_route_patch import build_title_route_patch, inspect_title_route_status
from mtga_font_patcher.runtime import InstallPaths, _atomic_copy, discover_installation


def build_combined_patch(
    paths: InstallPaths,
    default_font: str | Path,
    title_font: str | Path,
    output_dir: Path,
    *,
    beleren_ascii: bool = False,
    default_scale: float = 1.10,
    title_scale: float = 1.10,
    default_bold_style: float = 0.75,
    title_bold_style: float = 0.75,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = output_dir / '_kr_default_stage'
    default_outputs = build_kr_strict_default_route_patch(
        paths,
        default_font,
        stage_dir,
        default_scale=default_scale,
        default_bold_style=default_bold_style,
    )
    staged = InstallPaths(
        root=paths.root,
        resources=default_outputs['resources'],
        resources_ress=paths.resources_ress,
        fonts_bundle=default_outputs['fonts_bundle'],
        card_bundle=default_outputs['card_bundle'],
        sharedassets0=default_outputs['sharedassets0'],
        sharedassets0_ress=paths.sharedassets0_ress,
    )
    outputs = build_title_route_patch(
        staged,
        title_font,
        output_dir,
        beleren_ascii=beleren_ascii,
        title_scale=title_scale,
        title_bold_style=title_bold_style,
    )
    default_status = inspect_kr_strict_default_route_status(outputs)
    title_status = inspect_title_route_status(outputs, beleren_ascii=beleren_ascii)
    required_default = all((
        default_status['card_kr_fallbacks'] == 0,
        default_status['resources_kr_fallbacks'] == 0,
        default_status['card_kr_all_weights_zero'],
        default_status['resources_kr_all_weights_zero'],
        default_status['shared_kr_all_weights_zero'],
        default_status['fonts_kr_all_weights_zero'],
        default_status['card_kr_dynamic_empty'],
        default_status['resources_kr_dynamic_empty'],
    ))
    required_title = all((
        title_status['resources_title_router_empty'],
        title_status['card_title_router_empty'],
        title_status['resources_title_all_weights_zero'],
        title_status['card_title_all_weights_zero'],
        title_status['shared_title_all_weights_zero'],
        title_status['fonts_title_all_weights_zero'],
        title_status['resources_title_fallback_count'] == 2,
        title_status['card_title_fallback_count'] == 2,
    ))
    if not required_default or not required_title:
        raise RuntimeError(
            f'Combined structural validation failed: default={default_status}, title={title_status}'
        )
    report = {
        'mode': 'combined-kr-default-title',
        'default_font': str(Path(default_font).expanduser().resolve()),
        'title_font': str(Path(title_font).expanduser().resolve()),
        'default_scale': default_scale,
        'title_scale': title_scale,
        'default_bold_style': default_bold_style,
        'title_bold_style': title_bold_style,
        'beleren_ascii': bool(beleren_ascii),
        'default_status': default_status,
        'title_status': title_status,
    }
    report_path = output_dir / 'combined_report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    outputs['combined_report'] = report_path
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
    (backup_dir / 'backup.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return backup_dir


def apply_combined_patch(
    gamepath: str | Path,
    default_font: str | Path,
    title_font: str | Path,
    *,
    beleren_ascii: bool = False,
    default_scale: float = 1.10,
    title_scale: float = 1.10,
    default_bold_style: float = 0.75,
    title_bold_style: float = 0.75,
    build_only: bool = False,
) -> dict:
    paths = discover_installation(Path(gamepath))
    work_root = Path(__file__).resolve().parent / 'MTGASelectiveFontPatch_Work'
    run_dir = work_root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    outputs = build_combined_patch(
        paths,
        default_font,
        title_font,
        run_dir,
        beleren_ascii=beleren_ascii,
        default_scale=default_scale,
        title_scale=title_scale,
        default_bold_style=default_bold_style,
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
        default_status = inspect_kr_strict_default_route_status(installed)
        title_status = inspect_title_route_status(installed, beleren_ascii=beleren_ascii)
    else:
        default_status = inspect_kr_strict_default_route_status(outputs)
        title_status = inspect_title_route_status(outputs, beleren_ascii=beleren_ascii)
    return {
        'work_dir': str(run_dir),
        'backup_dir': str(backup) if backup else None,
        'build_only': build_only,
        'beleren_ascii': bool(beleren_ascii),
        'default_status': default_status,
        'title_status': title_status,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='MTGA Korean Default + Title patch')
    ap.add_argument('--gamepath', required=True)
    ap.add_argument('--default-font', required=True)
    ap.add_argument('--title-font', required=True)
    ap.add_argument('--default-scale', type=float, default=1.10)
    ap.add_argument('--title-scale', type=float, default=1.10)
    ap.add_argument('--default-bold-style', type=float, default=0.75)
    ap.add_argument('--title-bold-style', type=float, default=0.75)
    ap.add_argument('--beleren-ascii', action='store_true')
    ap.add_argument('--build-only', action='store_true')
    args = ap.parse_args()
    result = apply_combined_patch(
        args.gamepath,
        args.default_font,
        args.title_font,
        beleren_ascii=args.beleren_ascii,
        default_scale=args.default_scale,
        title_scale=args.title_scale,
        default_bold_style=args.default_bold_style,
        title_bold_style=args.title_bold_style,
        build_only=args.build_only,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
