from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

from fontTools.ttLib import TTFont, TTLibError

from .font_routing import is_korean_codepoint


def _name_string(font: TTFont, *name_ids: int) -> str:
    table = font['name'] if 'name' in font else None
    if table is None:
        return ''
    for name_id in name_ids:
        preferred = [
            rec for rec in table.names
            if rec.nameID == name_id and rec.platformID in (0, 3)
        ]
        preferred += [rec for rec in table.names if rec.nameID == name_id and rec not in preferred]
        for rec in preferred:
            try:
                value = rec.toUnicode().strip()
            except Exception:
                continue
            if value:
                return value
    return ''


def _normalize_scale(value: float) -> float:
    value = float(value)
    if value > 10.0:
        value /= 100.0
    if not (0.50 <= value <= 2.00):
        raise ValueError('Font scale must be between 50% and 200%.')
    return value


@dataclass(frozen=True)
class FontInfo:
    path: Path
    family: str
    style: str
    postscript_name: str
    units_per_em: int
    codepoints: frozenset[int]
    variable_axes: tuple[str, ...]
    is_cff: bool

    @property
    def has_korean(self) -> bool:
        return any(is_korean_codepoint(cp) for cp in self.codepoints)

    @property
    def korean_codepoint_count(self) -> int:
        return sum(1 for cp in self.codepoints if is_korean_codepoint(cp))


@dataclass(frozen=True)
class FontSelection:
    default_path: Path
    title_path: Path
    title_bold_path: Path | None = None
    default_scale: float = 1.10
    title_scale: float = 1.10

    def __post_init__(self) -> None:
        object.__setattr__(self, 'default_path', Path(self.default_path))
        object.__setattr__(self, 'title_path', Path(self.title_path))
        if self.title_bold_path is not None:
            object.__setattr__(self, 'title_bold_path', Path(self.title_bold_path))
        object.__setattr__(self, 'default_scale', _normalize_scale(self.default_scale))
        object.__setattr__(self, 'title_scale', _normalize_scale(self.title_scale))


@dataclass(frozen=True)
class AdjacentDefaultFonts:
    default_path: Path | None
    title_path: Path | None
    title_bold_path: Path | None


def _find_font_by_stem(base_dir: Path, stems: tuple[str, ...]) -> Path | None:
    if not base_dir.is_dir():
        return None
    wanted = {stem.casefold() for stem in stems}
    for path in sorted(base_dir.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.suffix.casefold() not in ('.ttf', '.otf'):
            continue
        if path.stem.casefold() in wanted:
            return path
    return None


def find_adjacent_default_fonts(base_dir: str | Path) -> AdjacentDefaultFonts:
    base = Path(base_dir)
    return AdjacentDefaultFonts(
        default_path=_find_font_by_stem(base, ('Pretendard-Medium',)),
        title_path=_find_font_by_stem(base, ('Hahmlet-SemiBold', 'Hahmlet-Semibold')),
        title_bold_path=_find_font_by_stem(base, ('Hahmlet-Bold',)),
    )


def load_font(path: str | Path) -> TTFont:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f'Font file not found: {path}')
    try:
        return TTFont(path, lazy=False, recalcBBoxes=False, recalcTimestamp=False)
    except (TTLibError, OSError, Exception) as exc:
        # Keep one stable user-facing exception while retaining the original as cause.
        raise ValueError(f'Not a readable OpenType/TrueType font: {path}') from exc


def inspect_font(path: str | Path) -> FontInfo:
    path = Path(path)
    font = load_font(path)
    try:
        cmap = font.getBestCmap() or {}
        axes: Iterable[str] = ()
        if 'fvar' in font:
            axes = [axis.axisTag for axis in font['fvar'].axes]
        return FontInfo(
            path=path,
            family=_name_string(font, 16, 1) or path.stem,
            style=_name_string(font, 17, 2) or 'Regular',
            postscript_name=_name_string(font, 6) or path.stem.replace(' ', '-'),
            units_per_em=int(font['head'].unitsPerEm) if 'head' in font else 1000,
            codepoints=frozenset(int(cp) for cp in cmap),
            variable_axes=tuple(axes),
            is_cff=('CFF ' in font or 'CFF2' in font),
        )
    finally:
        font.close()




def inspect_font_bytes(font_bytes: bytes, label: str = '<memory>') -> FontInfo:
    font = TTFont(BytesIO(font_bytes), lazy=False, recalcBBoxes=False, recalcTimestamp=False)
    try:
        cmap = font.getBestCmap() or {}
        axes = tuple(axis.axisTag for axis in font['fvar'].axes) if 'fvar' in font else ()
        return FontInfo(
            path=Path(label),
            family=_name_string(font, 16, 1) or Path(label).stem,
            style=_name_string(font, 17, 2) or 'Regular',
            postscript_name=_name_string(font, 6) or Path(label).stem.replace(' ', '-'),
            units_per_em=int(font['head'].unitsPerEm) if 'head' in font else 1000,
            codepoints=frozenset(int(cp) for cp in cmap),
            variable_axes=axes,
            is_cff=('CFF ' in font or 'CFF2' in font),
        )
    finally:
        font.close()

def read_font_bytes(path: str | Path) -> bytes:
    path = Path(path)
    # Validate with fontTools first so arbitrary data never reaches the MTGA serializer.
    font = load_font(path)
    font.close()
    return path.read_bytes()


# --- Font derivation helpers -------------------------------------------------

from fontTools import subset
from fontTools.varLib.instancer import instantiateVariableFont



def _font_bytes(font: TTFont) -> bytes:
    out = BytesIO()
    font.save(out)
    return out.getvalue()



def make_title_runtime_bytes(font_bytes: bytes, beleren_codepoints: set[int]) -> bytes:
    """Subset a title face so Beleren keeps its supported glyphs except pipe."""
    font = TTFont(BytesIO(font_bytes), recalcBBoxes=False, recalcTimestamp=False)
    try:
        cmap = set((font.getBestCmap() or {}).keys())
        keep = {cp for cp in cmap if cp not in beleren_codepoints or cp == 0x7C}
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


def resolve_title_bold_bytes(title_bytes: bytes, explicit_bold_bytes: bytes | None) -> tuple[bytes, str]:
    if explicit_bold_bytes is not None:
        # Validate explicit bytes before returning them.
        f = TTFont(BytesIO(explicit_bold_bytes), recalcBBoxes=False, recalcTimestamp=False)
        f.close()
        return explicit_bold_bytes, 'explicit'

    font = TTFont(BytesIO(title_bytes), recalcBBoxes=False, recalcTimestamp=False)
    try:
        if 'fvar' in font and any(axis.axisTag == 'wght' for axis in font['fvar'].axes):
            instanced = instantiateVariableFont(font, {'wght': 700}, inplace=False, optimize=True)
            try:
                return _font_bytes(instanced), 'variable-700'
            finally:
                instanced.close()
        return title_bytes, 'regular'
    finally:
        font.close()
