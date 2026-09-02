from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GuiConfig:
    default_font: Path
    title_font: Path
    default_size_percent: int = 100
    title_size_percent: int = 100
    default_bold_style: float = 0.75
    title_bold_style: float = 0.75
    beleren_ascii: bool = False


def default_gui_config(base_dir: Path) -> GuiConfig:
    base_dir = Path(base_dir)
    return GuiConfig(
        default_font=base_dir / 'Pretendard-Medium.ttf',
        title_font=base_dir / 'Hahmlet-SemiBold.ttf',
    )
