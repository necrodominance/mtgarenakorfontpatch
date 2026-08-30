from __future__ import annotations

from pathlib import Path
import json

from .serialized import SerializedFile, ObjectInfo
from .typetree import TypeNode, parse_top_fields, root_for_object
from .unityfs import UnityFSBundle, BundleMember


def cab_member(bundle: UnityFSBundle) -> BundleMember:
    candidates = [m for m in bundle.members if not m.name.endswith('.resS')]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected exactly one SerializedFile member, found {len(candidates)}")
    return candidates[0]


def ress_member(bundle: UnityFSBundle) -> BundleMember | None:
    candidates = [m for m in bundle.members if m.name.endswith('.resS')]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise RuntimeError(f"Expected at most one .resS member, found {len(candidates)}")
    return candidates[0]


def _candidate_objects_for_occurrence(serialized: SerializedFile, pos: int):
    for obj in serialized.objects:
        if obj.start <= pos < obj.start + obj.size:
            yield obj


def find_named_object(
    serialized: SerializedFile,
    schema_root: TypeNode | None,
    name: str,
    *,
    require_type_tree: bool = False,
) -> ObjectInfo:
    needle = name.encode('utf-8')
    positions = []
    start = 0
    while True:
        pos = serialized.data.find(needle, start)
        if pos < 0:
            break
        positions.append(pos)
        start = pos + 1

    seen = set()
    for pos in positions:
        for obj in _candidate_objects_for_occurrence(serialized, pos):
            if obj.path_id in seen:
                continue
            seen.add(obj.path_id)
            root = schema_root
            if root is None:
                if not serialized.enable_type_tree:
                    continue
                try:
                    root = root_for_object(serialized, obj)
                except Exception:
                    continue
            try:
                fields, _, _ = parse_top_fields(serialized.data, obj.start, obj.size, root, store=True)
            except Exception:
                continue
            if fields.get('m_Name') == name:
                return obj

    if require_type_tree and not serialized.enable_type_tree:
        raise RuntimeError(f"Cannot discover {name}: target file has no type tree")
    raise RuntimeError(f"Could not locate Unity object named {name!r}")



def active_bundle_names_from_manifest(downloads_dir: Path) -> tuple[str | None, str | None, Path | None]:
    manifests = sorted(downloads_dir.glob('Manifest_*.mtga'), key=lambda p: p.stat().st_mtime, reverse=True)
    for manifest in manifests:
        try:
            data = json.loads(manifest.read_text(encoding='utf-8'))
            assets = data.get('Assets', []) if isinstance(data, dict) else []
            fonts = None
            card = None
            for entry in assets:
                if not isinstance(entry, dict):
                    continue
                name = entry.get('Name')
                if not isinstance(name, str):
                    continue
                if name.startswith('Fonts_') and name.endswith('.mtga'):
                    fonts = name
                elif name.startswith('Bucket_Card.FieldFont_0_') and name.endswith('.mtga'):
                    card = name
                if fonts and card:
                    return fonts, card, manifest
        except Exception:
            continue
    return None, None, None

def find_bundle_by_prefix(assetbundle_dir: Path, prefix: str) -> Path:
    candidates = sorted(assetbundle_dir.glob(prefix + '*.mtga'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No bundle matching {prefix}*.mtga in {assetbundle_dir}")
    return candidates[0]


def locate_mtga_root(explicit: Path | None = None) -> Path:
    if explicit is not None:
        root = explicit.resolve()
        if (root / 'MTGA_Data').is_dir():
            return root
        if root.name == 'MTGA_Data' and root.is_dir():
            return root.parent
        raise FileNotFoundError(f"MTGA_Data not found under {root}")

    candidates = [
        Path(r'C:\Program Files (x86)\Steam\steamapps\common\MTGA'),
        Path(r'C:\Program Files\Steam\steamapps\common\MTGA'),
        Path(r'C:\Games\MTGA'),
    ]
    for root in candidates:
        if (root / 'MTGA_Data').is_dir():
            return root
    raise FileNotFoundError('Could not auto-detect MTGA installation. Use --root.')
