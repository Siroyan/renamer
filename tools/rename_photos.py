#!/usr/bin/env python3
"""Rename photos in-place on a filesystem based on EXIF capture date.

Naming format:
    yyyy-mm-dd-00001

Ordering for numbering is stable:
    capture datetime ASC, original filename ASC
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
except ImportError:  # pragma: no cover - environment dependent
    Image = None
    TAGS = {}

DEFAULT_EXTENSIONS = ".jpg,.jpeg,.heic,.png"
EXIF_FALLBACK_KEYS = ("DateTimeOriginal", "DateTimeDigitized", "DateTime")


@dataclass(frozen=True)
class PhotoFile:
    path: Path
    capture_dt: dt.datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename photo files on an SD card in place using EXIF capture date."
    )
    parser.add_argument("--root", required=True, help="Target root directory to process")
    parser.add_argument(
        "--ext",
        default=DEFAULT_EXTENSIONS,
        help=(
            "Comma-separated extensions to process (default: "
            f"{DEFAULT_EXTENSIONS})"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without renaming files",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recurse into subdirectories",
    )
    return parser.parse_args()


def normalize_extensions(raw: str) -> set[str]:
    values = set()
    for part in raw.split(","):
        ext = part.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        values.add(ext)
    return values


def iter_candidate_files(root: Path, recursive: bool, extensions: set[str]) -> Iterable[Path]:
    walker = root.rglob("*") if recursive else root.glob("*")
    for path in walker:
        if path.is_file() and path.suffix.lower() in extensions:
            yield path


def read_capture_datetime(path: Path) -> dt.datetime | None:
    if Image is None:
        return None

    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except Exception:
        return None

    if not exif:
        return None

    decoded = {}
    for key, value in exif.items():
        tag_name = TAGS.get(key, key)
        decoded[tag_name] = value

    for key in EXIF_FALLBACK_KEYS:
        raw_value = decoded.get(key)
        if not raw_value:
            continue
        if isinstance(raw_value, bytes):
            raw_value = raw_value.decode(errors="ignore")
        parsed = parse_exif_datetime(str(raw_value))
        if parsed:
            return parsed
    return None


def parse_exif_datetime(raw: str) -> dt.datetime | None:
    raw = raw.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def collect_photos(root: Path, recursive: bool, extensions: set[str]) -> tuple[list[PhotoFile], list[Path]]:
    photos: list[PhotoFile] = []
    skipped: list[Path] = []
    for path in iter_candidate_files(root, recursive, extensions):
        capture = read_capture_datetime(path)
        if capture is None:
            skipped.append(path)
            continue
        photos.append(PhotoFile(path=path, capture_dt=capture))
    return photos, skipped


def build_rename_plan(photos: list[PhotoFile]) -> list[tuple[Path, Path]]:
    ordered = sorted(photos, key=lambda p: (p.capture_dt, p.path.name))

    serial_by_day: dict[str, int] = {}
    plan: list[tuple[Path, Path]] = []
    for photo in ordered:
        day = photo.capture_dt.strftime("%Y-%m-%d")
        serial_by_day[day] = serial_by_day.get(day, 0) + 1
        new_stem = f"{day}-{serial_by_day[day]:05d}"
        target = photo.path.with_name(f"{new_stem}{photo.path.suffix.lower()}")
        plan.append((photo.path, target))

    return plan


def validate_plan(plan: list[tuple[Path, Path]]) -> list[str]:
    errors: list[str] = []
    target_set = [dst for _, dst in plan]
    seen: set[Path] = set()
    for dst in target_set:
        if dst in seen:
            errors.append(f"Duplicate target generated: {dst}")
        seen.add(dst)

    sources = {src for src, _ in plan}
    for src, dst in plan:
        if src == dst:
            continue
        if dst.exists() and dst not in sources:
            errors.append(f"Target already exists and is not part of rename set: {dst}")
    return errors


def execute_plan(plan: list[tuple[Path, Path]], dry_run: bool) -> None:
    actionable = [(src, dst) for src, dst in plan if src != dst]
    if not actionable:
        print("No rename needed.")
        return

    for src, dst in actionable:
        action = "DRY-RUN" if dry_run else "RENAME"
        print(f"{action}: {src} -> {dst}")

    if dry_run:
        return

    tmp_moves: list[tuple[Path, Path]] = []
    for src, _ in actionable:
        tmp = src.with_name(f".{src.name}.renaming-{uuid.uuid4().hex}")
        src.rename(tmp)
        tmp_moves.append((tmp, src))

    original_to_target = {src: dst for src, dst in actionable}
    for tmp, original_src in tmp_moves:
        tmp.rename(original_to_target[original_src])


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()

    if not root.exists() or not root.is_dir():
        print(f"ERROR: --root is not a directory: {root}", file=sys.stderr)
        return 2

    if Image is None:
        print(
            "ERROR: Pillow is required to read EXIF metadata. Install with `pip install Pillow`.",
            file=sys.stderr,
        )
        return 2

    extensions = normalize_extensions(args.ext)
    if not extensions:
        print("ERROR: --ext produced no valid extension", file=sys.stderr)
        return 2

    photos, skipped = collect_photos(root, args.recursive, extensions)
    print(f"Scanned files with matching extensions: {len(photos) + len(skipped)}")
    print(f"Files with usable EXIF datetime: {len(photos)}")
    if skipped:
        print("Skipped files without usable EXIF datetime:")
        for path in skipped:
            print(f"  - {path}")

    plan = build_rename_plan(photos)
    errors = validate_plan(plan)
    if errors:
        print("ERROR: rename plan validation failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    execute_plan(plan, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
