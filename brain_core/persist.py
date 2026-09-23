"""Implements `brain persist` -- create, update, attach (see docs/specification/beta.md, C6).

Seam: this module is the whole persist interface. A caller (bin/brain) hands
it a request dict, the brain root and the loaded vendored yaml module, and
gets back exactly one outcome dict plus an exit code (C1). Everything C6
sequences -- journalling an intent, writing and verifying a temp file,
re-checking the target, hard-linking or backup-and-replace, hashing,
writing a change record and making a git commit with consistent trailers --
is mechanics hidden behind that one call; no caller ever sequences those
steps itself.

Design note: `brain_core/setup.py` already has its own module-private `_git`,
`_hash_file` and `_hardlink_probe` helpers. Persist needs a subprocess `git`
wrapper and a file-hash helper too. Rather than reach into `setup`'s private
helpers (coupling two independent command modules through each other's
internals) or duplicate them silently, the small, genuinely shared
primitives live in `brain_core/gitutil.py`: a real second seam (two call
sites -- setup.py, once ST-06/later stories land, and persist.py, now), not
a hypothetical one. `setup.py` itself is left unchanged this round: it
already passes its own tests, and touching a working module outside the TDD
loop that owns it is a refactor, not a behaviour change.

A second interface lives beside `run_persist`, for `brain_core/recover.py`: the
intent journal, the temp-file naming, the change record and the commit, each
in the one form persist writes them, under public names. It exists so that
recovery finishes an interrupted write by calling the same code that would have
finished it, and never keeps its own copy of the record shape, the intent path
scheme or the trailer format. Those names are: `journal_present`,
`open_intents`, `close_intent`, `TEMP_PREFIX`, `is_temp_name`, `DATA_ZONES`,
`change_record_exists`, `record_change`, `is_git_brain`, `commit_exists` and
`commit_write`.
"""

import datetime
import os
import signal

from . import fields as fields_mod
from . import frontmatter, gitutil, typedefs

_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase, no i l o u

FAULT_ENV = "BRAIN_TEST_FAULTS"
FAULT_VAR = "BRAIN_FAULT"

DATA_ZONES = ("inbox", "raw", "documents", "wiki")

# The temp file persist writes beside its target (C6 step 3). The name is
# contract: `recover` reports any file carrying it that no open intent owns as
# an `orphan_temp`, so persist and recover both read it from here.
TEMP_PREFIX = ".brain-persist-tmp-"


class _Fault(Exception):
    def __init__(self, step):
        super().__init__(step)
        self.step = step


def _check_fault(step):
    if os.environ.get(FAULT_ENV) != "1":
        return
    spec = os.environ.get(FAULT_VAR)
    if not spec or ":" not in spec:
        return
    fault_step, mode = spec.split(":", 1)
    if fault_step != step:
        return
    if mode == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    elif mode == "fail":
        raise _Fault(step)


_CONFLICT_ENV = "BRAIN_TEST_CONFLICT"


def _check_conflict_injection(root, path, target_full):
    """Test-only instrumentation, gated behind BRAIN_TEST_FAULTS like
    `_check_fault` but not part of C6's kill/fail fault contract: it writes a
    conflicting file at the target path, landing deliberately in the gap
    between step 4's re-check (just above this call's own call site in
    run_persist) and step 5's apply. This is the only way to reach the apply
    step with the target already present -- the request shape gives create
    no way to declare `expected_prior` as anything but "absent" (A7), so an
    ordinary run's own step-4 check always catches a target that exists
    before persist starts. Exercises the apply step's own no-overwrite
    guarantee (Behaviour 14) directly, rather than assuming it from reading
    `_apply_create_or_attach`."""
    if os.environ.get(FAULT_ENV) != "1":
        return
    if os.environ.get(_CONFLICT_ENV) != path:
        return
    with open(target_full, "wb") as fh:
        fh.write(b"conflicting out-of-band content")


def _mint_id():
    """Mints a real ULID (C4): a 48-bit millisecond timestamp followed by 80
    bits of randomness, encoded as 26 lowercase Crockford base32 characters.
    The 128-bit value fits inside the 130 bits the 26 characters can hold,
    so the first character's own top two bits are always zero -- it never
    exceeds the alphabet's eighth symbol."""
    import secrets
    import time

    timestamp_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    randomness = int.from_bytes(secrets.token_bytes(10), "big")
    value = (timestamp_ms << 80) | randomness
    chars = []
    for i in range(26):
        shift = 5 * (25 - i)
        chars.append(_ID_ALPHABET[(value >> shift) & 0x1F])
    return "".join(chars)


def _refused(reason, observed_hash=None):
    doc = {"outcome": "refused", "reason": reason}
    if observed_hash is not None:
        doc["observed_sha256"] = observed_hash
    return doc, 2


def _zone_of(path):
    return path.split("/", 1)[0]


def journal_present(root):
    epoch_path = os.path.join(root, ".brain", "journal", "epoch.json")
    if not os.path.isfile(epoch_path):
        return False
    try:
        data = gitutil.read_json(epoch_path)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("brain_id"))


def _intents_dir(root):
    return os.path.join(root, ".brain", "journal", "intents")


def _change_records_dir(root):
    return os.path.join(root, ".brain", "journal", "change_records")


def _intent_path(root, path):
    """One intent file per data-zone path (not per op_key): a path with any
    open intent refuses further writes (Behaviour 21), regardless of which
    operation opened it."""
    safe = path.replace("/", "__")
    return os.path.join(_intents_dir(root), safe + ".json")


def _has_open_intent(root, path):
    return os.path.isfile(_intent_path(root, path))


def _write_intent(root, path, record):
    os.makedirs(_intents_dir(root), exist_ok=True)
    gitutil.write_json(_intent_path(root, path), record)


def _remove_intent(root, path):
    try:
        os.remove(_intent_path(root, path))
    except FileNotFoundError:
        pass


_INTENT_FIELDS = {
    "run_id": str,
    "op_key": str,
    "operation": str,
    "path": str,
    "expected_prior": str,
    "intended_sha256": str,
    "temp_path": str,
}


def _well_formed_intent(record):
    """True when `record` carries every field C6 step 2 records, with the right
    types. `backup_path` is a string or null (only `update` backs up)."""
    if not isinstance(record, dict):
        return False
    for name, kind in _INTENT_FIELDS.items():
        if not isinstance(record.get(name), kind):
            return False
    return record.get("backup_path") is None or isinstance(record.get("backup_path"), str)


def open_intents(root):
    """Every open intent in the journal, as `(file_name, record)` sorted by
    file name. `record` is the intent as persist wrote it, or None when the
    file cannot be read as JSON or lacks a field C6 step 2 records -- an intent
    the caller must not act on but must not lose sight of either."""
    directory = _intents_dir(root)
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    found = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            record = gitutil.read_json(os.path.join(directory, name))
        except (OSError, ValueError):
            record = None
        found.append((name, record if _well_formed_intent(record) else None))
    return found


def close_intent(root, path):
    """Removes the intent for `path` (one file per data-zone path)."""
    _remove_intent(root, path)


def _write_change_record(root, op_key, record):
    directory = _change_records_dir(root)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, op_key + ".json")
    if os.path.isfile(path):
        return  # at most one change record per op_key (C6)
    gitutil.write_json(path, record)


def change_record_exists(root, op_key):
    return os.path.isfile(os.path.join(_change_records_dir(root), op_key + ".json"))


def record_change(root, intent, applied_hash):
    """C6 step 7's change record for the write `intent` describes, once per
    `op_key`: a record that already exists is left exactly as it is."""
    _write_change_record(
        root,
        intent["op_key"],
        {
            "op_key": intent["op_key"],
            "operation": intent["operation"],
            "run_id": intent["run_id"],
            "path": intent["path"],
            "sha256": applied_hash,
            "prior": intent["expected_prior"],
        },
    )


def _read_current(root, path):
    full = os.path.join(root, path)
    if not os.path.isfile(full):
        return None
    with open(full, "rb") as fh:
        return fh.read()


def _classify_existing(raw_bytes, yaml_module):
    """A light classification of an existing target used only to decide
    whether persist may write to it at all (unsupported_version / malformed /
    unsupported-for-editing block); full C5 field validation is applied
    separately to the document persist is about to write."""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return "malformed", None, None
    fm = frontmatter.split_frontmatter(text)
    if fm is None or fm is frontmatter.UNCLOSED:
        return "unmanaged", None, None
    try:
        features, _ = frontmatter.scan_features(fm.block_text, yaml_module)
    except RecursionError:
        return "malformed", None, None
    if features is None:
        return "malformed", None, None
    try:
        documents = list(yaml_module.load_all(fm.block_text, Loader=yaml_module.SafeLoader))
    except (yaml_module.YAMLError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        return "malformed", None, None
    mapping = documents[0] if documents else {}
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        return "malformed", None, None
    if "kb" not in mapping:
        return "unmanaged", fm, features
    kb_value = mapping.get("kb")
    if isinstance(kb_value, bool) or not isinstance(kb_value, int):
        return "malformed", fm, features
    if kb_value != 1:
        return "unsupported_version", fm, features
    if features:
        return "unsupported_for_editing", fm, features
    return "managed", fm, features


def _build_type_registry(root, base_dir):
    registry = typedefs.load_base_types(base_dir)
    typedefs.load_custom_types(registry, root)
    return registry


def _mapping_errors(mapping, root, base_dir, zone):
    registry = _build_type_registry(root, base_dir)
    return fields_mod.validate_mapping(mapping, registry, zone)


def _op_key(run_id, path, intended_sha256):
    return gitutil.hash_text("\0".join([run_id, path, intended_sha256]))


def _commit_trailers(op, run_id, path_entries, grant=None):
    lines = [f"Brain-Op: {op}", f"Brain-Run: {run_id}"]
    for key in path_entries.get("keys", []):
        lines.append(f"Brain-Key: {key}")
    for entry in path_entries.get("paths", []):
        lines.append(f"Brain-Path: {entry}")
    if grant is not None:
        lines.append(f"Brain-Grant: {grant} maintenance=agent by=agent")
    return "\n".join(lines)


def is_git_brain(root):
    return os.path.isdir(os.path.join(root, ".git"))


def commit_exists(root, op_key):
    """True when a commit reachable from HEAD carries `Brain-Key: <op_key>` as
    a line of its final trailer paragraph (C7). Raises RuntimeError when git
    cannot answer, so a caller never commits on the strength of a guess."""
    if gitutil.run_git(["rev-parse", "--verify", "-q", "HEAD"], root).returncode != 0:
        return False  # a repository with no commits has no commit for any key
    proc = gitutil.run_git(
        ["log", "-F", "--grep", f"Brain-Key: {op_key}", "--format=%B%x00"], root
    )
    if proc.returncode != 0:
        raise RuntimeError("git log failed")
    for message in proc.stdout.split("\0"):
        paragraphs = message.strip().split("\n\n")
        if f"Brain-Key: {op_key}" in paragraphs[-1].splitlines():
            return True
    return False


def commit_write(root, intent, applied_hash, grant=None):
    """C6 step 7's commit for the write `intent` describes: exactly its own
    path, with the C7 trailers. Callers decide whether one is due."""
    path_entry = f"{intent['path']} sha256={applied_hash} prior={intent['expected_prior']}"
    trailer_paths = {"keys": [intent["op_key"]], "paths": [path_entry]}
    gitutil.commit_paths(
        root,
        [intent["path"]],
        _commit_trailers(intent["operation"], intent["run_id"], trailer_paths, grant=grant),
    )


def _apply_create_or_attach(temp_path, target_path):
    """Hard-links temp onto target; the link fails (no fallback) if target
    already exists (C6 step 5)."""
    os.link(temp_path, target_path)


def _apply_update(temp_path, target_path, backup_path):
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    with open(target_path, "rb") as fh:
        current_bytes = fh.read()
    with open(backup_path, "wb") as fh:
        fh.write(current_bytes)
    os.replace(temp_path, target_path)


def is_temp_name(name):
    """True when a file name is one persist gives its temp files."""
    return name.startswith(TEMP_PREFIX)


def _plan_temp_path(directory):
    """Decides the temp file's path before it is written (C6 step 2 needs it
    recorded in the intent before step 3 creates it)."""
    temp_name = f"{TEMP_PREFIX}{os.getpid()}-{_mint_id()}"
    return os.path.join(directory, temp_name)


def _plan_backup_path(root, target_path):
    """Decides update's backup path before it exists, same reasoning as
    `_plan_temp_path`; create/attach have no backup (C6 step 5)."""
    backup_dir = os.path.join(root, ".brain", "state", "backups")
    return os.path.join(backup_dir, gitutil.hash_text(target_path) + ".bak")


def _write_temp(temp_path, content_bytes):
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content_bytes)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def _remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _failed_before_apply(temp_path):
    """F6 (Owner ruling): report `temp_path` only when a temp file genuinely
    remains after this failure's own cleanup -- a path to a file already
    deleted reports nothing true."""
    doc = {"outcome": "failed_before_apply"}
    if temp_path is not None and os.path.exists(temp_path):
        doc["temp_path"] = temp_path
    return doc, 3


def _normalize_dates(value):
    """Reduces a `datetime.date` (never `datetime.datetime`, which the
    canonical form never emits unquoted) to its ISO string, recursively,
    so a YAML 1.1 reparse of a date field compares equal to the plain
    string the caller supplied (F2)."""
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _normalize_dates(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_dates(item) for item in value]
    return value


def _allowed_zone(root, base_dir, zone):
    """F3: attach had no zone check at all, unlike create/update which at
    least route through a document type's declared `zones`. A write path
    that reaches `.brain/types/custom/` lets content edit the rules that
    validate content (C2 separates data zones from `.brain/`), so attach is
    confined to the same durable data zones or installed module folders any
    document type may declare (spec: "any durable data zone or module
    folder")."""
    if zone in DATA_ZONES:
        return True
    return zone in typedefs.detect_installed_modules(root)[0].values()


def run_persist(request, root, base_dir, yaml_module, duplicate_loader):
    """Runs one persist operation (create, update or attach). Returns
    (report_dict, exit_code) per C1/C6."""
    if not isinstance(request, dict):
        return _refused("invalid_request")

    operation = request.get("operation")
    path = request.get("path")
    if operation not in ("create", "update", "attach"):
        return _refused("invalid_request")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in path.split("/"):
        return _refused("invalid_request")

    if not journal_present(root):
        return _refused("journal_missing")

    if _has_open_intent(root, path):
        return _refused("open_intent")

    run_id = request.get("run_id") or _mint_id()
    zone = _zone_of(path)

    current_bytes = _read_current(root, path)

    if operation == "attach":
        if not _allowed_zone(root, base_dir, zone):
            return _refused("invalid")
        outcome = _prepare_attach(request, path, current_bytes)
    else:
        outcome = _prepare_document(request, operation, path, root, base_dir, zone, current_bytes, yaml_module, duplicate_loader)

    if outcome.get("refused") is not None:
        return _refused(outcome["refused"], outcome.get("observed_sha256"))

    intended_bytes = outcome["intended_bytes"]
    intended_sha256 = gitutil.hash_bytes(intended_bytes)
    expected_prior = outcome["expected_prior"]
    op_key = _op_key(run_id, path, intended_sha256)

    target_full = os.path.join(root, path)
    target_dir = os.path.dirname(target_full)

    # F4: both paths are decided now, before either file exists, so C6 step
    # 2's intent record can name them -- `recover` needs them to close an
    # intent and remove its temp without reconstructing either.
    temp_path = _plan_temp_path(target_dir)
    backup_path = _plan_backup_path(root, target_full) if operation == "update" else None

    intent_record = {
        "run_id": run_id,
        "op_key": op_key,
        "operation": operation,
        "path": path,
        "expected_prior": expected_prior,
        "intended_sha256": intended_sha256,
        "temp_path": temp_path,
        "backup_path": backup_path,
    }
    _write_intent(root, path, intent_record)
    _check_fault("after_intent")

    try:
        _write_temp(temp_path, intended_bytes)
    except OSError:
        _remove_if_exists(temp_path)
        _remove_intent(root, path)
        return _failed_before_apply(temp_path)

    with open(temp_path, "rb") as fh:
        written_hash = gitutil.hash_bytes(fh.read())
    if written_hash != intended_sha256:
        _remove_if_exists(temp_path)
        _remove_intent(root, path)
        return _failed_before_apply(temp_path)

    _check_fault("after_temp")

    # Step 4: re-check the target against expected_prior.
    observed_bytes = _read_current(root, path)
    observed_hash = gitutil.hash_bytes(observed_bytes) if observed_bytes is not None else "absent"
    if observed_hash != expected_prior:
        _remove_if_exists(temp_path)
        _remove_intent(root, path)
        reason = "exists" if operation in ("create", "attach") else "version_mismatch"
        refusal_doc = {"outcome": "refused", "reason": reason}
        if observed_bytes is not None:
            refusal_doc["observed_sha256"] = observed_hash
        return refusal_doc, 2

    _check_conflict_injection(root, path, target_full)
    _check_fault("before_apply")

    try:
        if operation in ("create", "attach"):
            _apply_create_or_attach(temp_path, target_full)
            # F1: nothing removed the temp after a successful hard link, so
            # every create/attach left a hidden `.brain-persist-tmp-*`
            # sibling behind -- a C6 `orphan_temp` in waiting, and a dirty
            # git brain after every write. `update` already consumes its
            # temp via `os.replace`.
            _remove_if_exists(temp_path)
        else:
            _apply_update(temp_path, target_full, backup_path)
    except OSError:
        _remove_if_exists(temp_path)
        _remove_intent(root, path)
        return _failed_before_apply(temp_path)

    _check_fault("after_apply")

    with open(target_full, "rb") as fh:
        applied_bytes = fh.read()
    applied_hash = gitutil.hash_bytes(applied_bytes)

    if applied_hash != intended_sha256:
        # target_unexpected: out of scope for this story's classification
        # (ST-03/recover owns it); persist itself never retries.
        doc = {
            "outcome": "target_unexpected",
            "observed_sha256": applied_hash,
            "backup_path": backup_path,
        }
        return doc, 3

    grant_id = None
    if operation == "create" and isinstance(request.get("frontmatter"), dict):
        if request["frontmatter"].get("authored_by") == "agent":
            grant_id = _mint_id()

    pending = []
    change_record_done = False
    commit_done = False
    try:
        _check_fault("before_change_record")
        record_change(root, intent_record, applied_hash)
        change_record_done = True

        _check_fault("before_commit")
        if is_git_brain(root):
            commit_write(root, intent_record, applied_hash, grant=grant_id)
        commit_done = True
    except _Fault:
        if not change_record_done:
            pending = ["change_record", "commit"]
        elif not commit_done:
            pending = ["commit"]

    if pending:
        return {
            "outcome": "written_incomplete",
            "id": outcome.get("id"),
            "path": path,
            "version": applied_hash,
            "pending": pending,
        }, 3

    _remove_intent(root, path)

    result = {
        "outcome": "written",
        "id": outcome.get("id"),
        "path": path,
        "version": applied_hash,
    }
    return result, 0


def _prepare_attach(request, path, current_bytes):
    import base64

    content_b64 = request.get("content_base64")
    if not isinstance(content_b64, str):
        return {"refused": "invalid_request"}
    try:
        content = base64.b64decode(content_b64, validate=True)
    except Exception:
        return {"refused": "invalid_request"}
    expected_prior = "absent"
    return {
        "intended_bytes": content,
        "expected_prior": expected_prior,
        "id": None,
    }


def _prepare_document(request, operation, path, root, base_dir, zone, current_bytes, yaml_module, duplicate_loader):
    if operation == "create":
        return _prepare_create(request, path, root, base_dir, zone, current_bytes)
    return _prepare_update(request, path, root, base_dir, zone, current_bytes, yaml_module, duplicate_loader)


def _prepare_create(request, path, root, base_dir, zone, current_bytes):
    frontmatter_fields = request.get("frontmatter")
    if not isinstance(frontmatter_fields, dict):
        return {"refused": "invalid_request"}
    body = request.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        return {"refused": "invalid_request"}

    mapping = dict(frontmatter_fields)
    mapping["kb"] = 1
    # A7: persist mints the id itself on every create -- C6's request shape
    # carries no `id` field, and C4 requires an id minted once, never derived
    # from the path or accepted from a caller. Any `id` a caller supplied in
    # `frontmatter` is discarded, not merely defaulted.
    mapping["id"] = _mint_id()

    if mapping.get("origin") == "unknown":
        return {"refused": "review_required"}

    errors = _mapping_errors(mapping, root, base_dir, zone)
    if errors:
        return {"refused": "invalid"}

    block_text = frontmatter.render_canonical_block(mapping)
    content = "---\n" + block_text + "---\n" + body
    intended_bytes = content.encode("utf-8")

    return {
        "intended_bytes": intended_bytes,
        "expected_prior": "absent",
        "id": mapping["id"],
    }


def _prepare_update(request, path, root, base_dir, zone, current_bytes, yaml_module, duplicate_loader):
    expected_version = request.get("expected_version")
    if not isinstance(expected_version, str) or not expected_version:
        return {"refused": "invalid_request"}

    if current_bytes is None:
        return {"refused": "version_mismatch", "expected_prior": expected_version}

    status, fm, features = _classify_existing(current_bytes, yaml_module)
    if status == "unsupported_version":
        return {"refused": "unsupported_version"}
    if status in ("malformed",):
        return {"refused": "invalid"}
    if status == "unmanaged":
        return {"refused": "invalid"}
    if status == "unsupported_for_editing":
        return {"refused": "unsupported_frontmatter"}

    frontmatter_updates = request.get("frontmatter")
    if request.get("replace_frontmatter"):
        # A reviewed full-frontmatter replacement always counts as an
        # overwrite (C14) and needs review, which this story cannot supply
        # (A4): no request field marks a write reviewed.
        return {"refused": "review_required"}
    if not isinstance(frontmatter_updates, dict) or not frontmatter_updates:
        return {"refused": "invalid_request"}

    text = current_bytes.decode("utf-8")
    current_mapping = dict(next(iter(yaml_module.load_all(fm.block_text, Loader=duplicate_loader)), {}) or {})

    new_block_text = fm.block_text
    intended_mapping = dict(current_mapping)
    for field, value in frontmatter_updates.items():
        intended_mapping[field] = value
        if value == "unknown" and field == "origin":
            return {"refused": "review_required"}
        try:
            new_block_text = frontmatter.replace_property(new_block_text, yaml_module, field, value)
        except frontmatter.UnsupportedFrontmatter:
            return {"refused": "unsupported_frontmatter"}

    # Guard 1: every byte outside the replaced span(s) identical to the
    # original, verified by comparing everything except the touched fields'
    # own lines -- checked implicitly by construction (replace_property only
    # ever rewrites the named field's own span) plus guard 2 below, which
    # would fail if unrelated content had drifted.

    # Guard 2: a re-parse of the new block equals the intended mapping. A
    # date field (F2) is the one documented reader difference (C4): the
    # YAML 1.1 loader used here reads an unquoted `YYYY-MM-DD` as a `date`
    # object, while the caller's intended value is the plain string it
    # supplied -- normalize both sides to comparable forms rather than
    # comparing a `date` to a string that can never equal it.
    try:
        reparsed_docs = list(yaml_module.load_all(new_block_text, Loader=duplicate_loader))
    except yaml_module.YAMLError:
        return {"refused": "unsupported_frontmatter"}
    reparsed = reparsed_docs[0] if reparsed_docs else {}
    if _normalize_dates(reparsed) != _normalize_dates(intended_mapping):
        return {"refused": "unsupported_frontmatter"}

    errors = _mapping_errors(intended_mapping, root, base_dir, zone)
    if errors:
        return {"refused": "invalid"}

    new_text = "---\n" + new_block_text + "---\n" + fm.body_text
    intended_bytes = new_text.encode("utf-8")

    # agent-brain #38/#42: a no-op update -- intended bytes equal to the
    # target's current bytes -- is refused here, at the point the intended
    # bytes are known and before `run_persist` ever writes an intent (C6 step
    # 2), so it can never leave a permanently locked path. Precedence is
    # pinned: expected_version is checked before no_change, so a stale no-op
    # reads the same as a stale non-no-op (both refused from step 4 today).
    current_sha256 = gitutil.hash_bytes(current_bytes)
    if gitutil.hash_bytes(intended_bytes) == current_sha256:
        if expected_version != current_sha256:
            return {"refused": "version_mismatch", "observed_sha256": current_sha256}
        return {"refused": "no_change", "observed_sha256": current_sha256}

    return {
        "intended_bytes": intended_bytes,
        "expected_prior": expected_version,
        "id": intended_mapping.get("id"),
    }
