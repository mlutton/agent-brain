"""Implements `brain read` (see docs/specification/beta.md, C8, C10 and C13a).

Seam: `run_read` is the whole read interface. bin/brain hands it the parsed
request, the brain root and the loaded vendored yaml module and gets back
exactly one report dict plus an exit code (C1). Hidden behind it: locating the
file by id or path, applying the publication rule, reading a retained
original's publication record from git history, parsing and bounding the
content, and resolving the evidence references. No caller sequences any of it.
"""

import datetime
import hashlib
import os
import posixpath
import re

from . import frontmatter, gitutil, persist, scan, typedefs


class _GitUnavailable(Exception):
    """Git could not be consulted, so the publication rule cannot be evaluated."""


class _Repo:
    """The brain's git repository, read-only. Every command must succeed or
    be a failure the caller names in advance: a git that fails with empty
    output would otherwise read as an empty status, that is as a clean tree,
    and the rule would find every file published."""

    def __init__(self, root, module_folders, yaml_module, duplicate_loader):
        self.root = root
        self._data_zones = set(persist.DATA_ZONES) | set(module_folders.values())
        self._yaml_module = yaml_module
        self._duplicate_loader = duplicate_loader
        self._records = None
        top = self._run(["rev-parse", "--show-toplevel"]).stdout.strip()
        if not top or os.path.realpath(top) != os.path.realpath(root):
            raise _GitUnavailable(root)

    def _run(self, args):
        try:
            proc = gitutil.run_git(args, self.root)
        except OSError as exc:
            raise _GitUnavailable(str(exc)) from exc
        if proc.returncode != 0:
            raise _GitUnavailable(" ".join(args))
        return proc

    def _has_head(self):
        proc = gitutil.run_git(["rev-parse", "--verify", "-q", "HEAD"], self.root)
        if proc.returncode not in (0, 1):
            raise _GitUnavailable("rev-parse HEAD")
        return proc.returncode == 0

    def _blob(self, commit, path):
        """The bytes of `path` as committed in `commit`, or None if it is not in it."""
        proc = gitutil.run_git_bytes(["cat-file", "blob", f"{commit}:{path}"], self.root)
        if proc.returncode == 0:
            return proc.stdout
        if proc.returncode == 128:
            return None
        raise _GitUnavailable("cat-file")

    def _changed_paths(self, commit):
        out = self._run(["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root", commit]).stdout
        return {name for name in out.split("\0") if name}

    def _consistent(self, commit, entries):
        """C7: every data-zone path the commit changes is declared, every
        declared path's committed content matches its declared hash, and every
        declared path the commit does not change declares `prior` equal to that
        hash. Judged against the commit's own content, never the file as it is
        now."""
        changed = self._changed_paths(commit)
        declared = {entry["path"] for entry in entries}
        for name in changed:
            if name.split("/", 1)[0] in self._data_zones and name not in declared:
                return False
        for entry in entries:
            blob = self._blob(commit, entry["path"])
            if blob is None or _sha256(blob) != entry["sha256"]:
                return False
            if entry["path"] not in changed and entry["prior"] != entry["sha256"]:
                return False
        return True

    def _owner(self, commit, entries, path):
        """The role=document path in this commit whose `original` or
        `attachments` names `path` (C13a: the record is made in the same
        commit as the document that references the file)."""
        for entry in entries:
            if entry["role"] != "document":
                continue
            blob = self._blob(commit, entry["path"])
            if blob is None:
                continue
            parsed = _parse(blob, self._yaml_module, self._duplicate_loader)
            if path in _references_of(parsed, entry["path"]):
                return entry["path"]
        return None

    def records(self):
        """{path: record} for every path a publication commit declares a role
        for, the newest commit winning. A record is {role, sha256, commit} and,
        for an original or attachment, `owner`, the document naming it."""
        if self._records is None:
            self._records = {}
            if self._has_head():
                log = self._run(["log", "--no-merges", "--format=%H%x1f%B%x1e"]).stdout
                for chunk in log.split("\x1e"):
                    if "\x1f" not in chunk:
                        continue
                    commit, message = chunk.strip("\n").split("\x1f", 1)
                    entries = _publication_entries(message)
                    if not entries or not self._consistent(commit, entries):
                        continue
                    for entry in entries:
                        if entry["role"] is None or entry["path"] in self._records:
                            continue
                        record = {"role": entry["role"], "sha256": entry["sha256"], "commit": commit}
                        if entry["role"] != "document":
                            record["owner"] = self._owner(commit, entries, entry["path"])
                            if record["owner"] is None:
                                continue
                        self._records[entry["path"]] = record
        return self._records

    def has_uncommitted_change(self, rel_path):
        """True when the file differs from `HEAD`: modified, staged, untracked
        or ignored. A clean tracked file is the only one that is committed."""
        out = self._run(
            ["--literal-pathspecs", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored", "--", rel_path]
        ).stdout
        return out != ""


_TRAILER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*): (.*)$")
_PATH_ENTRY = re.compile(
    r"^(?P<path>.+) sha256=(?P<sha256>[0-9a-f]{64}) prior=(?P<prior>\S+)(?: role=(?P<role>document|original|attachment))?$"
)


def _trailers(message):
    """The `Key: value` pairs of a commit message's final paragraph, or None
    when that paragraph is not made of trailers alone (C7)."""
    lines = message.rstrip().splitlines()
    while lines and lines[-1].strip() == "":
        lines.pop()
    start = len(lines)
    while start > 0 and lines[start - 1].strip() != "":
        start -= 1
    final = lines[start:]
    pairs = []
    for line in final:
        match = _TRAILER_LINE.match(line)
        if match is None:
            return None
        pairs.append((match.group(1), match.group(2)))
    return pairs


def _publication_entries(message):
    """The `Brain-Path` entries of an ingest or adopt commit's trailer, or []."""
    pairs = _trailers(message)
    if not pairs:
        return []
    ops = [value for key, value in pairs if key == "Brain-Op"]
    if len(ops) != 1 or ops[0] not in ("ingest", "adopt"):
        return []
    entries = []
    for key, value in pairs:
        if key != "Brain-Path":
            continue
        match = _PATH_ENTRY.match(value)
        if match is None:
            return []
        entries.append(match.groupdict())
    return entries


def _references_of(parsed, doc_path):
    """The paths a parsed document names through `original` and `attachments`,
    resolved from the document's own folder."""
    if parsed.kind == "malformed":
        return set()
    names = []
    original = parsed.mapping.get("original")
    if isinstance(original, str):
        names.append(original)
    attachments = parsed.mapping.get("attachments")
    if isinstance(attachments, list):
        names.extend(item for item in attachments if isinstance(item, str))
    folder = posixpath.dirname(doc_path)
    return {posixpath.normpath(posixpath.join(folder, name)) for name in names}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(root, rel_path):
    with open(os.path.join(root, rel_path), "rb") as fh:
        return fh.read()


class _Parsed:
    """What C4's classification finds in a file's bytes: `kind` is `malformed`,
    `unmanaged`, `unsupported_version` or `managed`; `mapping` the frontmatter
    mapping ({} when there is none); `body` the text after the frontmatter."""

    def __init__(self, kind, mapping=None, body=None):
        self.kind = kind
        self.mapping = mapping if mapping is not None else {}
        self.body = body


def _parse(raw_bytes, yaml_module, duplicate_loader):
    """C4's classification of a file, without C5's field validation."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return _Parsed("malformed")
    block = frontmatter.split_frontmatter(text)
    if block is None:
        return _Parsed("unmanaged", {}, text)
    if block is frontmatter.UNCLOSED:
        return _Parsed("malformed")
    try:
        features, _count = frontmatter.scan_features(block.block_text, yaml_module)
        if features is None:
            return _Parsed("malformed")
        documents = list(yaml_module.load_all(block.block_text, Loader=duplicate_loader))
    except (yaml_module.YAMLError, ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError):
        return _Parsed("malformed")
    mapping = documents[0] if documents else {}
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        return _Parsed("malformed")
    if "kb" not in mapping:
        return _Parsed("unmanaged", mapping, block.body_text)
    kb_value = mapping["kb"]
    if isinstance(kb_value, bool) or not isinstance(kb_value, int):
        return _Parsed("malformed")
    if kb_value != 1:
        return _Parsed("unsupported_version", mapping, block.body_text)
    return _Parsed("managed", mapping, block.body_text)


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


def _id_index(root, module_folders, yaml_module, duplicate_loader, retained):
    """{id: path} for every Markdown file in a scanned zone that carries a
    frontmatter `id`, except a retained original or attachment, which is never
    indexed (C13a); where two files carry one id, the first path in code-point
    order wins."""
    index = {}
    entries, _scan_skipped = scan.scan_zones(root, module_folders)
    for rel_path, _zone in entries:
        if rel_path in retained:
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


def _named_by_open_intent(root, path):
    """C8 rule 1: an open intent in the journal names the file."""
    return any(intent is not None and intent["path"] == path for _name, intent in persist.open_intents(root))


def _has_unverified_referrer(root, path, repo, module_folders, yaml_module, duplicate_loader):
    """C8 rule 6: a `document` that rule 5 makes unverified names this file
    through `original` or `attachments`. The rule states no locality condition,
    so every document in the scanned zones is considered: `original` is
    relative to the referring document's own folder (C5), and that admits
    `../`, so a referrer may sit anywhere in the brain and still resolve to
    this file. What taints is the reference resolving here, never where the
    referrer sits.

    A path the publication record names is never such a referrer, and its
    record decides that before its bytes are read. C8's rules are first-match:
    rule 2 takes a retained original or attachment "whatever its current
    bytes", so it is "never a candidate", and C13a says it is never parsed as
    frontmatter at all; rule 5 leaves a recorded `document` published, so it
    cannot be the unverified one this rule asks for. Bytes that read as a
    processed document therefore cannot make a recorded path speak for one."""
    records = repo.records()
    entries, _scan_skipped = scan.scan_zones(root, module_folders)
    for candidate, _zone in entries:
        if candidate == path or candidate in records:
            continue
        try:
            parsed = _parse(_read_bytes(root, candidate), yaml_module, duplicate_loader)
        except OSError:
            continue
        if parsed.kind != "managed" or parsed.mapping.get("type") != "document":
            continue
        if path in _references_of(parsed, candidate):
            return True
    return False


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


def _open_repo(root, module_folders, yaml_module, duplicate_loader):
    """(repo, paths with role identity), or (None, empty) when git cannot be consulted."""
    try:
        repo = _Repo(root, module_folders, yaml_module, duplicate_loader)
        retained = {path for path, record in repo.records().items() if record["role"] != "document"}
    except _GitUnavailable:
        return None, set()
    return repo, retained


def _read_located(request, root, path, repo, index, module_folders, yaml_module, duplicate_loader):
    """Everything after the file is found and rule 1 has passed: the rest of
    the publication rule, then the content. Raises _GitUnavailable when the
    rule cannot be evaluated."""
    record = repo.records().get(path)
    if record is not None and record["role"] in ("original", "attachment"):
        return _retained(request, root, path, record)
    raw_bytes = _read_bytes(root, path)
    parsed = _parse(raw_bytes, yaml_module, duplicate_loader)
    if _has_unverified_referrer(root, path, repo, module_folders, yaml_module, duplicate_loader):
        return _unverified(request, path, raw_bytes, parsed, index, hint="suspected_ingestion")
    role = "note"
    if record is not None and record["role"] == "document":
        role = "document"
    elif parsed.kind == "managed" and parsed.mapping.get("type") == "document":
        # Rule 5: a processed document is published only by an ingest or adopt
        # record. Its own `type` says what it claims to be, not what it is.
        return _unverified(request, path, raw_bytes, parsed, index, reason="no_ingest_record")
    labels = {}
    if repo.has_uncommitted_change(path):
        if not persist.journal_present(root):
            return _unverified(request, path, raw_bytes, parsed, index, reason="journal_missing")
        labels["changed_out_of_band"] = True
    if parsed.kind == "malformed":
        return {"outcome": "invalid"}, 0
    if parsed.kind == "unsupported_version":
        return {"outcome": "unsupported_version"}, 0
    result = {"outcome": "ok", "path": path, "role": role, "labels": labels}
    result.update(_content_fields(request, raw_bytes, parsed, index))
    return result, 0


def run_read(request, root, base_dir, yaml_module, duplicate_loader):
    """Reads one document by id or path. Returns (report, exit code)."""
    if "heading" in request:
        # Presence is the test, not truthiness: heading semantics are not
        # delivered, and a request that carries the key is refused, never read.
        return {"outcome": "refused", "reason": "unsupported_heading"}, 2
    module_folders, _skipped = typedefs.detect_installed_modules(root)
    repo, retained = _open_repo(root, module_folders, yaml_module, duplicate_loader)
    index = _id_index(root, module_folders, yaml_module, duplicate_loader, retained)
    path = request.get("path")
    if path is None:
        path = index.get(request.get("id"))
    path = _zone_path(path, module_folders)
    unevaluable = {"outcome": "unverified", "reason": "git_unavailable"}
    if path is not None and _readable_path(root, path) is None:
        # Not there to read. A retained path whose file is gone is still
        # reported from its record (C13a); anything else is simply absent.
        record = repo.records().get(path) if repo is not None else None
        if record is not None and record["role"] != "document":
            return _retained(request, root, path, record)
        path = None
    if path is None:
        return (unevaluable if repo is None else {"outcome": "not_found"}), 0
    # Rule 1 comes before the file is even opened: a file an unfinished
    # operation owns is not published content, however it happens to parse.
    if _named_by_open_intent(root, path):
        return {"outcome": "unpublished", "path": path}, 0
    try:
        if repo is None:
            raise _GitUnavailable(path)
        return _read_located(request, root, path, repo, index, module_folders, yaml_module, duplicate_loader)
    except _GitUnavailable:
        # The rule cannot be evaluated, and no default may stand in for its
        # answer: publication is unknown, so the file is unverified.
        return dict(unevaluable, path=path), 0
