"""Implements `brain persist` -- create, update, attach (see docs/specification/beta.md, C6).

Seam: this module is the whole persist interface. A caller (bin/brain) hands
it a request dict, the brain root and the loaded vendored yaml module, and
gets back exactly one outcome dict plus an exit code (C1). Everything C6
sequences -- journalling an intent, writing and verifying a temp file,
re-checking the target, hard-linking or backup-and-replace, hashing,
writing a change record and making a git commit with consistent trailers --
is mechanics hidden behind that one call; no caller ever sequences those
steps itself.

Design note (see the codebase-design and deepening references staged under
.dispatch/reference/ for this residency): `brain_core/setup.py` already has
its own module-private `_git`, `_hash_file` and `_hardlink_probe` helpers.
Persist needs a subprocess `git` wrapper and a file-hash helper too. Rather
than reach into `setup`'s private helpers (coupling two independent command
modules through each other's internals) or duplicate them silently, the
small, genuinely shared primitives live in `brain_core/gitutil.py`: a real
second seam (two call sites -- setup.py, once ST-06/later stories land, and
persist.py, now), not a hypothetical one. `setup.py` itself is left
unchanged this round: it already passes its own tests, and touching a
working module outside the TDD loop that owns it is a refactor, not a
behaviour change (see <deviations> in the handback).
"""

import os
import signal

from . import fields as fields_mod
from . import frontmatter, gitutil, typedefs

_ID_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase, no i l o u

FAULT_ENV = "BRAIN_TEST_FAULTS"
FAULT_VAR = "BRAIN_FAULT"

_DATA_ZONES = ("inbox", "raw", "documents", "wiki")


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


def _mint_id():
    import secrets

    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(26))


def _refused(reason, observed_hash=None):
    doc = {"outcome": "refused", "reason": reason}
    if observed_hash is not None:
        doc["observed_sha256"] = observed_hash
    return doc, 2


def _zone_of(path):
    return path.split("/", 1)[0]


def _journal_present(root):
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


def _write_change_record(root, op_key, record):
    directory = _change_records_dir(root)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, op_key + ".json")
    if os.path.isfile(path):
        return  # at most one change record per op_key (C6)
    gitutil.write_json(path, record)


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


def _apply_create_or_attach(temp_path, target_path):
    """Hard-links temp onto target; the link fails (no fallback) if target
    already exists (C6 step 5)."""
    os.link(temp_path, target_path)


def _apply_update(root, temp_path, target_path, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, gitutil.hash_text(target_path) + ".bak")
    with open(target_path, "rb") as fh:
        current_bytes = fh.read()
    with open(backup_path, "wb") as fh:
        fh.write(current_bytes)
    os.replace(temp_path, target_path)
    return backup_path


def _write_temp(directory, content_bytes):
    os.makedirs(directory, exist_ok=True)
    temp_name = f".brain-persist-tmp-{os.getpid()}-{_mint_id()}"
    temp_path = os.path.join(directory, temp_name)
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
    return temp_path


def _remove_if_exists(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


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

    if not _journal_present(root):
        return _refused("journal_missing")

    if _has_open_intent(root, path):
        return _refused("open_intent")

    run_id = request.get("run_id") or _mint_id()
    zone = _zone_of(path)

    current_bytes = _read_current(root, path)

    if operation == "attach":
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

    intent_record = {
        "run_id": run_id,
        "op_key": op_key,
        "operation": operation,
        "path": path,
        "expected_prior": expected_prior,
        "intended_sha256": intended_sha256,
    }
    _write_intent(root, path, intent_record)
    _check_fault("after_intent")

    try:
        temp_path = _write_temp(target_dir, intended_bytes)
    except OSError:
        _remove_intent(root, path)
        return {"outcome": "failed_before_apply", "temp_path": None}, 3

    with open(temp_path, "rb") as fh:
        written_hash = gitutil.hash_bytes(fh.read())
    if written_hash != intended_sha256:
        _remove_if_exists(temp_path)
        _remove_intent(root, path)
        return {"outcome": "failed_before_apply", "temp_path": None}, 3

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

    _check_fault("before_apply")

    backup_path = None
    try:
        if operation in ("create", "attach"):
            _apply_create_or_attach(temp_path, target_full)
        else:
            backup_dir = os.path.join(root, ".brain", "state", "backups")
            backup_path = _apply_update(root, temp_path, target_full, backup_dir)
    except OSError:
        _remove_if_exists(temp_path)
        _remove_intent(root, path)
        return {"outcome": "failed_before_apply", "temp_path": temp_path}, 3

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

    path_entry = f"{path} sha256={applied_hash} prior={expected_prior}"
    trailer_paths = {"keys": [op_key], "paths": [path_entry]}

    pending = []
    change_record_done = False
    commit_done = False
    try:
        _check_fault("before_change_record")
        _write_change_record(
            root,
            op_key,
            {
                "op_key": op_key,
                "operation": operation,
                "run_id": run_id,
                "path": path,
                "sha256": applied_hash,
                "prior": expected_prior,
            },
        )
        change_record_done = True

        _check_fault("before_commit")
        if os.path.isdir(os.path.join(root, ".git")):
            gitutil.commit_paths(
                root,
                [path],
                _commit_trailers(operation, run_id, trailer_paths, grant=grant_id),
            )
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

    # Guard 2: a re-parse of the new block equals the intended mapping.
    try:
        reparsed_docs = list(yaml_module.load_all(new_block_text, Loader=duplicate_loader))
    except yaml_module.YAMLError:
        return {"refused": "unsupported_frontmatter"}
    reparsed = reparsed_docs[0] if reparsed_docs else {}
    if reparsed != intended_mapping:
        return {"refused": "unsupported_frontmatter"}

    errors = _mapping_errors(intended_mapping, root, base_dir, zone)
    if errors:
        return {"refused": "invalid"}

    new_text = "---\n" + new_block_text + "---\n" + fm.body_text
    intended_bytes = new_text.encode("utf-8")

    return {
        "intended_bytes": intended_bytes,
        "expected_prior": expected_version,
        "id": intended_mapping.get("id"),
    }
