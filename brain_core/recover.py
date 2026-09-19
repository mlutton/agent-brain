"""Implements `brain recover` (see docs/specification/beta.md, C6 and the
"Recover report" envelope).

Seam: `run_recover` is the whole recover interface. bin/brain hands it the
brain root and the parsed options and gets back exactly one report dict plus an
exit code (C1). Hidden behind it: listing the open intents, comparing each
intent's target with the hashes it recorded, closing or finishing each one,
finding temp files no intent owns, and folding the per-intent results into one
aggregate outcome. No caller sequences any of that.

Design note: recovery reuses `brain_core/persist.py`'s public interface -- the
intent journal, the temp-file naming, the change record and the commit -- rather
than reading the journal a second way. Persist already owns those shapes, and
the intent record carries `temp_path` and `backup_path` precisely so recovery
does not reconstruct them. A second reading of the journal would be a second
copy of the record shape, the intent path scheme and the trailer format, and
"exactly one record and one commit" would depend on the two copies staying
identical.
"""

import os
import stat

from . import gitutil, persist, typedefs


class _NotARegularFile(Exception):
    """The target is not a regular file inside the brain."""


def _target_hash(root, path):
    """The sha256 of the regular file at `path` under `root`, or `absent`.
    Nothing else is hashed: a link, a folder, a device or a path that leaves
    the brain (through `..`, an absolute path or a linked folder) raises
    `_NotARegularFile`, because reading through one would hash bytes recovery
    does not own and a commit would then assert that hash as the brain's."""
    root_real = os.path.realpath(root)
    full = os.path.normpath(os.path.join(root_real, path))
    if os.path.commonpath([root_real, full]) != root_real:
        raise _NotARegularFile(path)
    folder = os.path.dirname(full)
    if os.path.realpath(folder) != folder:
        raise _NotARegularFile(path)
    try:
        mode = os.lstat(full).st_mode
    except FileNotFoundError:
        return "absent"
    if not stat.S_ISREG(mode):
        raise _NotARegularFile(path)
    # Opened without following a link and checked again on the descriptor, so
    # a swap between the check above and this read is still refused.
    fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _NotARegularFile(path)
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return gitutil.hash_bytes(fh.read())
    finally:
        os.close(fd)


def _canonical(path):
    """`path` with every folder on the way resolved and its own name left
    alone, so a link named by the path is judged as a link and never as
    whatever it points at. Recovery checks this form and acts on this form."""
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _remove_temp(temp):
    try:
        os.remove(temp)
    except FileNotFoundError:
        return False
    return True


def _finish_bookkeeping(root, intent, applied_hash, entry):
    """C6 step 7 for an applied write, each step at most once per `op_key`: a
    change record or commit that already exists is not made again. A step that
    fails leaves it and every later step `pending`, so the intent stays open
    for a later recovery."""
    entry["finished"] = []
    entry["pending"] = []
    steps = [("change_record", persist.change_record_exists, persist.record_change)]
    if persist.is_git_brain(root):
        steps.append(("commit", persist.commit_exists, persist.commit_write))
    for index, (name, exists, make) in enumerate(steps):
        try:
            if not exists(root, intent["op_key"]):
                make(root, intent, applied_hash)
                entry["finished"].append(name)
        except Exception:
            entry["pending"] = [step[0] for step in steps[index:]]
            return


def _own_temp(root, intent):
    """The path of the temp file an intent names when it is one persist could
    have made for it, else None: a regular file (or nothing) named as persist
    names its temp files, inside this brain. The intent is a file on disk, so
    recovery checks before it removes anything a field points at. The path
    returned is the one that was checked, and the only one recovery removes."""
    try:
        temp = _canonical(intent["temp_path"])
    except ValueError:
        return None  # a path no filesystem accepts names nothing
    root_real = os.path.realpath(root)
    if not persist.is_temp_name(os.path.basename(temp)):
        return None
    if os.path.commonpath([root_real, os.path.dirname(temp)]) != root_real:
        return None
    try:
        mode = os.lstat(temp).st_mode
    except FileNotFoundError:
        return temp
    return temp if stat.S_ISREG(mode) else None


def _unresolved(file_name, intent, reason):
    """The `target_unexpected` entry for an intent recovery could not act on.
    Every field the intent does not carry is `null`, and `observed_sha256` is
    `null` because no hash was established."""
    fields = intent or {}
    return {
        "intent_file": file_name,
        "path": fields.get("path"),
        "op_key": fields.get("op_key"),
        "run_id": fields.get("run_id"),
        "operation": fields.get("operation"),
        "classification": "target_unexpected",
        "reason": reason,
        "expected_prior": fields.get("expected_prior"),
        "intended_sha256": fields.get("intended_sha256"),
        "observed_sha256": None,
        "backup_path": fields.get("backup_path"),
    }


def _recover_guarded(root, file_name, intent):
    """`_recover_one` for a single intent, so that whatever goes wrong with it
    is reported as that intent's and never as the command's. Whatever the
    intent had already finished stays finished and is not repeated by a later
    recovery; the intent itself stays open."""
    try:
        return _recover_one(root, file_name, intent)
    except Exception:
        return _unresolved(file_name, intent, "recovery_failed")


def _recover_one(root, file_name, intent):
    temp = _own_temp(root, intent) if intent is not None else None
    if temp is None:
        # Nothing here can be trusted enough to act on, and nothing may vanish
        # from the report: the intent stays open as unresolved.
        return _unresolved(file_name, intent, "invalid_intent")
    entry = {
        "intent_file": file_name,
        "path": intent["path"],
        "op_key": intent["op_key"],
        "run_id": intent["run_id"],
        "operation": intent["operation"],
    }
    try:
        observed = _target_hash(root, intent["path"])
    except _NotARegularFile:
        # No hash was established, so nothing may be asserted on its behalf:
        # no record, no commit, and the intent stays open.
        return _unresolved(file_name, intent, "not_a_regular_file")
    # `intended_sha256` is tested first (C6). An update that changes nothing has
    # the same hash for both, and a target holding the intended bytes is
    # `applied` whether or not the replace ran; `not_applied` would discard the
    # record and commit an applied write is owed.
    if observed == intent["intended_sha256"]:
        entry["classification"] = "applied"
        _finish_bookkeeping(root, intent, observed, entry)
        if not entry["pending"]:
            persist.close_intent(root, intent["path"])
    elif observed == intent["expected_prior"]:
        entry["classification"] = "not_applied"
        entry["temp_removed"] = _remove_temp(temp)
        persist.close_intent(root, intent["path"])
    else:
        # The tool cannot say what happened here, so it closes nothing: the
        # intent stays open, and Behaviour 21 keeps the path locked until a
        # person acts.
        entry["classification"] = "target_unexpected"
        entry["reason"] = None
        entry["expected_prior"] = intent["expected_prior"]
        entry["intended_sha256"] = intent["intended_sha256"]
        entry["observed_sha256"] = observed
        entry["backup_path"] = intent["backup_path"]
    return entry


def _orphan_temps(root, owned):
    """Root-relative paths of temp files no open intent owns, sorted. Persist
    writes a temp beside its target, so every data zone and installed module
    folder is walked; symbolic links are not followed."""
    folders = list(persist.DATA_ZONES) + sorted(typedefs.detect_installed_modules(root)[0].values())
    found = []
    for folder in folders:
        for directory, _dirs, files in os.walk(os.path.join(root, folder), followlinks=False):
            for name in files:
                full = os.path.join(directory, name)
                if persist.is_temp_name(name) and _canonical(full) not in owned:
                    found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def run_recover(root, options):
    """Recovers every open intent under `root`. Returns (report, exit code)."""
    if "--restore-retained" in options or "--start-journal" in options:
        return {"outcome": "refused", "reason": "not_implemented"}, 2
    if not persist.journal_present(root):
        return {"outcome": "refused", "reason": "journal_missing"}, 2

    intents = persist.open_intents(root)
    owned = set()
    for _name, intent in intents:
        if intent is None:
            continue
        try:
            owned.add(_canonical(intent["temp_path"]))
        except ValueError:
            pass  # an intent whose temp path names nothing owns nothing
    entries = [_recover_guarded(root, name, intent) for name, intent in intents]
    entries.sort(key=lambda entry: (entry["path"] or "", entry["intent_file"]))
    if any(e["classification"] == "target_unexpected" for e in entries):
        outcome = "target_unexpected"
    elif any(e.get("pending") for e in entries):
        outcome = "recovery_incomplete"
    else:
        outcome = "recovered"
    code = 0 if outcome == "recovered" else 3
    return {"outcome": outcome, "intents": entries, "orphan_temps": _orphan_temps(root, owned)}, code
