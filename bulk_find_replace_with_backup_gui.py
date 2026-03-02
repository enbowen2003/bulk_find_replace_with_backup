#!/usr/bin/env python3
"""
bulk_find_replace_with_backup_gui.py

Tkinter forms app to bulk find/replace text across files with automatic backups + CSV report.

What it does
- Target can be a single file OR a folder.
- For every file that changes:
    1) The ORIGINAL file is MOVED into a sibling backup folder (default "_backup")
    2) The backup filename is based on the (optional) renamed filename + a timestamp
    3) A NEW file is written at the original location (optionally renamed) containing updated text
- Recurse depth control: recurse up to N levels deep (0 = only chosen folder).
- Safety: never scans or updates files inside folders whose name contains "backup" (case-insensitive).
- Generates a CSV report listing changed files and the line numbers impacted (lines containing the Find text).

Version: v1.2.0
"""

from __future__ import annotations

import csv
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
__version__ = "1.2.0"


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
    line_numbers: Optional[List[int]] = None
    occurrences: int = 0


def _now_stamp() -> str:
    """Filesystem-safe timestamp like 20260302_134455."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _is_backup_dirname(name: str) -> bool:
    """Folder-name test for backup safety."""
    return "backup" in name.lower()


def _in_backup_folder(file_path: Path) -> bool:
    """
    Safety rule requested: never update files in *backup* folders.
    We treat any parent directory whose name contains 'backup' (case-insensitive) as a backup folder.
    """
    p = file_path.resolve()
    for parent in p.parents:
        if _is_backup_dirname(parent.name):
            return True
    return False


def _detect_and_read_text(path: Path) -> Tuple[str, str]:
    """
    Read file as text using common encodings.
    Returns (text, encoding_used).
    """
    data = path.read_bytes()

    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue

    # Last-resort for common Windows text
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
    Backup file name:
    - Uses the *new* filename (post-rename) as base
    - Appends __YYYYMMDD_HHMMSS before extension if possible
    """
    p = Path(new_name)
    suffix = "".join(p.suffixes)
    if not suffix:
        return f"{new_name}__{stamp}"
    return f"{p.stem}__{stamp}{suffix}"


def _changed_line_numbers_and_occurrences(text: str, find: str) -> Tuple[List[int], int]:
    """
    For a simple global replace, impacted lines are those containing 'find'.
    Returns (line_numbers_1_based, total_occurrences_in_file).
    """
    lines = text.splitlines()
    line_nums: List[int] = []
    occ = 0
    for idx, line in enumerate(lines, start=1):
        if find in line:
            line_nums.append(idx)
            occ += line.count(find)
    return line_nums, occ


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
    - Depth: 0 = only files directly inside base (no subfolders).
      1 = base + 1 level of subfolders, etc.
    - Prunes any directory whose name contains "backup" (case-insensitive).
    """
    if base.is_file():
        yield base
        return

    if not base.exists():
        return

    if not recurse:
        max_depth = 0

    base = base.resolve()
    stack: List[Tuple[Path, int]] = [(base, 0)]

    while stack:
        cur_dir, depth = stack.pop()

        # Do not even traverse into backup-ish folders
        if _is_backup_dirname(cur_dir.name):
            continue

        try:
            for child in cur_dir.iterdir():
                if child.is_dir():
                    if depth < max_depth:
                        stack.append((child, depth + 1))
                elif child.is_file():
                    if fnmatch.fnmatch(child.name, glob_pattern):
                        # Safety: if the file lives under a backup folder, skip (belt + suspenders)
                        if not _in_backup_folder(child):
                            yield child
        except PermissionError:
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
    Safety: refuse if file is inside a folder containing 'backup'.
    """
    if _in_backup_folder(file_path):
        return FileResult(path=file_path, changed=False, reason="Skipped (in backup folder)")

    try:
        text, enc = _detect_and_read_text(file_path)
    except Exception as e:
        return FileResult(path=file_path, changed=False, reason=f"Read failed: {e}")

    if find not in text:
        return FileResult(path=file_path, changed=False, reason="Find not present in content")

    line_nums, occ = _changed_line_numbers_and_occurrences(text, find)
    updated = text.replace(find, replace)
    if updated == text:
        return FileResult(path=file_path, changed=False, reason="No effective change")

    parent = file_path.parent
    backup_dir = _ensure_backup_dir(parent, backup_dir_name)

    new_name = file_path.name
    if rename_files and (find in file_path.name):
        new_name = _safe_filename_replace(file_path.name, find, replace)

    stamp = _now_stamp()
    backup_name = _build_backup_name(new_name, stamp)
    backup_path = backup_dir / backup_name
    new_path = parent / new_name

    if dry_run:
        return FileResult(
            path=file_path,
            changed=True,
            backup_path=backup_path,
            new_path=new_path,
            line_numbers=line_nums,
            occurrences=occ,
        )

    try:
        # Move original to backup, then write new file
        shutil.move(str(file_path), str(backup_path))
        _write_text(new_path, updated, enc)
        return FileResult(
            path=file_path,
            changed=True,
            backup_path=backup_path,
            new_path=new_path,
            line_numbers=line_nums,
            occurrences=occ,
        )
    except Exception as e:
        # Best-effort rollback if we moved original but failed to write
        try:
            if backup_path.exists() and not file_path.exists():
                shutil.move(str(backup_path), str(file_path))
        except Exception:
            pass
        return FileResult(
            path=file_path,
            changed=False,
            reason=f"Write/move failed: {e}",
            line_numbers=line_nums,
            occurrences=occ,
        )


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
        results.append(
            process_one_file(
                file_path=fp,
                find=find,
                replace=replace,
                backup_dir_name=backup_dir_name,
                rename_files=rename_files,
                dry_run=dry_run,
            )
        )
    return results


def _default_report_path(target: Path) -> Path:
    """Default report location: beside the target (folder) or beside the target file."""
    base_dir = target.parent if target.is_file() else target
    return base_dir / f"find_replace_report__{_now_stamp()}.csv"


def write_report_csv(report_path: Path, results: List[FileResult], mode: str) -> None:
    """
    Write a CSV report.
    mode: "PREVIEW" or "APPLY"
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with report_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mode", mode])
        w.writerow(["generated_at", datetime.now().isoformat(timespec="seconds")])
        w.writerow([])
        w.writerow(
            [
                "status",
                "original_path",
                "new_path",
                "backup_path",
                "occurrences",
                "line_numbers",
                "reason",
            ]
        )
        for r in results:
            status = "CHANGED" if r.changed else "SKIPPED"
            if r.reason and (r.reason.startswith("Read failed") or r.reason.startswith("Write/move failed")):
                status = "FAILED"
            w.writerow(
                [
                    status,
                    str(r.path),
                    str(r.new_path) if r.new_path else "",
                    str(r.backup_path) if r.backup_path else "",
                    r.occurrences,
                    ";".join(str(n) for n in (r.line_numbers or [])),
                    r.reason or "",
                ]
            )


# -----------------------------
# Tkinter UI
# -----------------------------

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{__app_name__} v{__version__}")
        self.geometry("1020x680")
        self.minsize(940, 600)

        self._last_results: List[FileResult] = []
        self._last_mode: str = ""
        self._build_ui()

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        form = ttk.Frame(outer)
        form.pack(fill="x")

        # Target row
        ttk.Label(form, text="Target (file or folder):").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.var_target = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_target).grid(row=0, column=1, sticky="we", padx=(8, 8), pady=(0, 6))
        ttk.Button(form, text="Browse File...", command=self._browse_file).grid(row=0, column=2, pady=(0, 6))
        ttk.Button(form, text="Browse Folder...", command=self._browse_folder).grid(row=0, column=3, padx=(8, 0), pady=(0, 6))

        # Find/Replace
        ttk.Label(form, text="Find:").grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.var_find = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_find).grid(row=1, column=1, sticky="we", padx=(8, 8), pady=(0, 6), columnspan=3)

        ttk.Label(form, text="Replace:").grid(row=2, column=0, sticky="w", pady=(0, 6))
        self.var_replace = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_replace).grid(row=2, column=1, sticky="we", padx=(8, 8), pady=(0, 6), columnspan=3)

        # Options row
        ttk.Label(form, text="File glob:").grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.var_glob = tk.StringVar(value="*.*")
        ttk.Entry(form, textvariable=self.var_glob, width=18).grid(row=3, column=1, sticky="w", padx=(8, 8), pady=(0, 6))

        ttk.Label(form, text="Backup folder name:").grid(row=3, column=2, sticky="e", pady=(0, 6))
        self.var_backup_dir = tk.StringVar(value="_backup")
        ttk.Entry(form, textvariable=self.var_backup_dir, width=18).grid(row=3, column=3, sticky="w", padx=(8, 0), pady=(0, 6))

        # Recurse + depth
        self.var_recurse = tk.BooleanVar(value=False)
        ttk.Checkbutton(form, text="Recurse", variable=self.var_recurse, command=self._toggle_depth).grid(
            row=4, column=0, sticky="w", pady=(0, 6)
        )

        ttk.Label(form, text="Max depth (0 = only this folder):").grid(row=4, column=1, sticky="w", padx=(8, 8), pady=(0, 6))
        self.var_depth = tk.IntVar(value=2)
        self.spin_depth = ttk.Spinbox(form, from_=0, to=50, textvariable=self.var_depth, width=6)
        self.spin_depth.grid(row=4, column=2, sticky="w", pady=(0, 6))
        ttk.Label(form, text="(ignored unless Recurse is checked)").grid(row=4, column=3, sticky="w", padx=(8, 0), pady=(0, 6))

        # More options
        self.var_rename_files = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="Rename filenames that contain Find", variable=self.var_rename_files).grid(
            row=5, column=0, sticky="w", pady=(0, 6)
        )

        ttk.Label(form, text="Safety: skip folders containing 'backup' (always on)").grid(
            row=5, column=1, sticky="w", padx=(8, 8), pady=(0, 6), columnspan=3
        )

        # Buttons row
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(10, 10))

        ttk.Button(btns, text="Preview (Dry Run)", command=self._preview).pack(side="left")
        ttk.Button(btns, text="Run (Apply Changes)", command=self._run_apply).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Save Report CSV...", command=self._save_report_as).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Clear Log", command=self._clear_log).pack(side="left", padx=(8, 0))

        # Log
        log_frame = ttk.LabelFrame(outer, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.txt_log = tk.Text(log_frame, wrap="none", height=20)
        self.txt_log.pack(fill="both", expand=True, side="left")

        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.txt_log.yview)
        yscroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=yscroll.set)

        form.columnconfigure(1, weight=1)

        self._toggle_depth()
        self._log(f"{__app_name__} v{__version__}")
        self._log("Safety: files inside folders containing 'backup' are skipped.")
        self._log("Tip: Start with Preview (Dry Run).")

    def _toggle_depth(self) -> None:
        self.spin_depth.configure(state="normal" if self.var_recurse.get() else "disabled")

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

    def _validate_inputs(self) -> Optional[Tuple[Path, str, str, str, bool, int, str, bool]]:
        target_raw = self.var_target.get().strip()
        find = self.var_find.get()
        replace = self.var_replace.get()
        glob_pattern = self.var_glob.get().strip() or "*.*"
        recurse = bool(self.var_recurse.get())
        max_depth = int(self.var_depth.get() or 0)
        backup_dir_name = (self.var_backup_dir.get().strip() or "_backup")
        rename_files = bool(self.var_rename_files.get())

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

        return (target, find, replace, glob_pattern, recurse, max_depth, backup_dir_name, rename_files)

    def _render_results(self, results: List[FileResult], dry_run: bool) -> Tuple[int, int, int]:
        changed = 0
        skipped = 0
        failed = 0

        for r in results:
            if r.changed:
                changed += 1
                ln = ";".join(str(n) for n in (r.line_numbers or []))
                if dry_run:
                    self._log(f"DRY  : {r.path} -> {r.new_path}  (backup would be {r.backup_path})  lines=[{ln}] occ={r.occurrences}")
                else:
                    self._log(f"OK   : {r.path} -> {r.new_path}  (backup {r.backup_path})  lines=[{ln}] occ={r.occurrences}")
            else:
                if r.reason and (r.reason.startswith("Read failed") or r.reason.startswith("Write/move failed")):
                    failed += 1
                    self._log(f"FAIL : {r.path}  ({r.reason})")
                else:
                    skipped += 1
                    self._log(f"SKIP : {r.path}  ({r.reason or 'No change'})")

        self._log("-" * 90)
        self._log(f"Done. Changed: {changed}  Skipped: {skipped}  Failed: {failed}")
        return changed, skipped, failed

    def _auto_write_report(self, target: Path, results: List[FileResult], mode: str) -> Optional[Path]:
        try:
            report_path = _default_report_path(target)
            write_report_csv(report_path, results, mode=mode)
            return report_path
        except Exception:
            self._log("ERROR: Could not write report CSV:")
            self._log(traceback.format_exc())
            return None

    def _preview(self) -> None:
        vals = self._validate_inputs()
        if not vals:
            return
        target, find, replace, glob_pattern, recurse, max_depth, backup_dir_name, rename_files = vals

        self._log("-" * 90)
        self._log("PREVIEW (DRY RUN)")
        self._log(f"Target     : {target}")
        self._log(f"Glob       : {glob_pattern}")
        self._log(f"Recurse    : {recurse}   Max depth: {max_depth if recurse else 0}")
        self._log(f"Backup dir : {backup_dir_name}")
        self._log(f"Rename file: {rename_files}")
        self._log(f"Find       : {find!r}")
        self._log(f"Replace    : {replace!r}")
        self._log("-" * 90)

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
            messagebox.showerror("Error", "Exception while previewing. See log for details.")
            return

        self._last_results = results
        self._last_mode = "PREVIEW"

        changed, skipped, failed = self._render_results(results, dry_run=True)
        report_path = self._auto_write_report(target, results, mode="PREVIEW")
        if report_path:
            self._log(f"Report CSV: {report_path}")

        if failed == 0:
            messagebox.showinfo("Preview complete", f"Would change: {changed}\nSkipped: {skipped}\nFailed: {failed}")
        else:
            messagebox.showwarning("Preview complete (with errors)", f"Would change: {changed}\nSkipped: {skipped}\nFailed: {failed}\n\nSee log.")

    def _run_apply(self) -> None:
        vals = self._validate_inputs()
        if not vals:
            return
        target, find, replace, glob_pattern, recurse, max_depth, backup_dir_name, rename_files = vals

        # Build preview counts first
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
            self._log("ERROR: Exception while building preview:")
            self._log(traceback.format_exc())
            messagebox.showerror("Error", "Exception while building preview. See log for details.")
            return

        would_change = sum(1 for r in preview if r.changed)
        if would_change == 0:
            messagebox.showinfo("Nothing to do", "No files would be changed (based on preview).")
            self._log("No changes detected in preview.")
            return

        ok = messagebox.askyesno(
            "Confirm apply",
            f"This will MODIFY files.\n\nFiles that would change: {would_change}\n\nProceed?",
        )
        if not ok:
            self._log("Apply canceled by user.")
            return

        self._log("-" * 90)
        self._log("APPLY CHANGES")
        self._log(f"Target     : {target}")
        self._log(f"Glob       : {glob_pattern}")
        self._log(f"Recurse    : {recurse}   Max depth: {max_depth if recurse else 0}")
        self._log(f"Backup dir : {backup_dir_name}")
        self._log(f"Rename file: {rename_files}")
        self._log(f"Find       : {find!r}")
        self._log(f"Replace    : {replace!r}")
        self._log("-" * 90)

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
            self._log("ERROR: Exception while applying changes:")
            self._log(traceback.format_exc())
            messagebox.showerror("Error", "Exception while applying changes. See log for details.")
            return

        self._last_results = results
        self._last_mode = "APPLY"

        changed, skipped, failed = self._render_results(results, dry_run=False)
        report_path = self._auto_write_report(target, results, mode="APPLY")
        if report_path:
            self._log(f"Report CSV: {report_path}")

        if failed == 0:
            messagebox.showinfo("Apply complete", f"Changed: {changed}\nSkipped: {skipped}\nFailed: {failed}")
        else:
            messagebox.showwarning("Completed with errors", f"Changed: {changed}\nSkipped: {skipped}\nFailed: {failed}\n\nSee log.")

    def _save_report_as(self) -> None:
        if not self._last_results:
            messagebox.showinfo("No results", "Run Preview or Apply first.")
            return

        p = filedialog.asksaveasfilename(
            title="Save report CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"find_replace_report__{_now_stamp()}.csv",
        )
        if not p:
            return

        try:
            write_report_csv(Path(p), self._last_results, mode=self._last_mode or "UNKNOWN")
            self._log(f"Report saved: {p}")
            messagebox.showinfo("Saved", f"Report saved:\n{p}")
        except Exception:
            self._log("ERROR: Could not save report CSV:")
            self._log(traceback.format_exc())
            messagebox.showerror("Error", "Could not save report. See log for details.")


def main() -> int:
    try:
        app = App()
        app.mainloop()
        return 0
    except Exception:
        print(f"{__app_name__} v{__version__}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
