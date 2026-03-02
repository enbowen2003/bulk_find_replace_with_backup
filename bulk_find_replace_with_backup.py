#!/usr/bin/env python3
"""
bulk_find_replace_with_backup.py

Purpose
- Find/replace text across one file or many files in a folder.
- For every file that changes:
  1) The ORIGINAL file is MOVED into a sibling "_backup" folder
  2) The original is renamed to the "new name" (optional filename replace) + a date/time stamp
  3) A NEW file is written in the original location (optionally with the renamed filename) containing the updated text

Examples
  python bulk_find_replace_with_backup.py "C:\\data\\in" --find OLD --replace NEW
  python bulk_find_replace_with_backup.py "C:\\data\\in" --find OLD --replace NEW --glob "*.sql" --recurse
  python bulk_find_replace_with_backup.py "C:\\data\\in\\onefile.txt" --find OLD --replace NEW --no-rename-files

Notes
- Best-effort encoding handling:
    * UTF-8 (with/without BOM), UTF-16 (LE/BE), else falls back to CP1252
- Only touches files where content actually changes.
- By default, also renames filenames that contain the find string (you can turn that off).

Version
- v1.0.1
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


__app_name__ = "bulk_find_replace_with_backup"
__version__ = "1.0.1"


@dataclass(frozen=True)
class FileResult:
    path: Path
    changed: bool
    backup_path: Optional[Path] = None
    new_path: Optional[Path] = None
    reason: Optional[str] = None


def _now_stamp() -> str:
    """Return a filesystem-safe timestamp like 20260302_134455."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _detect_and_read_text(path: Path) -> Tuple[str, str]:
    """
    Read file as text using a small set of common encodings.

    Returns: (text, encoding_used)
    Raises: UnicodeDecodeError if all attempts fail
    """
    data = path.read_bytes()

    # BOM-aware attempts
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue

    # Last-resort for "Windows text"
    return data.decode("cp1252"), "cp1252"


def _write_text(path: Path, text: str, encoding: str) -> None:
    """Write text using chosen encoding."""
    # Keep BOM behavior consistent if utf-8-sig was used
    path.write_text(text, encoding=encoding, newline="")


def _iter_files(target: Path, glob_pattern: str, recurse: bool) -> Iterable[Path]:
    """Yield files under target matching glob_pattern; supports single file input."""
    if target.is_file():
        yield target
        return

    if not target.exists():
        return

    if recurse:
        for p in target.rglob("*"):
            if p.is_file() and fnmatch.fnmatch(p.name, glob_pattern):
                yield p
    else:
        for p in target.iterdir():
            if p.is_file() and fnmatch.fnmatch(p.name, glob_pattern):
                yield p


def _safe_filename_replace(name: str, find: str, replace: str) -> str:
    """Replace find string in filename (not path) safely."""
    return name.replace(find, replace)


def _ensure_backup_dir(parent_dir: Path, backup_dir_name: str) -> Path:
    """Create/return backup directory under parent_dir."""
    bdir = parent_dir / backup_dir_name
    bdir.mkdir(parents=True, exist_ok=True)
    return bdir


def _build_backup_name(original_name: str, new_name: str, stamp: str) -> str:
    """
    Backup file name rule:
    - Uses the *new* filename (post rename) as the base
    - Appends __YYYYMMDD_HHMMSS before extension if possible
    """
    new_path = Path(new_name)
    stem = new_path.stem
    suffix = "".join(new_path.suffixes)  # handles .tar.gz etc
    if not suffix:
        return f"{new_name}__{stamp}"
    return f"{stem}__{stamp}{suffix}"


def process_one_file(
    file_path: Path,
    find: str,
    replace: str,
    backup_dir_name: str,
    rename_files: bool,
    dry_run: bool,
) -> FileResult:
    """Process a single file: replace content; move original into backup with stamped renamed name."""
    try:
        text, enc = _detect_and_read_text(file_path)
    except Exception as e:
        return FileResult(path=file_path, changed=False, reason=f"Read failed: {e}")

    if find not in text:
        return FileResult(path=file_path, changed=False, reason="Find string not found in content")

    updated = text.replace(find, replace)
    if updated == text:
        return FileResult(path=file_path, changed=False, reason="No effective change")

    parent = file_path.parent
    backup_dir = _ensure_backup_dir(parent, backup_dir_name)

    # Decide the new file name (optional filename rename)
    new_name = file_path.name
    if rename_files and find in file_path.name:
        new_name = _safe_filename_replace(file_path.name, find, replace)

    stamp = _now_stamp()
    backup_name = _build_backup_name(file_path.name, new_name, stamp)
    backup_path = backup_dir / backup_name

    # New output path (in original folder, potentially renamed)
    new_path = parent / new_name

    if dry_run:
        return FileResult(path=file_path, changed=True, backup_path=backup_path, new_path=new_path)

    try:
        # 1) Move original into backup folder with stamped name
        shutil.move(str(file_path), str(backup_path))

        # 2) Write updated content to new_path
        _write_text(new_path, updated, enc)

        return FileResult(path=file_path, changed=True, backup_path=backup_path, new_path=new_path)
    except Exception as e:
        # Attempt rollback if we already moved the file
        try:
            if backup_path.exists() and not file_path.exists():
                shutil.move(str(backup_path), str(file_path))
        except Exception:
            pass
        return FileResult(path=file_path, changed=False, reason=f"Write/move failed: {e}"


def _print_banner() -> None:
    print(f"{__app_name__} v{__version__}")
    print("-" * 72)


def _prompt_if_missing(args: argparse.Namespace) -> None:
    """Interactive prompts if caller omitted required arguments."""
    if not args.find:
        args.find = input("Find (text to find): ").strip()
    if not args.replace:
        args.replace = input("Replace (replacement text): ").strip()
    if not args.find or not args.replace:
        raise SystemExit("ERROR: --find and --replace are required.")


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk find/replace across files with stamped backups in _backup.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("target", help="File or folder path to process")
    p.add_argument("--find", help="Text to find")
    p.add_argument("--replace", help="Replacement text")
    p.add_argument("--glob", default="*.*", help="File glob when target is a folder (e.g. *.sql, *.txt)")
    p.add_argument("--recurse", action="store_true", help="Recurse subfolders when target is a folder")
    p.add_argument("--backup-dir", default="_backup", help="Backup folder name created under each processed folder")
    p.add_argument(
        "--no-rename-files",
        action="store_false",
        dest="rename_files",
        help="Do NOT rename filenames that contain the find string (content still replaced).",
    )
    p.set_defaults(rename_files=True)
    p.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    return p.parse_args(argv)


def main(argv: List[str]) -> int:
    _print_banner()
    args = parse_args(argv)
    _prompt_if_missing(args)

    target = Path(args.target).expanduser()
    if not target.exists():
        print(f"ERROR: target does not exist: {target}")
        return 2

    files = list(_iter_files(target, args.glob, args.recurse))
    if not files:
        print("No matching files found.")
        return 0

    print(f"Target:   {target}")
    print(f"Glob:     {args.glob}   Recurse: {args.recurse}")
    print(f"Backup:   {args.backup_dir}")
    print(f"Rename:   {args.rename_files}")
    print(f"Dry-run:  {args.dry_run}")
    print(f"Find:     {args.find!r}")
    print(f"Replace:  {args.replace!r}")
    print("-" * 72)

    changed = 0
    skipped = 0
    failed = 0

    for fp in files:
        r = process_one_file(
            file_path=fp,
            find=args.find,
            replace=args.replace,
            backup_dir_name=args.backup_dir,
            rename_files=args.rename_files,
            dry_run=args.dry_run,
        )

        if r.changed:
            changed += 1
            if args.dry_run:
                print(f"DRY  : {fp} -> {r.new_path}  (backup would be {r.backup_path})")
            else:
                print(f"OK   : {fp} -> {r.new_path}  (backup {r.backup_path})")
        else:
            # Distinguish "skipped" vs "failed"
            if r.reason and (r.reason.startswith("Read failed") or r.reason.startswith("Write/move failed")):
                failed += 1
                print(f"FAIL : {fp}  ({r.reason})")
            else:
                skipped += 1
                msg = r.reason or "No change"
                print(f"SKIP : {fp}  ({msg})")

    print("-" * 72)
    print(f"Done. Changed: {changed}  Skipped: {skipped}  Failed: {failed}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
