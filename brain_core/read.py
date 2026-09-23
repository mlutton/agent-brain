"""Implements `brain read` (see docs/specification/beta.md, C8, C10 and C13a).

Seam: `run_read` is the whole read interface. bin/brain hands it the parsed
request, the brain root and the loaded vendored yaml module and gets back
exactly one report dict plus an exit code (C1). Hidden behind it: locating the
file by id or path, applying the publication rule, reading a retained
original's publication record from git history, parsing and bounding the
content, and resolving the evidence references. No caller sequences any of it.
"""

import datetime
import os

from . import publication, typedefs
from .publication import _GitUnavailable, _parse, _sha256, _read_bytes


def _jsonable(value):
    """`value` as JSON can carry it: a date reads as its ISO text (the vendored
    YAML 1.1 loader reads an unquoted `YYYY-MM-DD` as a date), keys as text."""
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _bounded(data, max_bytes):
    """The longest prefix of `data` within `max_bytes` that ends on a character
    boundary, and whether anything was cut."""
    cut = min(max_bytes, len(data))
    while 0 < cut < len(data) and (data[cut] & 0xC0) == 0x80:
        cut -= 1
    return data[:cut].decode("utf-8"), len(data) > max_bytes


def _zone_path(path, module_folders):
    """`path` when it is a plain relative path into a zone read scans
    (documents, wiki, an installed module folder), with no hidden name; else None."""
    if not isinstance(path, str) or not path:
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return None
    if parts[0] not in ["documents", "wiki"] + sorted(set(module_folders.values())):
        return None
    return path


def _readable_path(root, path):
    """`path` when it names a regular file reached without a symbolic link."""
    current = root
    for part in path.split("/"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return None
    return path if os.path.isfile(current) else None


def _id_index(root, module_folders, yaml_module, duplicate_loader, pub):
    """{id: path} for every Markdown file in a scanned zone that carries a
    frontmatter `id`, except a retained original or attachment, which is never
    indexed (C13a); where two files carry one id, the first path in code-point
    order wins."""
    index = {}
    entries, _scan_skipped = pub.paths()
    for rel_path, _zone in entries:
        record = pub.records.get(rel_path)
        if record is not None and record["role"] in ("original", "attachment"):
            continue
        try:
            parsed = _parse(_read_bytes(root, rel_path), yaml_module, duplicate_loader)
        except OSError:
            continue
        doc_id = parsed.mapping.get("id") if parsed.kind != "malformed" else None
        if isinstance(doc_id, str):
            index.setdefault(doc_id, rel_path)
    return index


def _evidence_of(mapping, index):
    """C10's `evidence`: each reference with whether it resolves. An external
    URL is returned as written and never fetched, so whether it resolves is
    unknown (`null`), not false."""
    entries = []
    for target in mapping.get("evidence") or []:
        if not isinstance(target, str):
            continue
        if target.startswith(("http://", "https://")):
            entries.append({"target": target, "exists": None})
        else:
            entries.append({"target": target, "exists": target in index})
    return entries


def _retained(request, root, path, record):
    """C13a: a file with role identity is read as evidence, never parsed. Its
    integrity is judged against the recorded bytes, and never changes its role."""
    result = {
        "outcome": "ok",
        "path": path,
        "role": record["role"],
        "owner": record["owner"],
        "recorded_sha256": record["sha256"],
        "publication_commit": record["commit"],
    }
    if _readable_path(root, path) is None:
        # The file is gone. This story reports the state and delivers no
        # content fields for it.
        result["integrity"] = "retained_missing"
        return result, 0
    current = _read_bytes(root, path)
    try:
        # Text or not is decided on the whole file, so it never changes with
        # `max_bytes`. Bytes that are not text have no excerpt, and a budget
        # does not apply to them.
        current.decode("utf-8")
    except UnicodeDecodeError:
        excerpt, truncated = None, None
    else:
        excerpt, truncated = _bounded(current, request["max_bytes"])
    result.update(
        {
            "integrity": "retained" if _sha256(current) == record["sha256"] else "retained_changed",
            "version": _sha256(current),
            "excerpt": excerpt,
            "truncated": truncated,
        }
    )
    return result, 0


def _content_fields(request, raw_bytes, parsed, index):
    """The fields that are the file's content, which an unpublished or
    unverified file withholds."""
    excerpt, truncated = _bounded(parsed.body.encode("utf-8"), request["max_bytes"])
    return {
        "id": parsed.mapping.get("id"),
        "version": _sha256(raw_bytes),
        "frontmatter": _jsonable(parsed.mapping),
        "evidence": _evidence_of(parsed.mapping, index),
        "origin": _jsonable(parsed.mapping.get("origin", "unknown")),
        "excerpt": excerpt,
        "truncated": truncated,
    }


def _unverified(request, path, raw_bytes, parsed, index, **why):
    """`unverified` (C8): no content unless `include_unverified` asks for it."""
    result = {"outcome": "unverified", "path": path}
    result.update(why)
    if request.get("include_unverified") is True and parsed.kind in ("managed", "unmanaged"):
        result.update(_content_fields(request, raw_bytes, parsed, index))
    return result, 0


def _read_located(request, root, path, verdict, index, yaml_module, duplicate_loader):
    """Shape a publication verdict and any permitted content as a read report."""
    if verdict["status"] in ("retained", "retained_missing"):
        record = {"role": verdict["role"], "owner": verdict["owner"],
                  "sha256": verdict["recorded_sha256"], "commit": verdict["publication_commit"]}
        return _retained(request, root, path, record)
    if verdict["status"] == "unpublished":
        return {"outcome": "unpublished", "path": path}, 0
    if verdict["status"] == "absent":
        return {"outcome": "not_found"}, 0
    if verdict["status"] == "unverified" and verdict["reason"] == "git_unavailable":
        result = {"outcome": "unverified", "reason": "git_unavailable"}
        if _readable_path(root, path) is not None:
            result["path"] = path
        return result, 0
    raw_bytes = _read_bytes(root, path)
    parsed = _parse(raw_bytes, yaml_module, duplicate_loader)
    if verdict["status"] == "unverified":
        why = {key: verdict[key] for key in ("reason", "hint") if verdict[key] is not None}
        return _unverified(request, path, raw_bytes, parsed, index, **why)
    if parsed.kind == "malformed":
        return {"outcome": "invalid"}, 0
    if parsed.kind == "unsupported_version":
        return {"outcome": "unsupported_version"}, 0
    labels = {"changed_out_of_band": True} if verdict["changed_out_of_band"] else {}
    result = {"outcome": "ok", "path": path, "role": verdict["role"], "labels": labels}
    result.update(_content_fields(request, raw_bytes, parsed, index))
    return result, 0


def run_read(request, root, base_dir, yaml_module, duplicate_loader):
    """Reads one document by id or path. Returns (report, exit code)."""
    if "heading" in request:
        # Presence is the test, not truthiness: heading semantics are not
        # delivered, and a request that carries the key is refused, never read.
        return {"outcome": "refused", "reason": "unsupported_heading"}, 2
    module_folders, _skipped = typedefs.detect_installed_modules(root)
    pub = publication.Publication.open(root, os.path.join(root, ".brain", "journal"),
                                       module_folders, yaml_module, duplicate_loader)
    index = _id_index(root, module_folders, yaml_module, duplicate_loader, pub)
    path = request.get("path")
    if path is None:
        path = index.get(request.get("id"))
    path = _zone_path(path, module_folders)
    if path is None:
        return ({"outcome": "unverified", "reason": "git_unavailable"}
                if pub.repo is None else {"outcome": "not_found"}), 0
    try:
        verdict = pub.status(path)
        return _read_located(request, root, path, verdict, index, yaml_module, duplicate_loader)
    except _GitUnavailable:
        # The rule cannot be evaluated, and no default may stand in for its
        # answer: publication is unknown, so the file is unverified.
        return {"outcome": "unverified", "reason": "git_unavailable", "path": path}, 0
