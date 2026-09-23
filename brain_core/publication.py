"""C8 publication verdicts for data-zone paths.

The interface hides journal, history, referrer and working-tree decisions behind
``open``, ``status`` and ``paths``. Callers shape their own reports.
"""

import hashlib
import os
import posixpath
import re

from . import frontmatter, gitutil, persist, scan

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
        self._blobs = {}
        self._parents = None
        self._descent = {}
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
        key = (commit, path)
        if key in self._blobs:
            return self._blobs[key]
        proc = gitutil.run_git_bytes(["cat-file", "blob", f"{commit}:{path}"], self.root)
        if proc.returncode == 0:
            blob = proc.stdout
        elif proc.returncode == 128:
            blob = None
        else:
            raise _GitUnavailable("cat-file")
        self._blobs[key] = blob
        return blob

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

    def descends(self, path, record):
        """Follow every lineage that carries the path's blob back to its record."""
        head = self._run(["rev-parse", "HEAD"]).stdout.strip()
        key = (path, record["commit"], head)
        if key in self._descent:
            return self._descent[key]
        if self._parents is None:
            lines = self._run(["rev-list", "--parents", head]).stdout.splitlines()
            self._parents = {parts[0]: parts[1:] for line in lines if (parts := line.split())}
        seen = {}
        stack = [(head, False)]
        while stack:
            commit, ready = stack.pop()
            if commit in seen:
                continue
            if commit == record["commit"]:
                seen[commit] = True
                continue
            parents = self._parents.get(commit, [])
            if not parents:
                seen[commit] = False
                continue
            blob = self._blob(commit, path)
            if len(parents) > 1:
                following = [parent for parent in parents if self._blob(parent, path) == blob]
                valid = bool(following)
            else:
                parent = parents[0]
                following = [parent]
                valid = True
                if self._blob(parent, path) != blob:
                    message = self._run(["show", "-s", "--format=%B", commit]).stdout
                    entries = _operation_entries(message, "update")
                    valid = (any(entry["path"] == path for entry in entries)
                             and self._consistent(commit, entries))
            if not valid:
                seen[commit] = False
            elif ready:
                seen[commit] = all(seen[parent] for parent in following)
            else:
                stack.append((commit, True))
                stack.extend((parent, False) for parent in following if parent not in seen)
        self._descent[key] = seen[head]
        return seen[head]


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


def _operation_entries(message, operation):
    pairs = _trailers(message)
    if not pairs:
        return []
    ops = [value for key, value in pairs if key == "Brain-Op"]
    if ops != [operation]:
        return []
    entries = []
    for key, value in pairs:
        if key == "Brain-Path":
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


class Publication:
    """One command's journal, publication records and path verdicts."""

    def __init__(self, root, journal, module_folders, yaml_module, duplicate_loader):
        self.root = root
        self.journal = journal
        self.module_folders = module_folders
        self.yaml_module = yaml_module
        self.duplicate_loader = duplicate_loader
        self.intents = {intent["path"] for _name, intent in persist.open_intents(root) if intent is not None}
        try:
            self.repo = _Repo(root, module_folders, yaml_module, duplicate_loader)
            self.records = self.repo.records()
        except _GitUnavailable:
            self.repo = None
            self.records = {}
        self._referrers = None

    @classmethod
    def open(cls, root, journal, module_folders, yaml_module, duplicate_loader):
        return cls(root, journal, module_folders, yaml_module, duplicate_loader)

    def paths(self):
        return scan.scan_zones(self.root, self.module_folders)

    def _readable(self, path):
        current = self.root
        for part in path.split("/"):
            current = os.path.join(current, part)
            if os.path.islink(current):
                return False
        return os.path.isfile(current)

    def _unverified_referrers(self):
        """Map references from unverified documents. Exclude retained paths
        with original or attachment records, and recorded documents only while
        rule 5 publishes them: a recorded document failing descent can refer."""
        if self._referrers is None:
            self._referrers = set()
            entries, _skipped = self.paths()
            for candidate, _zone in entries:
                record = self.records.get(candidate)
                if record is not None:
                    if record["role"] != "document" or self.repo.descends(candidate, record):
                        continue
                try:
                    parsed = _parse(_read_bytes(self.root, candidate), self.yaml_module, self.duplicate_loader)
                except OSError:
                    continue
                if parsed.kind == "managed" and (record is not None or parsed.mapping.get("type") == "document"):
                    self._referrers.update(_references_of(parsed, candidate))
        return self._referrers

    def status(self, path):
        verdict = dict(status="absent", reason=None, hint=None, role=None,
                       owner=None, integrity=None, recorded_sha256=None,
                       publication_commit=None, changed_out_of_band=False)
        if path in self.intents:
            verdict["status"] = "unpublished"
            return verdict
        if self.repo is None:
            verdict.update(status="unverified", reason="git_unavailable")
            return verdict
        record = self.records.get(path)
        if record is not None and record["role"] in ("original", "attachment"):
            verdict.update(status="retained", role=record["role"], owner=record["owner"],
                           recorded_sha256=record["sha256"], publication_commit=record["commit"])
            if not self._readable(path):
                verdict.update(status="retained_missing", integrity="retained_missing")
            else:
                current = _read_bytes(self.root, path)
                verdict["integrity"] = "retained" if _sha256(current) == record["sha256"] else "retained_changed"
            return verdict
        if not self._readable(path):
            return verdict
        if record is not None and record["role"] == "document":
            verdict["role"] = "document"
            if not self.repo.descends(path, record):
                verdict.update(status="unverified", reason="no_ingest_record")
                return verdict
        else:
            parsed = _parse(_read_bytes(self.root, path), self.yaml_module, self.duplicate_loader)
            if parsed.kind == "managed" and parsed.mapping.get("type") == "document":
                verdict.update(status="unverified", reason="no_ingest_record")
                return verdict
            verdict["role"] = "note"
        if path in self._unverified_referrers():
            verdict.update(status="unverified", hint="suspected_ingestion")
            return verdict
        if self.repo.has_uncommitted_change(path):
            if not persist.journal_present(self.root):
                verdict.update(status="unverified", reason="journal_missing")
                return verdict
            verdict["changed_out_of_band"] = True
        verdict["status"] = "published"
        return verdict
