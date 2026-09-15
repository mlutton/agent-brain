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
    def add_unreadable(path):
        skipped.append({"path": _rel_posix(root, path), "reason": "unreadable"})

    def walk(folder, zone):
        try:
            with os.scandir(folder) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            add_unreadable(folder)
            return

        for entry in children:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink():
                    skipped.append({"path": _rel_posix(root, entry.path), "reason": "symlink"})
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                if entry.name.endswith(".md"):
                    add_unreadable(entry.path)
                continue
            if is_directory:
                walk(entry.path, zone)
            elif entry.name.endswith(".md"):
                entries.append((_rel_posix(root, entry.path), zone))

    for zone in zones:
        zone_path = os.path.join(root, zone)
        if os.path.islink(zone_path):
            skipped.append({"path": _rel_posix(root, zone_path), "reason": "symlink"})
            continue
        if not os.path.isdir(zone_path):
            continue
        walk(zone_path, zone)
    entries.sort(key=lambda e: e[0])
    skipped.sort(key=lambda e: e["path"])
    return entries, skipped
