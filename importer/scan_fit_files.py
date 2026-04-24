"""
scan_fit_files.py — Discover all .fit files under the data directories.

Returns a list of dicts:
    source       str   'garmin_export' | 'garmin_device'
    file_name    str   basename
    source_path  str   absolute path
    file_size_b  int   bytes

Does NOT compute hashes here — that happens in import_metadata.py so we
can skip hashing for files already tracked by path (fast-path).
"""
import os
import sys
from pathlib import Path

# Directories to scan, relative to project root
SCAN_ROOTS = {
    "garmin_export": os.path.join("data", "garmin_export"),
    "garmin_device": os.path.join("data", "garmin_device"),
}


def scan(project_root: str, verbose: bool = False) -> list:
    """
    Recursively find all .fit files in both source directories.

    Args:
        project_root: Absolute path to the project root directory.
        verbose:      Print per-directory progress if True.

    Returns:
        List of file-info dicts, sorted by source_path.
    """
    results = []
    for source, rel_path in SCAN_ROOTS.items():
        root = os.path.join(project_root, rel_path)

        if not os.path.isdir(root):
            print(f"  [SKIP] {rel_path} does not exist — skipping {source}")
            continue

        count_before = len(results)
        for dirpath, _dirs, filenames in os.walk(root):
            fit_files = [f for f in filenames if f.lower().endswith(".fit")]
            if verbose and fit_files:
                rel_dir = os.path.relpath(dirpath, root)
                print(f"  {rel_dir}: {len(fit_files)} file(s)")

            for fname in fit_files:
                full_path = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(full_path)
                    results.append({
                        "source":      source,
                        "file_name":   fname,
                        "source_path": full_path,
                        "file_size_b": stat.st_size,
                    })
                except OSError as exc:
                    print(f"  [WARN] Cannot stat {full_path}: {exc}", file=sys.stderr)

        found = len(results) - count_before
        print(f"  {source}: {found} FIT file(s) found")

    results.sort(key=lambda r: r["source_path"])
    return results


if __name__ == "__main__":
    # Quick standalone test: print counts
    root = Path(__file__).resolve().parent.parent
    print(f"Scanning from: {root}")
    files = scan(str(root), verbose=True)
    print(f"\nTotal: {len(files)} FIT files")
