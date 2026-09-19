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

from . import gitutil, persist, typedefs


def _target_hash(root, path):
    full = os.path.join(root, path)
    if not os.path.isfile(full):
        return "absent"
    with open(full, "rb") as fh:
        return gitutil.hash_bytes(fh.read())


def _remove_temp(intent):
    try:
        os.remove(intent["temp_path"])
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


def _owns_its_temp(root, intent):
    """True when the temp file an intent names is one persist could have made
    for it: inside this brain and named as persist names its temp files. The
    intent is a file on disk, so recovery checks before it removes anything a
    field points at."""
    temp = os.path.realpath(intent["temp_path"])
    root_real = os.path.realpath(root)
    return persist.is_temp_name(os.path.basename(temp)) and os.path.commonpath([root_real, temp]) == root_real


def _recover_one(root, file_name, intent):
    if intent is None or not _owns_its_temp(root, intent):
        # Nothing here can be trusted enough to act on, and nothing may vanish
        # from the report: the intent stays open as unresolved.
        fields = intent or {}
        return {
            "intent_file": file_name,
            "path": fields.get("path"),
            "op_key": fields.get("op_key"),
            "run_id": fields.get("run_id"),
            "operation": fields.get("operation"),
            "classification": "target_unexpected",
            "reason": "invalid_intent",
            "expected_prior": fields.get("expected_prior"),
            "intended_sha256": fields.get("intended_sha256"),
            "observed_sha256": None,
            "backup_path": fields.get("backup_path"),
        }
    entry = {
        "intent_file": file_name,
        "path": intent["path"],
        "op_key": intent["op_key"],
        "run_id": intent["run_id"],
        "operation": intent["operation"],
    }
    observed = _target_hash(root, intent["path"])
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
        entry["temp_removed"] = _remove_temp(intent)
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
                if persist.is_temp_name(name) and os.path.realpath(full) not in owned:
                    found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(found)


def run_recover(root, options):
    """Recovers every open intent under `root`. Returns (report, exit code)."""
    if "--restore-retained" in options or "--start-journal" in options:
        return {"outcome": "refused", "reason": "not_implemented"}, 2
    if not persist.journal_present(root):
        return {"outcome": "refused", "reason": "journal_missing"}, 2

    intents = persist.open_intents(root)
    owned = {os.path.realpath(intent["temp_path"]) for _name, intent in intents if intent is not None}
    entries = [_recover_one(root, name, intent) for name, intent in intents]
    entries.sort(key=lambda entry: (entry["path"] or "", entry["intent_file"]))
    if any(e["classification"] == "target_unexpected" for e in entries):
        outcome = "target_unexpected"
    elif any(e.get("pending") for e in entries):
        outcome = "recovery_incomplete"
    else:
        outcome = "recovered"
    code = 0 if outcome == "recovered" else 3
    return {"outcome": outcome, "intents": entries, "orphan_temps": _orphan_temps(root, owned)}, code
