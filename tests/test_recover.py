"""Tests for `brain recover` (ST-03), against docs/specification/beta.md C6 (the
`recover` paragraph and the fault-injection contract), C7 and the report
envelope recorded under "Recover report".

Every scenario runs `bin/brain` as a subprocess against a synthetic brain in a
fresh temporary directory outside the repository. Interruptions are produced
only through the specification's own `BRAIN_TEST_FAULTS` / `BRAIN_FAULT`
variables on a real `persist` run. Before any claim about what recovery did,
a test shows that the injection landed: the process died by SIGKILL (or
reported `written_incomplete`) and the on-disk state is what that interrupted
write leaves. Expected hashes come from `hashlib` over bytes the test read, and
expected op keys from the intent file the interrupted write left, never from
recomputing the code's own formula. One exception:
`AppliedTests.test_update_that_leaves_its_bytes_unchanged_and_was_killed_after_apply_is_applied`
builds its interrupted state by hand, since persist can no longer produce an
equal-hash intent for `recover` to classify (agent-brain #38/#42).
"""

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest

from tests import support
from tests.test_persist import NOTE_FRONTMATTER, build_brain

_TIMEOUT = 120


def _sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _clean_env():
    """The environment for a `recover` run: no fault variables leak in."""
    env = dict(os.environ)
    env.pop("BRAIN_TEST_FAULTS", None)
    env.pop("BRAIN_FAULT", None)
    return env


class RecoverTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="brain-recover-")
        # Registered before anything is built, so a failing test or a killed
        # child cannot leave the fixture behind.
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def fresh_brain(self, git=False):
        build_brain(self.root, git=git)
        return self.root

    # -- driving the interrupted write -------------------------------------

    def persist(self, request, fault=None):
        env = dict(os.environ)
        if fault is not None:
            env.update(BRAIN_TEST_FAULTS="1", BRAIN_FAULT=fault)
        proc = support.run_brain(
            ["persist", "--root", self.root],
            input_data=json.dumps(request),
            env=env,
            timeout=_TIMEOUT,
        )
        return proc

    def persist_killed(self, request, step):
        """Runs persist with `kill` at `step` and proves the kill landed: the
        child died by SIGKILL and printed nothing."""
        proc = self.persist(request, fault=f"{step}:kill")
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc)
        self.assertEqual(proc.stdout, "")
        return proc

    def create_request(self, path, **extra):
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        request.update(extra)
        return request

    def created(self, path):
        proc = self.persist(self.create_request(path))
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "written", doc)
        return doc

    # -- observing the brain -----------------------------------------------

    def recover(self, *extra):
        proc = support.run_brain(
            ["recover", "--root", self.root, *extra], env=_clean_env(), timeout=_TIMEOUT
        )
        return proc, support.parse_single_json(proc.stdout)

    def tearDown_root(self):
        """Empties the fixture between sub-cases of one test."""
        shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root)

    def created_in_fresh_brain(self, path, git=False):
        self.fresh_brain(git=git)
        return self.created(path)

    def full(self, path):
        return os.path.join(self.root, path)

    def intent_files(self):
        directory = os.path.join(self.root, ".brain", "journal", "intents")
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def only_intent(self):
        files = self.intent_files()
        self.assertEqual(len(files), 1, files)
        with open(os.path.join(self.root, ".brain", "journal", "intents", files[0]), encoding="utf-8") as fh:
            return json.load(fh)

    def change_records_for(self, op_key):
        directory = os.path.join(self.root, ".brain", "journal", "change_records")
        if not os.path.isdir(directory):
            return []
        found = []
        for name in sorted(os.listdir(directory)):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                record = json.load(fh)
            if record.get("op_key") == op_key:
                found.append(record)
        return found

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        ).stdout

    def commits_for(self, op_key):
        out = self.git("log", "-F", "--grep", f"Brain-Key: {op_key}", "--format=%H")
        return out.split()

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def listing(self, directory):
        return sorted(os.listdir(self.full(directory)))


class RouteTests(RecoverTestCase):
    """`recover` is a wired subcommand that prints one JSON object."""

    def test_brain_with_no_open_intent_recovers_nothing(self):
        """C6/Recover report: zero open intents is `recovered`, exit 0."""
        self.fresh_brain()
        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(doc, {"outcome": "recovered", "intents": [], "orphan_temps": []})

    def test_brain_without_a_journal_is_refused_journal_missing(self):
        """C6: any operation refuses `journal_missing` while the journal is
        not present -- reporting `recovered` would hide a lost journal."""
        self.fresh_brain()
        os.remove(os.path.join(self.root, ".brain", "journal", "epoch.json"))
        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 2, proc)
        self.assertEqual(doc, {"outcome": "refused", "reason": "journal_missing"})


class NotAppliedTests(RecoverTestCase):
    """`kill` at `after_intent` and at `before_apply` is `not_applied` --
    the intent is closed, the temp removed and the target unchanged."""

    def test_create_killed_before_the_target_exists_is_not_applied(self):
        for step, temp_expected in (("after_intent", False), ("before_apply", True)):
            with self.subTest(step=step):
                self.tearDown_root()
                self.fresh_brain()
                path = "documents/killed/killed.md"
                self.persist_killed(self.create_request(path), step)

                # The injection landed, and the disk is what an interrupted
                # create leaves: an open intent, no target, and a temp file
                # beside where the target would be only once the temp was written.
                intent = self.only_intent()
                self.assertEqual(intent["path"], path)
                self.assertEqual(intent["expected_prior"], "absent")
                self.assertFalse(os.path.exists(self.full(path)))
                leftover = self.listing("documents/killed") if os.path.isdir(self.full("documents/killed")) else []
                self.assertEqual(len(leftover), 1 if temp_expected else 0, leftover)

                proc, doc = self.recover()
                self.assertEqual(proc.returncode, 0, doc)
                self.assertEqual(doc["outcome"], "recovered")
                self.assertEqual([(e["path"], e["classification"]) for e in doc["intents"]], [(path, "not_applied")])
                self.assertEqual(doc["orphan_temps"], [])

                self.assertEqual(self.intent_files(), [])
                self.assertFalse(os.path.exists(self.full(path)))
                remaining = self.listing("documents/killed") if os.path.isdir(self.full("documents/killed")) else []
                self.assertEqual(remaining, [])

    def test_update_killed_before_apply_leaves_the_target_at_its_prior_bytes(self):
        path = "documents/upd/upd.md"
        created = self.created_in_fresh_brain(path)
        prior_hash = created["version"]
        request = {
            "operation": "update",
            "path": path,
            "expected_version": prior_hash,
            "frontmatter": {"status": "complete"},
        }
        self.persist_killed(request, "before_apply")

        intent = self.only_intent()
        self.assertEqual(intent["expected_prior"], prior_hash)
        self.assertEqual(_sha256_file(self.full(path)), prior_hash)
        self.assertEqual(len(self.listing("documents/upd")), 2)  # the target and the temp

        proc, doc = self.recover()
        self.assertEqual([(e["path"], e["classification"]) for e in doc["intents"]], [(path, "not_applied")])
        self.assertEqual(self.intent_files(), [])
        self.assertEqual(_sha256_file(self.full(path)), prior_hash)
        self.assertEqual(self.listing("documents/upd"), ["upd.md"])


class AppliedTests(RecoverTestCase):
    """`kill` at `after_apply` is `applied`, and after recovery exactly
    one change record and one commit exist for that `op_key`."""

    def test_create_killed_after_apply_is_applied_with_one_record_and_one_commit(self):
        self.fresh_brain(git=True)
        path = "documents/applied/applied.md"
        self.persist_killed(self.create_request(path), "after_apply")

        # The injection landed after the apply and before any bookkeeping: the
        # target holds the intent's own intended bytes, the intent is open, and
        # neither a change record nor a commit exists for its op key.
        intent = self.only_intent()
        self.assertEqual(_sha256_file(self.full(path)), intent["intended_sha256"])
        op_key = intent["op_key"]
        self.assertEqual(self.change_records_for(op_key), [])
        self.assertEqual(self.commits_for(op_key), [])
        commits_before = self.git("rev-list", "--count", "HEAD").strip()

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 0, doc)
        self.assertEqual(doc["outcome"], "recovered")
        self.assertEqual([(e["path"], e["classification"]) for e in doc["intents"]], [(path, "applied")])
        self.assertEqual(doc["intents"][0]["finished"], ["change_record", "commit"])
        self.assertEqual(doc["intents"][0]["pending"], [])

        self.assertEqual(len(self.change_records_for(op_key)), 1)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), str(int(commits_before) + 1))
        self.assertEqual(self.intent_files(), [])
        self.assertEqual(_sha256_file(self.full(path)), intent["intended_sha256"])

        # The commit is the brain's own: it declares exactly this write (C7).
        message = self.git("log", "-1", "--format=%B")
        self.assertIn("Brain-Op: create", message.splitlines())
        self.assertIn(f"Brain-Key: {op_key}", message.splitlines())
        self.assertIn(
            f"Brain-Path: {path} sha256={intent['intended_sha256']} prior=absent", message.splitlines()
        )

    def test_update_killed_after_apply_is_applied_with_one_record_and_one_commit(self):
        path = "documents/appliedupd/appliedupd.md"
        created = self.created_in_fresh_brain(path, git=True)
        prior_hash = created["version"]
        request = {
            "operation": "update",
            "path": path,
            "expected_version": prior_hash,
            "frontmatter": {"status": "complete"},
        }
        self.persist_killed(request, "after_apply")

        intent = self.only_intent()
        self.assertEqual(intent["expected_prior"], prior_hash)
        self.assertNotEqual(intent["intended_sha256"], prior_hash)
        self.assertEqual(_sha256_file(self.full(path)), intent["intended_sha256"])
        op_key = intent["op_key"]
        self.assertEqual(self.change_records_for(op_key), [])
        self.assertEqual(self.commits_for(op_key), [])

        proc, doc = self.recover()
        self.assertEqual([(e["path"], e["classification"]) for e in doc["intents"]], [(path, "applied")])
        self.assertEqual(len(self.change_records_for(op_key)), 1)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])

    def test_recovered_commit_carries_no_brain_grant(self):
        """C6/C7: the grant id was minted by the interrupted process and is not in
        the intent, so recovery cannot honestly name one. The commit declares its
        op, key and path and nothing about a grant."""
        self.fresh_brain(git=True)
        path = "documents/nogrant/nogrant.md"
        self.persist_killed(self.create_request(path), "after_apply")
        op_key = self.only_intent()["op_key"]

        self.recover()

        (commit,) = self.commits_for(op_key)
        trailers = self.git("log", "-1", "--format=%B", commit).splitlines()
        self.assertIn(f"Brain-Key: {op_key}", trailers)  # the right commit was read
        self.assertEqual([line for line in trailers if line.startswith("Brain-Grant")], [])

    def test_update_that_leaves_its_bytes_unchanged_and_was_killed_after_apply_is_applied(self):
        """C6 classification order: a target holding `intended_sha256` is
        `applied` first. An update that would leave a target's bytes
        unchanged -- `expected_prior` and `intended_sha256` the same hash --
        can no longer be produced through `persist` (agent-brain #38/#42): a
        no-op update is now refused before any intent exists. This
        state is therefore built by hand: a real `create` gets the target
        committed in `HEAD` (as the earlier create used to leave it), then the
        intent, its backup and its consumed temp are written directly, exactly
        as the killed persist used to leave them, at the file name
        `persist._intent_path` gives that path."""
        path = "documents/samebytes/samebytes.md"
        created = self.created_in_fresh_brain(path, git=True)
        prior_hash = created["version"]
        with open(self.full(path), "rb") as fh:
            target_bytes = fh.read()
        self.assertEqual(hashlib.sha256(target_bytes).hexdigest(), prior_hash)

        # A hand-built backup (the update's apply step always writes one) and
        # a hand-built, already-consumed temp path (its recorded name carries
        # persist's temp prefix and lies in the target's own folder, but the
        # file itself does not exist -- the replace already consumed it).
        backup_path = os.path.join(self.root, ".brain", "state", "backups", "handcrafted.bak")
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        with open(backup_path, "wb") as fh:
            fh.write(target_bytes)
        temp_path = os.path.join(
            self.root, "documents", "samebytes", ".brain-persist-tmp-999-handcraftedtemp01"
        )
        self.assertFalse(os.path.exists(temp_path))

        op_key = "handcrafted-op-key-0001"
        intent = {
            "run_id": "handcrafted-run-0001",
            "op_key": op_key,
            "operation": "update",
            "path": path,
            "expected_prior": prior_hash,
            "intended_sha256": prior_hash,
            "temp_path": temp_path,
            "backup_path": backup_path,
        }
        intents_dir = os.path.join(self.root, ".brain", "journal", "intents")
        os.makedirs(intents_dir, exist_ok=True)
        intent_file = path.replace("/", "__") + ".json"
        with open(os.path.join(intents_dir, intent_file), "w", encoding="utf-8") as fh:
            json.dump(intent, fh)

        # The hand-built state is really there: prior and intended are one
        # hash, the target holds it, a backup exists and the temp does not.
        self.assertEqual(_sha256_file(self.full(path)), prior_hash)
        self.assertTrue(os.path.isfile(backup_path))
        self.assertFalse(os.path.exists(temp_path))
        self.assertEqual(self.change_records_for(op_key), [])
        self.assertEqual(self.commits_for(op_key), [])

        proc, doc = self.recover()
        (entry,) = doc["intents"]
        self.assertEqual(entry["classification"], "applied", entry)
        self.assertEqual(entry["finished"][:1], ["change_record"])
        self.assertEqual(len(self.change_records_for(op_key)), 1)

        # Git refuses a commit that changes no bytes, so the commit step may be
        # `pending` here. What must hold is that the report says what happened:
        # `recovered` and a closed intent exactly when nothing is pending, and a
        # commit for the op key exactly when the report says it was made.
        done = not entry["pending"]
        self.assertEqual((doc["outcome"], proc.returncode), ("recovered", 0) if done else ("recovery_incomplete", 3))
        self.assertEqual(self.intent_files() == [], done)
        self.assertEqual(len(self.commits_for(op_key)), 1 if "commit" in entry["finished"] else 0)

    def test_applied_write_in_a_brain_without_git_gets_its_change_record_only(self):
        self.fresh_brain(git=False)
        path = "documents/nogit/nogit.md"
        self.persist_killed(self.create_request(path), "after_apply")
        intent = self.only_intent()

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 0, doc)
        self.assertEqual([(e["path"], e["classification"]) for e in doc["intents"]], [(path, "applied")])
        self.assertEqual(len(self.change_records_for(intent["op_key"])), 1)
        self.assertFalse(os.path.exists(self.full(".git")))
        self.assertEqual(self.intent_files(), [])


class BookkeepingFailureTests(RecoverTestCase):
    """`fail` at `before_change_record` or at `before_commit`, followed by
    recovery, yields exactly one change record and one commit."""

    def _failed_write(self, step, path):
        self.fresh_brain(git=True)
        proc = self.persist(self.create_request(path), fault=f"{step}:fail")
        doc = support.parse_single_json(proc.stdout)
        # The injection landed: persist itself reports the write incomplete,
        # naming the steps still owed, and left its intent open.
        self.assertEqual(doc["outcome"], "written_incomplete", doc)
        intent = self.only_intent()
        self.assertEqual(_sha256_file(self.full(path)), intent["intended_sha256"])
        return doc, intent

    def test_fail_before_change_record_is_recovered_to_one_record_and_one_commit(self):
        path = "documents/failrec/failrec.md"
        doc, intent = self._failed_write("before_change_record", path)
        self.assertEqual(doc["pending"], ["change_record", "commit"])
        op_key = intent["op_key"]
        self.assertEqual(self.change_records_for(op_key), [])
        self.assertEqual(self.commits_for(op_key), [])

        proc, report = self.recover()
        self.assertEqual(proc.returncode, 0, report)
        self.assertEqual([(e["path"], e["classification"]) for e in report["intents"]], [(path, "applied")])
        self.assertEqual(report["intents"][0]["finished"], ["change_record", "commit"])
        self.assertEqual(len(self.change_records_for(op_key)), 1)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])

    def test_fail_before_commit_is_recovered_without_a_second_record(self):
        path = "documents/failcommit/failcommit.md"
        doc, intent = self._failed_write("before_commit", path)
        self.assertEqual(doc["pending"], ["commit"])
        op_key = intent["op_key"]
        records_before = self.change_records_for(op_key)
        self.assertEqual(len(records_before), 1)  # the record persist did write
        self.assertEqual(self.commits_for(op_key), [])

        proc, report = self.recover()
        self.assertEqual(proc.returncode, 0, report)
        self.assertEqual([(e["path"], e["classification"]) for e in report["intents"]], [(path, "applied")])
        self.assertEqual(report["intents"][0]["finished"], ["commit"])
        self.assertEqual(self.change_records_for(op_key), records_before)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])


class ExactlyOnceTests(RecoverTestCase):
    """Recovery finishes bookkeeping "exactly once", a property of the code, so
    it is observed against a state the fault seam cannot produce: an intent
    still open while its bookkeeping is already complete. No fault point sits between the commit
    and the intent's removal, so the state is constructed -- the intent file is
    copied aside before the first recovery and restored after it. The second
    recovery then meets an open intent whose change record and commit both
    exist, and must add neither again."""

    def test_open_intent_over_completed_bookkeeping_adds_no_record_and_no_commit(self):
        self.fresh_brain(git=True)
        path = "documents/once/once.md"
        self.persist_killed(self.create_request(path), "after_apply")
        intent_name = self.intent_files()[0]
        intent_path = os.path.join(self.root, ".brain", "journal", "intents", intent_name)
        with open(intent_path, "rb") as fh:
            saved_intent = fh.read()
        op_key = self.only_intent()["op_key"]

        proc, first = self.recover()
        self.assertEqual(proc.returncode, 0, first)
        self.assertEqual(len(self.change_records_for(op_key)), 1)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])

        # Construct the state: bookkeeping complete, intent open again.
        with open(intent_path, "wb") as fh:
            fh.write(saved_intent)
        records = self.change_records_for(op_key)
        head = self.head()

        proc, second = self.recover()
        self.assertEqual(proc.returncode, 0, second)
        self.assertEqual(second["outcome"], "recovered")
        self.assertEqual([(e["path"], e["classification"]) for e in second["intents"]], [(path, "applied")])
        self.assertEqual(second["intents"][0]["finished"], [])
        self.assertEqual(second["intents"][0]["pending"], [])
        self.assertEqual(self.change_records_for(op_key), records)
        self.assertEqual(self.head(), head)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])


class TargetUnexpectedTests(RecoverTestCase):
    """A target modified after apply is `target_unexpected`, is not
    overwritten, and is reported with its observed hash and its backup path
    (`null` where the operation never takes one). The intent stays open."""

    MODIFIED = b"edited by hand after the write applied\n"

    def _modify(self, path):
        with open(self.full(path), "wb") as fh:
            fh.write(self.MODIFIED)
        return hashlib.sha256(self.MODIFIED).hexdigest()

    def test_create_target_modified_after_apply_is_reported_and_left_alone(self):
        self.fresh_brain(git=True)
        path = "documents/edited/edited.md"
        self.persist_killed(self.create_request(path), "after_apply")
        intent = self.only_intent()
        self.assertEqual(_sha256_file(self.full(path)), intent["intended_sha256"])
        modified_hash = self._modify(path)
        head = self.head()

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        self.assertEqual(doc["outcome"], "target_unexpected")
        (entry,) = doc["intents"]
        self.assertEqual(entry["path"], path)
        self.assertEqual(entry["classification"], "target_unexpected")
        self.assertEqual(entry["observed_sha256"], modified_hash)
        self.assertIn("backup_path", entry)  # present, and null: create takes no backup
        self.assertIsNone(entry["backup_path"])

        # Not overwritten, nothing finished on its behalf, and the intent stays open.
        with open(self.full(path), "rb") as fh:
            self.assertEqual(fh.read(), self.MODIFIED)
        self.assertEqual(self.change_records_for(intent["op_key"]), [])
        self.assertEqual(self.commits_for(intent["op_key"]), [])
        self.assertEqual(self.head(), head)
        self.assertEqual(len(self.intent_files()), 1)

    def test_update_target_modified_after_apply_reports_its_backup_path(self):
        path = "documents/editedupd/editedupd.md"
        created = self.created_in_fresh_brain(path, git=True)
        with open(self.full(path), "rb") as fh:
            prior_bytes = fh.read()
        request = {
            "operation": "update",
            "path": path,
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        self.persist_killed(request, "after_apply")
        intent = self.only_intent()
        recorded_backup = intent["backup_path"]
        self.assertTrue(recorded_backup)
        with open(recorded_backup, "rb") as fh:
            self.assertEqual(fh.read(), prior_bytes)  # the backup holds the displaced bytes
        modified_hash = self._modify(path)

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        (entry,) = doc["intents"]
        self.assertEqual(entry["classification"], "target_unexpected")
        self.assertEqual(entry["observed_sha256"], modified_hash)
        self.assertEqual(entry["backup_path"], recorded_backup)
        with open(self.full(path), "rb") as fh:
            self.assertEqual(fh.read(), self.MODIFIED)
        self.assertEqual(len(self.intent_files()), 1)

    def test_target_deleted_after_apply_reports_it_absent(self):
        self.fresh_brain()
        path = "documents/gone/gone.md"
        created = self.created(path)
        request = {
            "operation": "update",
            "path": path,
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        self.persist_killed(request, "after_apply")
        os.remove(self.full(path))

        proc, doc = self.recover()
        self.assertEqual(doc["outcome"], "target_unexpected", doc)
        self.assertEqual(doc["intents"][0]["observed_sha256"], "absent")
        self.assertFalse(os.path.exists(self.full(path)))

    def test_every_target_unexpected_entry_carries_the_same_keys(self):
        """An ordinary entry and an `invalid_intent` one differ in values, never
        in which keys exist, so a consumer never has to tell an absent field from
        a null one. `reason` is `null` where recovery recorded none."""
        self.fresh_brain(git=True)
        edited = "documents/a-edited/a.md"
        broken = "documents/b-broken/b.md"
        self.persist_killed(self.create_request(edited), "after_apply")
        self.persist_killed(self.create_request(broken), "after_intent")
        self._modify(edited)
        broken_file = "documents__b-broken__b.md.json"
        with open(os.path.join(self.root, ".brain", "journal", "intents", broken_file), "r+", encoding="utf-8") as fh:
            text = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(text[: len(text) // 2])  # a write cut short

        _, doc = self.recover()
        by_file = {e["intent_file"]: e for e in doc["intents"]}
        ordinary = by_file["documents__a-edited__a.md.json"]
        invalid = by_file[broken_file]
        self.assertEqual(ordinary["classification"], "target_unexpected")
        self.assertEqual(invalid["classification"], "target_unexpected")
        self.assertIsNone(ordinary["reason"])
        self.assertEqual(invalid["reason"], "invalid_intent")
        expected_keys = {
            "intent_file", "path", "op_key", "run_id", "operation", "classification",
            "expected_prior", "intended_sha256", "observed_sha256", "backup_path", "reason",
        }  # the C6 table's row for `target_unexpected`, and the envelope's own fields
        self.assertEqual(set(ordinary), expected_keys)
        self.assertEqual(set(invalid), expected_keys)

    def test_unresolved_state_is_reported_again_and_still_blocks_writes(self):
        """`target_unexpected` classifies "could not establish what
        happened", so nothing resolves it: every later recovery reports it
        again, and Behaviour 21 keeps the path locked against persist."""
        self.fresh_brain()
        path = "documents/stuck/stuck.md"
        self.persist_killed(self.create_request(path), "after_apply")
        self._modify(path)

        _, first = self.recover()
        _, second = self.recover()
        self.assertEqual(first["outcome"], "target_unexpected")
        self.assertEqual(second, first)
        self.assertEqual(len(self.intent_files()), 1)

        proc = self.persist(self.create_request(path))
        self.assertEqual(support.parse_single_json(proc.stdout), {"outcome": "refused", "reason": "open_intent"})


class NonRegularTargetTests(RecoverTestCase):
    """Recovery hashes only a regular file inside the brain. Anything else is
    reported and never hashed, and no change record or commit asserts a hash
    for bytes recovery did not read from a regular file it owns."""

    def _applied_create(self, path):
        """A create killed at `after_apply`: the target holds the intended
        bytes, and there is an intent, and no record or commit yet."""
        self.persist_killed(self.create_request(path), "after_apply")
        intent = self.only_intent()
        self.assertEqual(_sha256_file(self.full(path)), intent["intended_sha256"])
        return intent

    def _regular_file_in_place(self, path):
        """True when `path` is a regular file at exactly that place in the brain,
        reached through no link."""
        target = self.full(path)
        if not os.path.lexists(target):
            return False
        return stat.S_ISREG(os.lstat(target).st_mode) and os.path.realpath(target) == os.path.join(os.path.realpath(self.root), path)

    def test_target_that_is_not_a_regular_file_in_the_brain_is_reported_not_hashed(self):
        outside = tempfile.mkdtemp(prefix="brain-recover-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        cases = ("symlink to the real file", "dangling symlink", "directory", "symlinked folder")
        for label in cases:
            with self.subTest(label):
                self.tearDown_root()
                for leftover in os.listdir(outside):
                    shutil.rmtree(os.path.join(outside, leftover), ignore_errors=True)
                self.fresh_brain(git=True)
                path = "documents/moved/moved.md"
                intent = self._applied_create(path)
                with open(self.full(path), "rb") as fh:
                    real_bytes = fh.read()
                elsewhere = os.path.join(outside, "moved")
                target = self.full(path)
                if label == "symlink to the real file":
                    shutil.move(target, os.path.join(outside, "moved.md"))
                    os.symlink(os.path.join(outside, "moved.md"), target)
                    kept = os.path.join(outside, "moved.md")
                elif label == "dangling symlink":
                    os.remove(target)
                    os.symlink(os.path.join(outside, "nothing-here"), target)
                    kept = None
                elif label == "directory":
                    os.remove(target)
                    os.mkdir(target)
                    kept = None
                else:
                    shutil.move(os.path.dirname(target), elsewhere)
                    os.symlink(elsewhere, os.path.dirname(target))
                    kept = os.path.join(elsewhere, "moved.md")
                # The injection landed: what is at the target is not a regular
                # file inside the brain, and (for the linked cases) the bytes
                # the intent expects are still reachable through it.
                self.assertFalse(self._regular_file_in_place(path))
                head = self.head()

                proc, doc = self.recover()
                self.assertEqual(proc.returncode, 3, doc)
                self.assertEqual(doc["outcome"], "target_unexpected")
                (entry,) = doc["intents"]
                self.assertEqual(entry["classification"], "target_unexpected")
                # A symlinked folder is caught one check earlier: the temp
                # file persist names beside the target now resolves outside
                # the brain, so the intent itself is not one recovery trusts.
                expected_reason = "invalid_intent" if label == "symlinked folder" else "not_a_regular_file"
                self.assertEqual(entry["reason"], expected_reason)
                self.assertIsNone(entry["observed_sha256"])

                # What the brain's own record says: no hash was asserted for it.
                self.assertEqual(self.change_records_for(intent["op_key"]), [])
                self.assertEqual(self.commits_for(intent["op_key"]), [])
                self.assertEqual(self.head(), head)
                self.assertEqual(len(self.intent_files()), 1)
                if kept is not None:
                    with open(kept, "rb") as fh:
                        self.assertEqual(fh.read(), real_bytes)


    def test_intent_whose_path_leaves_the_brain_hashes_nothing_outside_it(self):
        """The intent is a file on disk, so its `path` is checked like any other
        field: a regular file outside the brain that holds exactly the intended
        bytes must not make the write `applied` or earn a change record."""
        outside = tempfile.mkdtemp(prefix="brain-recover-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        outside_file = os.path.join(outside, "same-bytes.md")
        for label in ("absolute path", "path climbing out with ..", "path through a linked folder"):
            with self.subTest(label):
                self.tearDown_root()
                self.fresh_brain(git=True)
                intent = self._applied_create("documents/leaving/leaving.md")
                with open(self.full("documents/leaving/leaving.md"), "rb") as fh:
                    intended_bytes = fh.read()
                with open(outside_file, "wb") as fh:
                    fh.write(intended_bytes)  # the injection: the outside bytes are exactly what is expected
                self.assertEqual(_sha256_file(outside_file), intent["intended_sha256"])
                if label == "absolute path":
                    leaving = outside_file
                elif label == "path climbing out with ..":
                    leaving = os.path.relpath(outside_file, self.root)
                else:
                    # Inside the brain, but reached through a link: the same
                    # file under another name, which is not the path named.
                    os.symlink(self.full("documents/leaving"), self.full("documents/hop"))
                    leaving = "documents/hop/leaving.md"
                intent_file = os.path.join(self.root, ".brain", "journal", "intents", self.intent_files()[0])
                with open(intent_file, encoding="utf-8") as fh:
                    record = json.load(fh)
                record["path"] = leaving
                with open(intent_file, "w", encoding="utf-8") as fh:
                    json.dump(record, fh)
                head = self.head()

                proc, doc = self.recover()
                self.assertEqual(proc.returncode, 3, doc)
                (entry,) = doc["intents"]
                self.assertEqual((entry["classification"], entry.get("reason")), ("target_unexpected", "not_a_regular_file"))
                self.assertEqual(self.change_records_for(intent["op_key"]), [])
                self.assertEqual(self.head(), head)
                self.assertEqual(len(self.intent_files()), 1)


class OrphanTempTests(RecoverTestCase):
    """A temp file with no intent is an `orphan_temp`. The fixture is
    built by the product itself: persist killed at `after_temp` writes a real
    temp file, and removing that write's intent leaves the temp unowned. The
    temp file's name is therefore whatever persist gives it -- never a string
    this test writes -- so a change of the naming convention on either side
    cannot pass unnoticed."""

    def _orphaned_temp(self, path):
        self.persist_killed(self.create_request(path), "after_temp")
        target_dir = os.path.dirname(path)
        (temp_name,) = self.listing(target_dir)
        intent_name = self.intent_files()[0]
        # The injection landed: a temp file beside an absent target, owned by an intent.
        self.assertFalse(os.path.exists(self.full(path)))
        self.assertEqual(self.only_intent()["temp_path"], self.full(os.path.join(target_dir, temp_name)))
        os.remove(os.path.join(self.root, ".brain", "journal", "intents", intent_name))
        return os.path.join(target_dir, temp_name)

    def test_temp_file_with_no_intent_is_reported_orphan_and_left_in_place(self):
        self.fresh_brain()
        temp = self._orphaned_temp("documents/orphan/orphan.md")

        proc, doc = self.recover()
        self.assertEqual(doc["orphan_temps"], [temp])
        self.assertEqual(doc["intents"], [])
        # An orphan temp is reported, never escalated: nothing needs attention
        # that recovery did not already settle.
        self.assertEqual(doc["outcome"], "recovered")
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(os.path.isfile(self.full(temp)))

    def test_temp_file_still_owned_by_an_intent_is_not_an_orphan(self):
        """A temp an open intent names is that intent's business: recovery
        removes it with the intent (`not_applied`) or reports it with the
        unresolved intent -- it is not also listed as an orphan."""
        self.fresh_brain()
        path = "documents/owned/owned.md"
        self.persist_killed(self.create_request(path), "after_temp")
        self._modify_target_to_unexpected(path)

        proc, doc = self.recover()
        self.assertEqual(doc["outcome"], "target_unexpected", doc)
        self.assertEqual(doc["orphan_temps"], [])
        self.assertEqual(len(self.listing("documents/owned")), 2)  # the intent's temp is still there

    def _modify_target_to_unexpected(self, path):
        with open(self.full(path), "wb") as fh:
            fh.write(b"someone else's file\n")


class ReportEnvelopeTests(RecoverTestCase):
    """The aggregate `outcome` and the per-intent detail in the "Recover
    report" envelope."""

    def test_unresolved_intent_outranks_a_recovered_one_and_does_not_stop_it(self):
        self.fresh_brain(git=True)
        applied_path = "documents/a-applied/a.md"
        stuck_path = "documents/b-stuck/b.md"
        self.persist_killed(self.create_request(applied_path), "after_apply")
        self.persist_killed(self.create_request(stuck_path), "after_apply")
        with open(self.full(stuck_path), "wb") as fh:
            fh.write(b"changed\n")

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        self.assertEqual(doc["outcome"], "target_unexpected")
        self.assertEqual(
            [(e["path"], e["classification"]) for e in doc["intents"]],
            [(applied_path, "applied"), (stuck_path, "target_unexpected")],
        )
        self.assertEqual(len(self.intent_files()), 1)  # only the stuck one remains

    def test_ordinary_intent_that_carries_a_run_id_is_not_run_pending(self):
        """C6's `run_pending` is for the `run_id` of a run with a manifest.
        Persist gives every intent a `run_id`, so the field alone means nothing."""
        self.fresh_brain()
        path = "documents/runid/runid.md"
        self.persist_killed(self.create_request(path, run_id="run-chosen-by-caller"), "after_apply")
        self.assertEqual(self.only_intent()["run_id"], "run-chosen-by-caller")

        proc, doc = self.recover()
        self.assertEqual(doc["outcome"], "recovered", doc)
        self.assertEqual([(e["path"], e["classification"]) for e in doc["intents"]], [(path, "applied")])

    def test_bookkeeping_that_cannot_finish_leaves_the_intent_open_and_is_retried(self):
        """A commit git refuses (here: the path became ignored) is not
        `recovered`. The intent stays open, the report names the pending step
        and exits 3, and a later recovery finishes only what is still owed."""
        self.fresh_brain(git=True)
        path = "documents/ignored/ignored.md"
        self.persist_killed(self.create_request(path), "after_apply")
        op_key = self.only_intent()["op_key"]
        gitignore = self.full(".gitignore")
        with open(gitignore, "a", encoding="utf-8") as fh:
            fh.write("documents/ignored/\n")

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        self.assertEqual(doc["outcome"], "recovery_incomplete")
        (entry,) = doc["intents"]
        self.assertEqual(entry["classification"], "applied")
        self.assertEqual(entry["finished"], ["change_record"])
        self.assertEqual(entry["pending"], ["commit"])
        self.assertEqual(len(self.intent_files()), 1)
        self.assertEqual(self.commits_for(op_key), [])

        with open(gitignore, "w", encoding="utf-8") as fh:
            fh.write(".brain/journal/\n.brain/state/\n")
        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 0, doc)
        self.assertEqual(doc["outcome"], "recovered")
        self.assertEqual(doc["intents"][0]["finished"], ["commit"])
        self.assertEqual(doc["intents"][0]["pending"], [])
        self.assertEqual(len(self.change_records_for(op_key)), 1)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])


    def test_recovery_incomplete_is_an_aggregate_outcome_never_a_classification(self):
        """`recovery_incomplete` names the whole report. An intent whose
        bookkeeping could not finish is still classified `applied`, and its
        non-empty `pending` is the per-intent signal."""
        self.fresh_brain(git=True)
        path = "documents/incomplete/incomplete.md"
        self.persist_killed(self.create_request(path), "after_apply")
        with open(self.full(".gitignore"), "a", encoding="utf-8") as fh:
            fh.write("documents/incomplete/\n")  # git now refuses the commit

        _, doc = self.recover()
        self.assertEqual(doc["outcome"], "recovery_incomplete")
        classifications = {entry["classification"] for entry in doc["intents"]}
        self.assertEqual(classifications, {"applied"})
        self.assertNotEqual(doc["intents"][0]["pending"], [])
        self.assertLessEqual(classifications, {"not_applied", "applied", "target_unexpected", "run_pending"})


class ModuleFolderOrphanTests(RecoverTestCase):
    def test_temp_file_in_an_installed_module_folder_is_an_orphan(self):
        self.fresh_brain()
        module_dir = os.path.join(self.root, ".brain", "modules", "projects")
        os.makedirs(module_dir)
        with open(os.path.join(module_dir, "module.json"), "w", encoding="utf-8") as fh:
            json.dump({"module": "projects", "folder": "projects"}, fh)
        request = {"operation": "attach", "path": "projects/p/img.bin", "content_base64": "AAEC"}
        self.persist_killed(request, "after_temp")
        (temp_name,) = self.listing("projects/p")
        os.remove(os.path.join(self.root, ".brain", "journal", "intents", self.intent_files()[0]))

        proc, doc = self.recover()
        self.assertEqual(doc["orphan_temps"], [f"projects/p/{temp_name}"])
        self.assertEqual(doc["outcome"], "recovered")


class InvalidIntentTests(RecoverTestCase):
    """An intent recovery cannot trust is never acted on and never dropped: it
    is reported `target_unexpected` (the tool cannot establish what happened),
    with `reason: invalid_intent`, and stays open."""

    def _intent_file(self):
        return os.path.join(self.root, ".brain", "journal", "intents", self.intent_files()[0])

    def test_unreadable_intent_is_reported_and_kept_and_the_others_still_recover(self):
        self.fresh_brain(git=True)
        good = "documents/good/good.md"
        broken = "documents/zbroken/broken.md"
        self.persist_killed(self.create_request(good), "after_apply")
        self.persist_killed(self.create_request(broken), "after_intent")
        broken_file = "documents__zbroken__broken.md.json"
        with open(os.path.join(self.root, ".brain", "journal", "intents", broken_file), "r+", encoding="utf-8") as fh:
            text = fh.read()
            fh.seek(0)
            fh.truncate()
            fh.write(text[: len(text) // 2])  # a write cut short

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        self.assertEqual(doc["outcome"], "target_unexpected")
        by_file = {e["intent_file"]: e for e in doc["intents"]}
        self.assertEqual(by_file[broken_file]["classification"], "target_unexpected")
        self.assertEqual(by_file[broken_file]["reason"], "invalid_intent")
        self.assertIsNone(by_file[broken_file]["observed_sha256"])
        self.assertIsNone(by_file[broken_file]["backup_path"])
        self.assertEqual(by_file["documents__good__good.md.json"]["classification"], "applied")
        self.assertEqual(self.intent_files(), [broken_file])

    def test_intent_naming_a_temp_it_should_not_own_removes_nothing(self):
        outside = tempfile.mkdtemp(prefix="brain-recover-outside-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        inside_precious = "documents/precious.md"
        cases = {
            "outside the brain": os.path.join(outside, ".brain-persist-tmp-elsewhere"),
            "inside the brain but not a temp file": self.full(inside_precious),
        }
        for label, victim in cases.items():
            with self.subTest(label):
                self.tearDown_root()
                self.fresh_brain()
                with open(self.full("documents/.keep"), "w", encoding="utf-8") as fh:
                    fh.write("")
                with open(victim, "w", encoding="utf-8") as fh:
                    fh.write("not a persist temp file\n")
                self.persist_killed(self.create_request("documents/victim/victim.md"), "before_apply")
                with open(self._intent_file(), encoding="utf-8") as fh:
                    record = json.load(fh)
                record["temp_path"] = victim
                with open(self._intent_file(), "w", encoding="utf-8") as fh:
                    json.dump(record, fh)

                proc, doc = self.recover()
                self.assertEqual(doc["intents"][0]["reason"], "invalid_intent", doc)
                self.assertTrue(os.path.isfile(victim))
                self.assertEqual(len(self.intent_files()), 1)


class OneIntentsFailureTests(RecoverTestCase):
    """One intent's failure is that intent's, not the command's: it is reported
    in its own entry, every other intent is still classified and finished, and
    the command still prints one JSON object carrying `outcome`."""

    def _make_unreadable(self, path, mode):
        """Applies `mode` to `path` and proves it bites, so a test never claims
        a failure the filesystem did not produce. Registered before the change
        so the fixture stays deletable whatever happens next."""
        original = os.stat(path).st_mode & 0o777
        self.addCleanup(os.chmod, path, original)
        os.chmod(path, mode)
        return original

    def test_unreadable_target_is_that_intents_failure_and_the_next_intent_still_recovers(self):
        self.fresh_brain(git=True)
        first = "documents/a-locked/a.md"
        second = "documents/b-fine/b.md"
        self.persist_killed(self.create_request(first), "after_apply")
        self.persist_killed(self.create_request(second), "after_apply")
        keys = {e["path"]: e["op_key"] for e in self._intents()}
        self._make_unreadable(self.full(first), 0o000)
        with self.assertRaises(PermissionError):
            open(self.full(first), "rb").close()  # the injection landed

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        self.assertEqual(doc["outcome"], "target_unexpected")
        by_path = {e["path"]: e for e in doc["intents"]}
        self.assertEqual(sorted(by_path), [first, second])
        self.assertEqual(by_path[first]["classification"], "target_unexpected")
        self.assertEqual(by_path[first]["reason"], "recovery_failed")
        self.assertIsNone(by_path[first]["observed_sha256"])
        # The other intent was processed, not merely skipped without an error.
        self.assertEqual(by_path[second]["classification"], "applied")
        self.assertEqual(by_path[second]["finished"], ["change_record", "commit"])
        self.assertEqual(len(self.change_records_for(keys[second])), 1)
        self.assertEqual(len(self.commits_for(keys[second])), 1)
        # The failed one wrote nothing and stays open for a later recovery.
        self.assertEqual(self.change_records_for(keys[first]), [])
        self.assertEqual(self.commits_for(keys[first]), [])
        self.assertEqual(self.intent_files(), ["documents__a-locked__a.md.json"])

    def test_intent_that_cannot_be_closed_is_reported_and_finishes_once_when_it_can(self):
        """With the journal's intents folder read-only, the change record and
        commit are made and the intent then cannot be closed. The report is not
        lost, and the recovery that follows repeats neither step."""
        self.fresh_brain(git=True)
        path = "documents/stuck-open/stuck.md"
        self.persist_killed(self.create_request(path), "after_apply")
        op_key = self.only_intent()["op_key"]
        intents_dir = os.path.join(self.root, ".brain", "journal", "intents")
        self._make_unreadable(intents_dir, 0o500)
        probe = os.path.join(intents_dir, "probe")
        with self.assertRaises(PermissionError):
            open(probe, "wb").close()  # the folder really refuses changes now
        with self.assertRaises(PermissionError):
            os.remove(os.path.join(intents_dir, self.intent_files()[0]))

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        self.assertEqual(doc["outcome"], "target_unexpected")
        (entry,) = doc["intents"]
        self.assertEqual((entry["path"], entry["reason"]), (path, "recovery_failed"))
        self.assertEqual(len(self.intent_files()), 1)

        os.chmod(intents_dir, 0o700)
        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 0, doc)
        self.assertEqual(doc["outcome"], "recovered")
        self.assertEqual(doc["intents"][0]["finished"], [])
        self.assertEqual(len(self.change_records_for(op_key)), 1)
        self.assertEqual(len(self.commits_for(op_key)), 1)
        self.assertEqual(self.intent_files(), [])

    def test_intent_with_a_path_no_filesystem_accepts_is_reported_and_the_next_still_recovers(self):
        """The same isolation without depending on file permissions: a
        well-formed intent whose `temp_path` holds a NUL byte."""
        self.fresh_brain(git=True)
        bad = "documents/a-nul/a.md"
        good = "documents/b-good/b.md"
        self.persist_killed(self.create_request(bad), "after_apply")
        self.persist_killed(self.create_request(good), "after_apply")
        bad_file = os.path.join(self.root, ".brain", "journal", "intents", "documents__a-nul__a.md.json")
        with open(bad_file, encoding="utf-8") as fh:
            record = json.load(fh)
        folder, name = os.path.split(record["temp_path"])
        record["temp_path"] = os.path.join(folder + "\u0000", name)
        with open(bad_file, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        good_key = [i for i in self._intents() if i["path"] == good][0]["op_key"]

        proc, doc = self.recover()
        self.assertEqual(proc.returncode, 3, doc)
        by_path = {e["path"]: e for e in doc["intents"]}
        self.assertEqual(by_path[bad]["classification"], "target_unexpected")
        self.assertEqual(by_path[good]["classification"], "applied")
        self.assertEqual(len(self.commits_for(good_key)), 1)
        self.assertEqual(self.intent_files(), ["documents__a-nul__a.md.json"])

    def _intents(self):
        directory = os.path.join(self.root, ".brain", "journal", "intents")
        found = []
        for name in sorted(os.listdir(directory)):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                found.append(json.load(fh))
        return found


class TempRemovalTests(RecoverTestCase):
    """Recovery removes only the file it validated. The path checked and the
    path removed are one path, so a file the command does not own can neither
    be destroyed nor hide the real orphan."""

    def _intent_file(self):
        return os.path.join(self.root, ".brain", "journal", "intents", self.intent_files()[0])

    def _interrupted_create(self, path):
        """A create killed at `before_apply`: an open intent, and beside the
        absent target the real temp file persist wrote for it."""
        self.persist_killed(self.create_request(path), "before_apply")
        intent = self.only_intent()
        self.assertFalse(os.path.exists(self.full(path)))
        temp = intent["temp_path"]
        self.assertTrue(os.path.isfile(temp) and not os.path.islink(temp))
        return intent, temp

    def _point_intent_at(self, victim):
        with open(self._intent_file(), encoding="utf-8") as fh:
            record = json.load(fh)
        record["temp_path"] = victim
        with open(self._intent_file(), "w", encoding="utf-8") as fh:
            json.dump(record, fh)

    def test_intent_naming_a_link_to_a_temp_removes_neither_and_reports_the_real_temp(self):
        for label, link_name in (
            ("a link not named like a temp", "IMPORTANT-not-a-temp.md"),
            ("a link named like a temp", ".brain-persist-tmp-0-linked"),
        ):
            with self.subTest(label):
                self.tearDown_root()
                self.fresh_brain()
                path = "documents/linked/linked.md"
                _, temp = self._interrupted_create(path)
                link = os.path.join(os.path.dirname(temp), link_name)
                os.symlink(temp, link)
                self._point_intent_at(link)

                proc, doc = self.recover()
                (entry,) = doc["intents"]
                self.assertEqual((entry["classification"], entry.get("reason")), ("target_unexpected", "invalid_intent"), doc)
                self.assertNotIn("temp_removed", entry)
                # Both files are still there: the link the intent named and the
                # real temp behind it. Nothing was removed on the strength of
                # a path the check did not look at.
                self.assertTrue(os.path.islink(link))
                self.assertTrue(os.path.isfile(temp))
                self.assertEqual(len(self.intent_files()), 1)
                # The real temp is nobody's: it is reported, not hidden.
                self.assertEqual(doc["orphan_temps"], [os.path.relpath(temp, self.root)])
                self.assertEqual(doc["outcome"], "target_unexpected")


class UnimplementedModesTests(RecoverTestCase):
    """C6 and C8 name two more `recover` modes; neither is this story's. A
    request for one is refused, so it is never quietly answered with a plain
    recovery that changes the brain."""

    def test_restore_retained_and_start_journal_are_refused_and_change_nothing(self):
        self.fresh_brain()
        self.persist_killed(self.create_request("documents/modes/modes.md"), "after_apply")
        before = support.hash_tree(self.root)
        for extra in (["--restore-retained", "documents/modes/modes.md"], ["--start-journal"]):
            with self.subTest(extra=extra):
                proc, doc = self.recover(*extra)
                self.assertEqual(proc.returncode, 2, doc)
                self.assertEqual(doc, {"outcome": "refused", "reason": "not_implemented"})
                self.assertEqual(support.hash_tree(self.root), before)


if __name__ == "__main__":
    unittest.main()
