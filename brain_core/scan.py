"""Walks a brain's data zones per specification C5a scan rules."""

import os


def _rel_posix(root, full):
    return os.path.relpath(full, root).replace(os.sep, "/")


def scan_zones(root, module_folders):
    """Returns (entries, skipped).

    entries is a sorted list of (relative_posix_path, zone) for every scanned
    Markdown file. skipped is a sorted list of {"path", "reason"} dicts for
    symlinks encountered inside a scanned zone.
    """
    zones = ["documents", "wiki"] + sorted(set(module_folders.values()))
    entries = []
    skipped = []
    for zone in zones:
        zone_path = os.path.join(root, zone)
        if not os.path.isdir(zone_path) or os.path.islink(zone_path):
            continue
        for dirpath, dirnames, filenames in os.walk(zone_path, followlinks=False):
            kept_dirs = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    skipped.append({"path": _rel_posix(root, full), "reason": "symlink"})
                    continue
                if d.startswith("."):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for f in filenames:
                full = os.path.join(dirpath, f)
                if os.path.islink(full):
                    skipped.append({"path": _rel_posix(root, full), "reason": "symlink"})
                    continue
                if f.startswith("."):
                    continue
                if not f.endswith(".md"):
                    continue
                entries.append((_rel_posix(root, full), zone))
    entries.sort(key=lambda e: e[0])
    skipped.sort(key=lambda e: e["path"])
    return entries, skipped
