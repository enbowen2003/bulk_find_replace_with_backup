#!/usr/bin/env python3
"""
bulk_find_replace_with_backup_gui.py

A Tkinter “forms” app to bulk find/replace text across files with automatic backups.

Key behaviors
- Works on a single file OR a folder.
- For every file that changes:
    1) The ORIGINAL file is MOVED into a sibling backup folder (default "_backup")
    2) The backup filename is based on the (optional) renamed filename + a timestamp
    3) A NEW file is written at the original location (optionally with renamed filename) containing updated text
- Recurse depth control: you can recurse up to N levels deep (0 = only the chosen folder).
- Safety: never reads/updates files that live in any folder whose name contains "backup"
  (case-insensitive; e.g., "_backup", "BACKUP", "mybackup_old", etc.)

Version: v1.1.0
"""

from __future__ import annotations

import fnmatch
import shutil
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


__app_name__ = "Bulk Find/Replace + Backup (GUI)"
__version__ = "1.1.0"


# -----------------------------
# Core processing
# -----------------------------

@dataclass(frozen=True)
class FileResult:
    path: Path
    changed: bool
    backup_path: Optional[Path] = None
    new_path: Optional[Path] = None
    reason: Optional[str] = None


def _now_stamp() -> str:
    """Return filesystem-safe timestamp like 20260302_134455."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _path_has_backup_folder(p: Path) -> bool:
    """
    Safety rule: if ANY parent folder name contains 'backup' (case-insensitive),
    we will not touch the file.
    """
    for part in p.parts:
        # parts includes drive/root and filename too; only folders matter,
        # but treating all parts is safe: filename containing 'backup' is fine either way.
        if "backup" in str(part).lower():
            # If it's only in the filename, that's not a folder. We still prefer safety.
            # However, requirement is "never update files in *backup* folders".
            # We enforce a stricter rule: if any path segment contains backup, skip.
            return True
    return False


def _detect_and_read_text(path: Path) -> Tuple[str, str]:
    """
    Read file as text using common encodings.

    Returns: (text, encoding_used)
    """
    data = path.read_bytes()

    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue

    # Last-resort for "Windows text"
    return data.decode("cp1252"), "cp1252"


def _write_text(path: Path, text: str, encoding: str) -> None:
    """Write text preserving selected encoding style."""
    path.write_text(text, encoding=encoding, newline="")


def _safe_filename_replace(name: str, find: str, replace: str) -> str:
    """Replace find string in filename (not path)."""
    return name.replace(find, replace)


def _ensure_backup_dir(parent_dir: Path, backup_dir_name: str) -> Path:
    """Create/return backup directory under parent_dir."""
    bdir = parent_dir / backup_dir_name
    bdir.mkdir(parents=True, exist_ok=True)
    return bdir


def _build_backup_name(new_name: str, stamp: str) -> str:
    """
    Backup file name rule:
    - Uses the *new* filename (post-rename) as base
    - Appends __YYYYMMDD_HHMMSS before extension if possible
    """
    p = Path(new_name)
    suffix = "".join(p.suffixes)
    if not suffix:
        return f"{new_name}__{stamp}"
    return f"{p.stem}__{stamp}{suffix}"


def iter_files_with_depth(
    base: Path,
    glob_pattern: str,
    recurse: bool,
    max_depth: int,
) -> Iterable[Path]:
    """
    Yield files under base matching glob_pattern.
    - If base is a file, yield it.
    - If recurse=False, behaves like depth=0.
    - Depth definition: 0 means only files directly inside base (no subfolders).
      1 means base + 1 level of subfolders, etc.
    - Skips any directory whose name contains "backup" (case-insensitive).
    """
    if base.is_file():
        yield base
        return

    if not base.exists():
        return

    if not recurse:
        max_depth = 0

    base = base.resolve()

    # Manual walk with pruning
    # We'll use rglob-like logic via stack to control depth.
    stack: List[Tuple[Path, int]] = [(base, 0)]

    while stack:
        cur_dir, depth = stack.pop()

        # Skip backup folders entirely
        if "backup" in cur_dir.name.lower():
            continue

        try:
            for child in cur_dir.iterdir():
                if child.is_dir():
                    if depth < max_depth:
                        stack.append((child, depth + 1))
                elif child.is_file():
                    if fnmatch.fnmatch(child.name, glob_pattern):
                        yield child
        except PermissionError:
            # Skip unreadable dirs
            continue


def process_one_file(
    file_path: Path,
    find: str,
    replace: str,
    backup_dir_name: str,
    rename_files: bool,
    dry_run: bool,
) -> FileResult:
    """
    Replace content; move original into backup with stamped renamed name.

    Safety: refuse if file is inside any path segment containing 'backup' (case-insensitive).
    """
    if _path_has_backup_folder(file_path.resolve()):
        return FileResult(path=file_path, changed=False, reason="Skipped (path contains 'backup')")

    try:
        text, enc = _detect_and_read_text(file_path)
    except Exception as e:
        return FileResult(path=file_path, changed=False, reason=f"Read failed: {e}")

    if find not in text:
        return FileResult(path=file_path, changed=False, reason="Find not present in content")

    updated = text.replace(find, replace)
    if updated == text:
        return FileResult(path=file_path, changed=False, reason="No effective change")

    parent = file_path.parent
    # Creating backup folder is fine; we never *process* files under it.
    backup_dir = _ensure_backup_dir(parent, backup_dir_name)

    new_name = file_path.name
    if rename_files and find and (find in file_path.name):
        new_name = _safe_filename_replace(file_path.name, find, replace)

    stamp = _now_stamp()
    backup_name = _build_backup_name(new_name, stamp)
    backup_path = backup_dir / backup_name
    new_path = parent / new_name

    if dry_run:
        return FileResult(path=file_path, changed=True, backup_path=backup_path, new_path=new_path)

    try:
        # Move original to backup, then write new file
        shutil.move(str(file_path), str(backup_path))
        _write_text(new_path, updated, enc)
        return FileResult(path=file_path, changed=True, backup_path=backup_path, new_path=new_path)
    except Exception as e:
        # Best-effort rollback if we moved original but failed to write
        try:
            if backup_path.exists() and not file_path.exists():
                shutil.move(str(backup_path), str(file_path))
        except Exception:
            pass
        return FileResult(path=file_path, changed=False, reason=f"Write/move failed: {e}"


def run_batch(
    target: Path,
    find: str,
    replace: str,
    glob_pattern: str,
    recurse: bool,
    max_depth: int,
    backup_dir_name: str,
    rename_files: bool,
    dry_run: bool,
) -> List[FileResult]:
    files = list(iter_files_with_depth(target, glob_pattern, recurse, max_depth))

    results: List[FileResult] = []
    for fp in files:
        r = process_one_file(
            file_path=fp,
            find=find,
            replace=replace,
            backup_dir_name=backup_dir_name,
            rename_files=rename_files,
            dry_run=dry_run,
        )
        results.append(r)

    return results


# -----------------------------
# Tkinter UI
# -----------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{__app_name__} v{__version__}")
        self.geometry("980x620")
        self.minsize(900, 560)

        self._build_ui()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # Top form
        form = ttk.Frame(outer)
        form.pack(fill="x")

        # Target row
        ttk.Label(form, text="Target (file or folder):").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.var_target = tk.StringVar()
        ent_target = ttk.Entry(form, textvariable=self.var_target)
        ent_target.grid(row=0, column=1, sticky="we", padx=(8, 8), pady=(0, 6))
        ttk.Button(form, text="Browse File...", command=self._browse_file).grid(row=0, column=2, pady=(0, 6))
        ttk.Button(form, text="Browse Folder...", command=self._browse_folder).grid(row=0, column=3, padx=(8, 0), pady=(0, 6))

        # Find/Replace row
        ttk.Label(form, text="Find:").grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.var_find = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_find).grid(row=1, column=1, sticky="we", padx=(8, 8), pady=(0, 6), columnspan=3)

        ttk.Label(form, text="Replace:").grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.var_replace = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_replace).grid(row=2, column=1, sticky="we", padx=(8, 8), pady=(0, 6), columnspan=3)

        # Options row 1
        ttk.Label(form, text="File glob:").grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.var_glob = tk.StringVar(value="*.*")
        ttk.Entry(form, textvariable=self.var_glob, width=16).grid(row=3, column=1, sticky="w", padx=(8, 8), pady=(0, 6))

        self.var_backup_dir = tk.StringVar(value="_backup")
        ttk.Label(form, text="Backup folder name:").grid(row=3, column=2, sticky="e", pady=(0, 6))
        ttk.Entry(form, textvariable=self.var_backup_dir, width=18).grid(row=3, column=3, sticky="w", padx=(8, 0), pady=(0, 6))

        # Options row 2
        self.var_recurse = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Recurse", variable=self.var_recurse, command=self._toggle_depth).grid(
            row=4, column=0, sticky="w", pady=(0, 6)
        )

        ttk.Label(form, text="Max depth (0 = only this folder):").grid(row=4, column=1, sticky="w", padx=(8, 8), pady=(0, 6))
        self.var_depth = tk.IntVar(value=2)
        self.spin_depth = ttk.Spinbox(form, from_=0, to=50, textvariable=self.var_depth, width=6)
        self.spin_depth.grid(row=4, column=2, sticky="w", pady=(0, 6))
        ttk.Label(form, text="(ignored unless Recurse is checked)").grid(row=4, column=3, sticky="w", padx=(8, 0), pady=(0, 6))

        # Options row 3
        self.var_rename_files = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Rename filenames that contain Find", variable=self.var_rename_files).grid(
            row=5, column=0, sticky="w", pady=(0, 6)
        )

        self.var_dry_run = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Dry run (no changes)", variable=self.var_dry_run).grid(
            row=5, column=1, sticky="w", padx=(8, 8), pady=(0, 6)
        )

        self.var_strict_backup_safety = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            form,
            text="Safety: skip any path segment containing 'backup' (recommended)",
            variable=self.var_strict_backup_safety,
        ).grid(row=5, column=2, sticky="w", columnspan=2, pady=(0, 6))

        # Buttons row
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(10, 10))

        ttk.Button(btns, text="Preview (Dry Run)", command=self._preview).pack(side="left")
        ttk.Button(btns, text="Run (Apply Changes)", command=self._run_apply).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Clear Log", command=self._clear_log).pack(side="left", padx=(8, 0))

        # Log area
        log_frame = ttk.LabelFrame(outer, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_frame, wrap="none", height=18)
        self.txt_log.pack(fill="both", expand=True, side="left")

        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        yscroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=yscroll.set)

        form.columnconfigure(1, weight=1)

        self._toggle_depth()
        self._log(f"{__app_name__} v{__version__}")
        self._log("Safety: files under any folder with 'backup' in the path are skipped.")
        self._log("Tip: Start with Preview (Dry Run).")

    def _toggle_depth(self) -> None:
        state = "normal" if self.var_recurse.get() else "disabled"
        self.spin_depth.configure(state=state)

    def _browse_file(self) -> None:
        p = filedialog.askopenfilename(title="Select a file")
        if p:
            self.var_target.set(p)

    def _browse_folder(self) -> None:
        p = filedialog.askdirectory(title="Select a folder")
        if p:
            self.var_target.set(p)

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", "end")

    def _log(self, msg: str) -> None:
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")

    def _validate_inputs(self) -> Optional[Tuple[Path, str, str, str, bool, int, str, bool, bool]]:
        target_raw = self.var_target.get().strip()
        find = self.var_find.get()
        replace = self.var_replace.get()
        glob_pattern = self.var_glob.get().strip() or "*.*"
        recurse = bool(self.var_recurse.get())
        max_depth = int(self.var_depth.get() or 0)
        backup_dir_name = (self.var_backup_dir.get().strip() or "_backup")
        rename_files = bool(self.var_rename_files.get())
        strict_backup_safety = bool(self.var_strict_backup_safety.get())

        if not target_raw:
            messagebox.showerror("Missing target", "Please select a target file or folder.")
            return None

        target = Path(target_raw).expanduser()
        if not target.exists():
            messagebox.showerror("Invalid target", f"Target does not exist:\n{target}")
            return None

        if find == "":
            messagebox.showerror("Missing Find", "Find cannot be empty.")
            return None

        if max_depth < 0:
            messagebox.showerror("Invalid depth", "Max depth must be >= 0.")
            return None

        # Toggle safety function behavior (strict vs folder-only)
        # Requirement is folder-only, but strict is safer; user can uncheck if desired.
        global _path_has_backup_folder
        if strict_backup_safety:
            # keep strict behavior (already strict)
            pass
        else:
            # redefine to folder-only check (less strict)
            def _folder_only_check(p: Path) -> bool:
                for parent in p.resolve().parents:
                    if "backup" in parent.name.lower():
                        return True
                return False
            _path_has_backup_folder = _folder_only_check  # type: ignore

        return (target, find, replace, glob_pattern, recurse, max_depth, backup_dir_name, rename_files, strict_backup_safety)

    def _preview(self) -> None:
        vals = self._validate_inputs()
        if not vals:
            return
        target, find, replace, glob_pattern, recurse, max_depth, backup_dir_name, rename_files, _ = vals

        self._log("-" * 80)
        self._log("PREVIEW (DRY RUN)")
        self._log(f"Target     : {target}")
        self._log(f"Glob       : {glob_pattern}")
        self._log(f"Recurse    : {recurse}   Max depth: {max_depth if recurse else 0}")
        self._log(f"Backup dir : {backup_dir_name}")
        self._log(f"Rename file: {rename_files}")
        self._log(f"Find       : {find!r}")
        self._log(f"Replace    : {replace!r}")
        self._log("-" * 80)

        try:
            results = run_batch(
                target=target,
                find=find,
                replace=replace,
                glob_pattern=glob_pattern,
                recurse=recurse,
                max_depth=max_depth,
                backup_dir_name=backup_dir_name,
                rename_files=rename_files,
                dry_run=True,
            )
        except Exception:
            self._log("ERROR: Exception while previewing:")
            self._log(traceback.format_exc())
            return

        self._render_results(results, dry_run=True)

    def _run_apply(self) -> None:
        vals = self._validate_inputs()
        if not vals:
            return
        target, find, replace, glob_pattern, recurse, max_depth, backup_dir_name, rename_files, _ = vals

        # Always compute preview first so user sees counts
        try:
            preview = run_batch(
                target=target,
                find=find,
                replace=replace,
                glob_pattern=glob_pattern,
                recurse=recurse,
                max_depth=max_depth,
                backup_dir_name=backup_dir_name,
                rename_files=rename_files,
                dry_run=True,
            )
        except Exception:
            messagebox.showerror("Error", "Exception while building preview.\nSee log for details.")
            self._log("ERROR: Exception while building preview:")
            self._log(traceback.format_exc())
            return

        would_change = sum(1 for r in preview if r.changed)
        if would_change == 0:
            messagebox.showinfo("Nothing to do", "No files would be changed (based on preview).")
            self._log("No changes detected in preview.")
            return

        ok = messagebox.askyesno(
            "Confirm apply",
            f"This will MODIFY files.\n\n"
            f"Files that would change: {would_change}\n\n"
            f"Proceed?",
        )
        if not ok:
            self._log("Apply canceled by user.")
            return

        self._log("-" * 80)
        self._log("APPLY CHANGES")
        self._log(f"Target     : {target}")
        self._log(f"Glob       : {glob_pattern}")
        self._log(f"Recurse    : {recurse}   Max depth: {max_depth if recurse else 0}")
        self._log(f"Backup dir : {backup_dir_name}")
        self._log(f"Rename file: {rename_files}")
        self._log(f"Find       : {find!r}")
        self._log(f"Replace    : {replace!r}")
        self._log("-" * 80)

        try:
            results = run_batch(
                target=target,
                find=find,
                replace=replace,
                glob_pattern=glob_pattern,
                recurse=recurse,
                max_depth=max_depth,
                backup_dir_name=backup_dir_name,
                rename_files=rename_files,
                dry_run=False,
            )
        except Exception:
            messagebox.showerror("Error", "Exception while applying changes.\nSee log for details.")
            self._log("ERROR: Exception while applying changes:")
            self._log(traceback.format_exc())
            return

        self._render_results(results, dry_run=False)

    def _render_results(self, results: List[FileResult], dry_run: bool) -> None:
        changed = 0
        skipped = 0
        failed = 0

        for r in results:
            if r.changed:
                changed += 1
                if dry_run:
                    self._log(f"DRY  : {r.path} -> {r.new_path}  (backup would be {r.backup_path})")
                else:
                    self._log(f"OK   : {r.path} -> {r.new_path}  (backup {r.backup_path})")
            else:
                # classify failure vs skip
                if r.reason and (r.reason.startswith("Read failed") or r.reason.startswith("Write/move failed")):
                    failed += 1
                    self._log(f"FAIL : {r.path}  ({r.reason})")
                else:
                    skipped += 1
                    msg = r.reason or "No change"
                    self._log(f"SKIP : {r.path}  ({msg})")

        self._log("-" * 80)
        self._log(f"Done. Changed: {changed}  Skipped: {skipped}  Failed: {failed}")

        if failed == 0:
            if dry_run:
                messagebox.showinfo("Preview complete", f"Would change: {changed}\nSkipped: {skipped}\nFailed: {failed}")
            else:
                messagebox.showinfo("Apply complete", f"Changed: {changed}\nSkipped: {skipped}\nFailed: {failed}")
        else:
            messagebox.showwarning("Completed with errors", f"Changed: {changed}\nSkipped: {skipped}\nFailed: {failed}\n\nSee log.")


def main() -> int:
    try:
        app = App()
        app.mainloop()
        return 0
    except Exception:
        # If Tk cannot initialize, emit something useful.
        print(f"{__app_name__} v{__version__}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
