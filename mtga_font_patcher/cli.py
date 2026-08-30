from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

from .final import VERSION, apply_final_patch, build_final_patch, inspect_final_status
from .font_sources import FontSelection, find_adjacent_default_fonts, inspect_font
from .runtime import InstallPaths, discover_installation, restore_latest


def _is_mtga_running() -> bool:
    if sys.platform != 'win32':
        return False
    try:
        result = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq MTGA.exe', '/NH'],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return 'MTGA.exe' in result.stdout
    except Exception:
        return False


def _add_font_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--default-font', type=Path, help='Default TTF/OTF path (default: Pretendard-Medium beside the EXE)')
    parser.add_argument('--title-font', type=Path, help='Title TTF/OTF path (default: Hahmlet-SemiBold beside the EXE)')
    parser.add_argument('--title-bold', type=Path, help='Title Bold TTF/OTF path (default: Hahmlet-Bold beside the EXE)')
    parser.add_argument('--default-scale', type=float, default=110.0, help='Default text scale percent (default 110)')
    parser.add_argument('--title-scale', type=float, default=110.0, help='Title text scale percent (default 110)')
    parser.add_argument('--allow-running', action='store_true', help='Patch even if MTGA.exe is running')


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='MTGAFontPatcher',
        description=f'MTG Arena font patcher v{VERSION} (Unity not required).',
    )
    parser.add_argument('--root', type=Path, help='MTGA installation root (folder containing MTGA_Data)')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('gui', help='Open the graphical interface')
    sub.add_parser('check', help='Inspect current structural patch status')
    for name, help_text in [('patch', 'Apply/reapply the selected fonts'), ('dry-run', 'Build and validate without changing MTGA')]:
        cmd = sub.add_parser(name, help=help_text)
        _add_font_args(cmd)
    restore = sub.add_parser('restore', help='Restore the latest backup')
    restore.add_argument('--allow-running', action='store_true')
    return parser


def _runtime_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _selection_from_args(args, *, base_dir: Path | None = None) -> FontSelection:
    defaults = find_adjacent_default_fonts(base_dir or _runtime_dir())
    default_path = args.default_font or defaults.default_path
    title_path = args.title_font or defaults.title_path
    title_bold_path = args.title_bold or defaults.title_bold_path
    missing = []
    if default_path is None:
        missing.append('Pretendard-Medium.ttf')
    if title_path is None:
        missing.append('Hahmlet-SemiBold.ttf')
    if missing:
        raise ValueError('기본 폰트를 찾을 수 없습니다: ' + ', '.join(missing))
    return FontSelection(
        default_path=default_path,
        title_path=title_path,
        title_bold_path=title_bold_path,
        default_scale=args.default_scale,
        title_scale=args.title_scale,
    )


def _print_font_warning(label: str, path: Path) -> None:
    info = inspect_font(path)
    print(f'{label}: {info.family} {info.style} ({info.korean_codepoint_count} Korean codepoints)')
    if not info.has_korean:
        print(f'WARNING: {label} has no Korean glyph coverage; missing glyphs will fall through to MTGA fallbacks.')


def _print_status(paths: InstallPaths) -> None:
    status = inspect_final_status(paths)
    print(f'MTGA Font Patcher v{VERSION}')
    print(f'MTGA root: {paths.root}')
    print(f'Dynamic Default: {status.default_face[0]} {status.default_face[1]}')
    print(f'Dynamic Title: {status.title_face[0]} {status.title_face[1]}')
    print(f'Title Bold: {status.title_bold_face[0]} {status.title_bold_face[1]}')
    print(f'Default static router empty: {"YES" if status.default_router_empty else "NO"}')
    print(f'Title static router empty: {"YES" if status.title_router_empty else "NO"}')
    print(f'Default Italic alternative routes clear: {"YES" if status.default_italic_routes_clear else "NO"}')
    print(f'Title weight 700 -> Bold slot: {"YES" if status.title_bold_route_ok else "NO"}')
    print(f'Structurally patched: {"YES" if status.structurally_patched else "NO"}')


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    command = args.command
    if command in (None, 'gui'):
        from .gui import run_gui
        return run_gui()
    try:
        paths = discover_installation(args.root)
        if command == 'check':
            _print_status(paths)
            return 0
        if _is_mtga_running() and not getattr(args, 'allow_running', False):
            raise RuntimeError('MTGA.exe is running. Close the game first.')
        if command == 'restore':
            backup = restore_latest(paths)
            print(f'Restored: {backup}')
            return 0

        selection = _selection_from_args(args)
        _print_font_warning('Default', selection.default_path)
        _print_font_warning('Title', selection.title_path)
        if selection.title_bold_path:
            _print_font_warning('Title Bold', selection.title_bold_path)

        if command == 'dry-run':
            with tempfile.TemporaryDirectory(prefix='mtga-font-patcher-final-dryrun-') as tmp:
                outputs = build_final_patch(paths, selection, Path(tmp))
                synthetic = InstallPaths(
                    root=paths.root,
                    resources=outputs['resources'],
                    resources_ress=outputs['resources_ress'],
                    fonts_bundle=outputs['fonts_bundle'],
                    card_bundle=outputs['card_bundle'],
                    sharedassets0=outputs['sharedassets0'],
                    sharedassets0_ress=outputs['sharedassets0_ress'] if outputs['sharedassets0_ress'].stat().st_size else None,
                )
                print('Dry-run succeeded; MTGA files were not changed.')
                _print_status(synthetic)
            return 0

        backup, _ = apply_final_patch(paths, selection)
        print(f'Patch applied. Backup: {backup}')
        _print_status(discover_installation(args.root))
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
