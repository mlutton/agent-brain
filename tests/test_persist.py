"""Tests for `brain persist` (ST-02), against docs/specification/beta.md C4/C6/C7."""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import support
from tests.yaml12 import make_yaml12_loader

# The vendored YAML library is a pinned dependency, not a product module (see
# vendor/VENDORED.md): tests read a persisted file's frontmatter block with it
# directly, exactly as a real consumer (or Obsidian) would, never by importing
# brain_core to assert on internal structure (see this file's own module
# docstring convention, mirrored from README.md's Contributing section).
sys.path.insert(0, os.path.join(support.REPO_ROOT, "vendor"))
import yaml as _yaml_module  # noqa: E402

_YAML12_LOADER = make_yaml12_loader(_yaml_module)


def _frontmatter_block(text):
    """Splits a canonical `---\\n<block>---\\n<body>` file's own frontmatter
    block out for reparsing, by the same plain marker rule C4 defines --
    without importing brain_core's own frontmatter module."""
    lines = text.splitlines(keepends=True)
    assert lines[0].rstrip("\r\n") == "---"
    close_idx = next(i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---")
    return "".join(lines[1:close_idx])


def _run_git(args, cwd):
    subprocess.run(
        ["git"] + args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        ),
    )


def build_brain(root, git=False):
    """A minimal synthetic brain: the C2 layout plus a present journal, built
    directly (like tests/test_validate.py's fixtures) rather than through
    `brain setup`, since persist needs only the journal and, for some cases,
    a git repository -- not a full copied runtime."""
    for zone in ("inbox", "raw", "documents", "wiki"):
        os.makedirs(os.path.join(root, zone), exist_ok=True)
    os.makedirs(os.path.join(root, ".brain", "types", "custom"), exist_ok=True)
    os.makedirs(os.path.join(root, ".brain", "journal"), exist_ok=True)
    with open(os.path.join(root, ".brain", "journal", "epoch.json"), "w", encoding="utf-8") as fh:
        json.dump({"brain_id": "test-brain-0000000000000000000000000"}, fh)
    if git:
        _run_git(["init", "-q"], root)
        with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write(".brain/journal/\n.brain/state/\n")
        _run_git(["add", "-A"], root)
        _run_git(["commit", "-q", "-m", "initial"], root)
    return root


NOTE_FRONTMATTER = {
    "type": "note",
    "title": "Hello Note",
    "summary": "A test document.",
    "status": "draft",
    "created": "2026-01-01",
    "reviewed": "2026-01-01",
    "origin": "authored",
    "evidence": [],
    "kind": "decision",
    "authored_by": "human",
    "retention": "durable",
}


class PersistTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="brain-persist-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        build_brain(self.root)

    def git_root(self):
        self.root = tempfile.mkdtemp(prefix="brain-persist-git-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        build_brain(self.root, git=True)
        return self.root

    def persist(self, request, root=None):
        root = root or self.root
        proc = support.run_brain(
            ["persist", "--root", root], input_data=json.dumps(request)
        )
        return proc, support.parse_single_json(proc.stdout)


class CreateTests(PersistTestCase):
    """P1: create of a new valid document returns written with id, path and
    version equal to the file's SHA-256, and leaves a change record."""

    def test_create_returns_written_with_id_path_and_sha256_version(self):
        request = {
            "operation": "create",
            "path": "documents/hello/hello.md",
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body text.\n",
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(doc["path"], "documents/hello/hello.md")
        self.assertTrue(doc["id"])

        full = os.path.join(self.root, "documents/hello/hello.md")
        self.assertTrue(os.path.isfile(full))
        import hashlib

        with open(full, "rb") as fh:
            actual_hash = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(doc["version"], actual_hash)

        change_records = os.listdir(os.path.join(self.root, ".brain", "journal", "change_records"))
        self.assertEqual(len(change_records), 1)

    def test_create_leaves_no_temp_file_sibling_in_target_directory(self):
        """F1: `_apply_create_or_attach` hard-links the temp onto the target
        and nothing removed the temp afterwards, so every successful create
        left a hidden `.brain-persist-tmp-<pid>-<ulid>` sibling behind.
        Assert the whole directory listing, not merely the temp prefix's
        absence -- a listing check still catches the defect even if the
        prefix ever changes."""
        request = {
            "operation": "create",
            "path": "documents/notemp/notemp.md",
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body text.\n",
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)

        target_dir = os.path.join(self.root, "documents/notemp")
        self.assertEqual(os.listdir(target_dir), ["notemp.md"])

    def test_second_create_on_same_path_is_refused_exists_and_file_unchanged(self):
        """P2: a second create on the same path returns refused (exists), the
        file is unchanged, and there is no code path that overwrites."""
        request = {
            "operation": "create",
            "path": "documents/dup/dup.md",
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "First body.\n",
        }
        _, first = self.persist(request)
        self.assertEqual(first["outcome"], "written", first)

        full = os.path.join(self.root, "documents/dup/dup.md")
        with open(full, "rb") as fh:
            before = fh.read()

        second_request = dict(request)
        second_request["frontmatter"] = dict(NOTE_FRONTMATTER)
        second_request["body"] = "Second body -- must never land.\n"
        proc, second = self.persist(second_request)
        # C6's outcome table: a `refused` outcome always reports the
        # observed hash alongside its reason.
        self.assertEqual(second["outcome"], "refused")
        self.assertEqual(second["reason"], "exists")
        self.assertIn("observed_sha256", second)
        self.assertEqual(proc.returncode, 2)

        with open(full, "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after)

    def test_apply_step_itself_refuses_to_overwrite_a_target_that_appears_after_the_precheck(self):
        """P2/Behaviour 14, decided by injection at the true TOCTOU gap: an
        ordinary run can never reach the apply step with the target already
        present, because create's `expected_prior` is always "absent" (A7)
        and step 4's own re-check already refuses whenever the target exists
        at that moment. The only way to exercise the apply step's own
        no-overwrite guarantee is to land a conflicting write in the gap
        between that re-check and the apply itself -- exactly what the
        BRAIN_TEST_CONFLICT hook does."""
        path = "documents/raceoverwrite/race.md"
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Intended body -- must never land.\n",
        }
        proc = support.run_brain(
            ["persist", "--root", self.root],
            input_data=json.dumps(request),
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_TEST_CONFLICT=path),
        )
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "failed_before_apply", doc)
        # F6 (Owner ruling): the temp file this failure's own cleanup just
        # removed is genuinely gone, so `temp_path` must not be reported at
        # all -- a path to an already-deleted file reports nothing true.
        self.assertNotIn("temp_path", doc, doc)

        full = os.path.join(self.root, path)
        with open(full, "rb") as fh:
            actual = fh.read()
        # The conflicting bytes injected into the gap must survive
        # untouched: no fallback (an os.replace, for example) may land the
        # intended content over them.
        self.assertEqual(actual, b"conflicting out-of-band content")


class CreateAlwaysMintsIdTests(PersistTestCase):
    """A7: persist mints the document id itself -- C6's request carries no
    `id` field, and C4 requires the id minted once, never derived from the
    path or accepted from the caller. A caller-supplied `frontmatter.id` must
    never end up as the document's id."""

    def test_create_ignores_a_caller_supplied_id_and_mints_its_own(self):
        fm = dict(NOTE_FRONTMATTER)
        fm["id"] = "zzzzzzzzzzzzzzzzzzzzzzzzzz"  # a caller-supplied, well-formed id
        request = {
            "operation": "create",
            "path": "documents/mint/mint.md",
            "frontmatter": fm,
            "body": "Body.\n",
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)
        self.assertNotEqual(doc["id"], "zzzzzzzzzzzzzzzzzzzzzzzzzz")

        full = os.path.join(self.root, "documents/mint/mint.md")
        with open(full, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("zzzzzzzzzzzzzzzzzzzzzzzzzz", text)
        self.assertIn(f'id: "{doc["id"]}"', text)


class UpdateTests(PersistTestCase):
    """P3: an update with a stale expected_version after a hand edit returns
    refused (version_mismatch), with the hand edit intact."""

    def _create(self, path, extra_frontmatter=None):
        fm = dict(NOTE_FRONTMATTER)
        if extra_frontmatter:
            fm.update(extra_frontmatter)
        request = {"operation": "create", "path": path, "frontmatter": fm, "body": "Body.\n"}
        _, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)
        return doc

    def test_stale_expected_version_after_hand_edit_is_refused(self):
        created = self._create("documents/stale/stale.md")
        full = os.path.join(self.root, "documents/stale/stale.md")

        # A hand edit lands after the agent read `created`'s version.
        with open(full, "a", encoding="utf-8") as fh:
            fh.write("\nHand-edited line.\n")
        with open(full, "rb") as fh:
            hand_edited_bytes = fh.read()

        update_request = {
            "operation": "update",
            "path": "documents/stale/stale.md",
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        proc, doc = self.persist(update_request)
        self.assertEqual(doc["outcome"], "refused", doc)
        self.assertEqual(doc["reason"], "version_mismatch")
        self.assertEqual(proc.returncode, 2)

        with open(full, "rb") as fh:
            after = fh.read()
        self.assertEqual(after, hand_edited_bytes)

    def test_matching_expected_version_updates_one_property(self):
        created = self._create("documents/ok/ok.md")
        update_request = {
            "operation": "update",
            "path": "documents/ok/ok.md",
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        proc, doc = self.persist(update_request)
        self.assertEqual(doc["outcome"], "written", doc)
        self.assertEqual(proc.returncode, 0)
        self.assertNotEqual(doc["version"], created["version"])

    def test_update_of_date_field_is_written_and_reparses_to_new_date(self):
        """F2: `_prepare_update`'s guard 2 re-parses the rendered block with
        the YAML 1.1 loader, which reads an unquoted `YYYY-MM-DD` as a `date`
        object, and compares it to the caller's string -- they never compare
        equal, so every update of a date field (`reviewed`, `created`,
        `fresh_until`, `first_seen`, `decided`) was refused
        unsupported_frontmatter. The README documents `update` applying any
        named property, so this must actually work."""
        created = self._create("documents/dateupd/dateupd.md")
        update_request = {
            "operation": "update",
            "path": "documents/dateupd/dateupd.md",
            "expected_version": created["version"],
            "frontmatter": {"reviewed": "2026-03-01"},
        }
        proc, doc = self.persist(update_request)
        self.assertEqual(doc["outcome"], "written", doc)
        self.assertEqual(proc.returncode, 0)

        full = os.path.join(self.root, "documents/dateupd/dateupd.md")
        with open(full, "r", encoding="utf-8") as fh:
            text = fh.read()
        block = _frontmatter_block(text)

        under_11 = next(iter(_yaml_module.load_all(block, Loader=_yaml_module.SafeLoader)))
        under_12 = next(iter(_yaml_module.load_all(block, Loader=_YAML12_LOADER)))
        self.assertEqual(under_11["reviewed"], datetime.date(2026, 3, 1))
        self.assertEqual(under_12["reviewed"], "2026-03-01")


class NoOpUpdateTests(PersistTestCase):
    """agent-brain #38/#42: a no-op update -- one whose resulting file
    bytes would equal the target's current bytes -- is refused before
    anything is written, so it can never leave an open intent with no CLI
    escape to close it. Precedence is pinned: expected_version is checked
    before no_change (agent-brain #38/#42)."""

    def _create(self, path, root=None):
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        _, doc = self.persist(request, root=root)
        self.assertEqual(doc["outcome"], "written", doc)
        return doc

    def test_noop_update_is_refused_no_change_and_writes_nothing(self):
        created = self._create("documents/noop/noop.md")
        full = os.path.join(self.root, "documents/noop/noop.md")
        with open(full, "rb") as fh:
            before = fh.read()
        change_records_dir = os.path.join(self.root, ".brain", "journal", "change_records")
        change_records_before = sorted(os.listdir(change_records_dir))

        request = {
            "operation": "update",
            "path": "documents/noop/noop.md",
            "expected_version": created["version"],
            "frontmatter": {"status": NOTE_FRONTMATTER["status"]},  # already what it holds
        }
        proc, doc = self.persist(request)
        self.assertEqual(
            doc,
            {"outcome": "refused", "reason": "no_change", "observed_sha256": created["version"]},
        )
        self.assertEqual(proc.returncode, 2)

        with open(full, "rb") as fh:
            after = fh.read()
        self.assertEqual(after, before)

        intents_dir = os.path.join(self.root, ".brain", "journal", "intents")
        self.assertEqual(os.listdir(intents_dir) if os.path.isdir(intents_dir) else [], [])
        self.assertEqual(sorted(os.listdir(change_records_dir)), change_records_before)
        self.assertEqual(os.listdir(os.path.dirname(full)), ["noop.md"])

    def test_noop_update_with_stale_expected_version_is_refused_version_mismatch(self):
        """Twin: precedence pins expected_version before no_change -- a
        no-op-by-bytes request whose expected_version is stale is
        version_mismatch, not no_change. The JSON matches what a stale
        non-no-op update returns from step 4 today, so a caller cannot tell
        which check refused it (agent-brain #38/#42)."""
        created = self._create("documents/noopstale/noopstale.md")
        full = os.path.join(self.root, "documents/noopstale/noopstale.md")
        first_update = {
            "operation": "update",
            "path": "documents/noopstale/noopstale.md",
            "expected_version": created["version"],
            "frontmatter": {"title": "Renamed"},
        }
        _, moved = self.persist(first_update)
        self.assertEqual(moved["outcome"], "written", moved)
        with open(full, "rb") as fh:
            before = fh.read()

        # Its own values already equal what the target now holds (a no-op by
        # bytes), but expected_version still names the original, stale
        # version.
        stale_request = {
            "operation": "update",
            "path": "documents/noopstale/noopstale.md",
            "expected_version": created["version"],
            "frontmatter": {"title": "Renamed"},
        }
        proc, doc = self.persist(stale_request)
        self.assertEqual(
            doc,
            {"outcome": "refused", "reason": "version_mismatch", "observed_sha256": moved["version"]},
        )
        self.assertEqual(proc.returncode, 2)
        with open(full, "rb") as fh:
            after = fh.read()
        self.assertEqual(after, before)

    def test_stale_non_noop_that_reconstructs_earlier_bytes_stays_version_mismatch(self):
        """Twin (stale, not a no-op): the target moved from version A to B. A
        request naming expected_version A, whose values reconstruct A's exact
        bytes, has intended SHA-256 = A = expected_version but != current (B).
        It must stay version_mismatch and never become no_change (agent-brain
        #38/#42)."""
        created = self._create("documents/stalenonoop/stalenonoop.md")  # version A
        full = os.path.join(self.root, "documents/stalenonoop/stalenonoop.md")

        moved_request = {
            "operation": "update",
            "path": "documents/stalenonoop/stalenonoop.md",
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        _, moved = self.persist(moved_request)  # version B
        self.assertEqual(moved["outcome"], "written", moved)
        self.assertNotEqual(moved["version"], created["version"])
        with open(full, "rb") as fh:
            before = fh.read()

        # Reconstructs A's exact bytes (the field goes back to its original
        # value) while still naming A as expected_version.
        reconstruct_request = {
            "operation": "update",
            "path": "documents/stalenonoop/stalenonoop.md",
            "expected_version": created["version"],
            "frontmatter": {"status": NOTE_FRONTMATTER["status"]},
        }
        proc, doc = self.persist(reconstruct_request)
        self.assertEqual(
            doc,
            {"outcome": "refused", "reason": "version_mismatch", "observed_sha256": moved["version"]},
        )
        self.assertEqual(proc.returncode, 2)
        with open(full, "rb") as fh:
            after = fh.read()
        self.assertEqual(after, before)

    def test_path_is_not_locked_after_a_no_change_refusal(self):
        """A later real update against the same expected_version is written
        (agent-brain #38/#42)."""
        created = self._create("documents/notlocked/notlocked.md")
        noop_request = {
            "operation": "update",
            "path": "documents/notlocked/notlocked.md",
            "expected_version": created["version"],
            "frontmatter": {"status": NOTE_FRONTMATTER["status"]},
        }
        _, doc = self.persist(noop_request)
        self.assertEqual(doc["reason"], "no_change", doc)

        real_request = {
            "operation": "update",
            "path": "documents/notlocked/notlocked.md",
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        proc, doc = self.persist(real_request)
        self.assertEqual(doc["outcome"], "written", doc)
        self.assertEqual(proc.returncode, 0)

    def test_replace_frontmatter_request_is_unaffected_by_the_noop_check(self):
        """Scope (agent-brain #38/#42): the no-op check applies only to a
        targeted-replacement update, the route that produces intended bytes.
        replace_frontmatter stays review_required as on the base -- it is
        refused before this function ever computes intended bytes (A4: no
        request field marks a write reviewed)."""
        created = self._create("documents/replacenoop/replacenoop.md")
        request = {
            "operation": "update",
            "path": "documents/replacenoop/replacenoop.md",
            "expected_version": created["version"],
            "replace_frontmatter": True,
            "frontmatter": dict(NOTE_FRONTMATTER),
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc, {"outcome": "refused", "reason": "review_required"})
        self.assertEqual(proc.returncode, 2)


class NoOpUpdateFaultSeamTests(PersistTestCase):
    """agent-brain #38/#42: with BRAIN_TEST_FAULTS=1 and BRAIN_FAULT=after_intent:kill, both
    no-op requests (current and stale expected_version) exit 2 with their
    refusal and never die by SIGKILL, because the after_intent checkpoint is
    never reached -- the refusal returns before `run_persist` ever writes an
    intent (agent-brain #38/#42)."""

    def _create(self, path, root=None):
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        _, doc = self.persist(request, root=root)
        self.assertEqual(doc["outcome"], "written", doc)
        return doc

    def _run_with_after_intent_kill(self, request, root):
        return support.run_brain(
            ["persist", "--root", root],
            input_data=json.dumps(request),
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="after_intent:kill"),
        )

    def _assert_end_state_untouched(self, root, path, expected_bytes, head_before):
        full = os.path.join(root, path)
        with open(full, "rb") as fh:
            self.assertEqual(fh.read(), expected_bytes)
        intents_dir = os.path.join(root, ".brain", "journal", "intents")
        self.assertEqual(os.listdir(intents_dir) if os.path.isdir(intents_dir) else [], [])
        self.assertEqual(os.listdir(os.path.dirname(full)), [os.path.basename(full)])
        backups_dir = os.path.join(root, ".brain", "state", "backups")
        self.assertFalse(os.path.isdir(backups_dir) and os.listdir(backups_dir))
        head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(head_after, head_before)

    def test_current_and_stale_noop_requests_never_die_by_sigkill_and_leave_no_trace(self):
        root = self.git_root()
        created = self._create("documents/faultnoop/faultnoop.md", root=root)
        full = os.path.join(root, "documents/faultnoop/faultnoop.md")
        with open(full, "rb") as fh:
            original_bytes = fh.read()
        change_records_dir = os.path.join(root, ".brain", "journal", "change_records")
        change_records_before = sorted(os.listdir(change_records_dir))
        head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

        current_request = {
            "operation": "update",
            "path": "documents/faultnoop/faultnoop.md",
            "expected_version": created["version"],
            "frontmatter": {"status": NOTE_FRONTMATTER["status"]},
        }
        proc = self._run_with_after_intent_kill(current_request, root)
        self.assertEqual(proc.returncode, 2, proc)  # never killed: a SIGKILL death is negative
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(
            doc,
            {"outcome": "refused", "reason": "no_change", "observed_sha256": created["version"]},
        )
        self._assert_end_state_untouched(
            root, "documents/faultnoop/faultnoop.md", original_bytes, head_before
        )
        self.assertEqual(sorted(os.listdir(change_records_dir)), change_records_before)

        stale_request = {
            "operation": "update",
            "path": "documents/faultnoop/faultnoop.md",
            "expected_version": "0" * 64,
            "frontmatter": {"status": NOTE_FRONTMATTER["status"]},
        }
        proc = self._run_with_after_intent_kill(stale_request, root)
        self.assertEqual(proc.returncode, 2, proc)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(
            doc,
            {"outcome": "refused", "reason": "version_mismatch", "observed_sha256": created["version"]},
        )
        self._assert_end_state_untouched(
            root, "documents/faultnoop/faultnoop.md", original_bytes, head_before
        )
        self.assertEqual(sorted(os.listdir(change_records_dir)), change_records_before)


class AttachTests(PersistTestCase):
    """P4: an attachment persists with its hash recorded and matching."""

    def test_attach_persists_binary_content_with_matching_hash(self):
        import base64
        import hashlib

        content = b"\x89PNG\r\n\x1a\nsynthetic image bytes"
        request = {
            "operation": "attach",
            "path": "documents/withimg/attachments/pic.png",
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)
        self.assertEqual(proc.returncode, 0)

        full = os.path.join(self.root, "documents/withimg/attachments/pic.png")
        with open(full, "rb") as fh:
            actual = fh.read()
        self.assertEqual(actual, content)
        self.assertEqual(doc["version"], hashlib.sha256(content).hexdigest())

    def test_attach_outside_data_zones_is_refused_and_writes_nothing(self):
        """F3: no zone check exists for `attach`; `_DATA_ZONES` is defined in
        persist.py and never used. An attach to `.brain/types/custom/evil.json`
        must be refused rather than writing into the custom type definitions
        that drive validation -- that write path lets content edit the rules
        that validate content. Cover both `.brain/` and the brain root."""
        import base64

        content = base64.b64encode(b"evil bytes").decode("ascii")

        for path in (".brain/types/custom/evil.json", "evil-at-root.md"):
            with self.subTest(path=path):
                request = {
                    "operation": "attach",
                    "path": path,
                    "content_base64": content,
                }
                proc, doc = self.persist(request)
                self.assertEqual(doc["outcome"], "refused", doc)
                self.assertFalse(os.path.exists(os.path.join(self.root, path)))


class AttachGitBrainTests(PersistTestCase):
    """X1: P4/A6's "hash recorded" half, in a git brain -- the attach commit's
    Brain-Path trailer carries the same hash as the change record. No
    existing attach test runs in a git brain, so nothing in the suite
    observed the trailer amendment A6 pins the record to."""

    def test_attach_in_git_brain_leaves_change_record_and_matching_trailer(self):
        import base64
        import hashlib

        root = self.git_root()
        content = b"\x89PNG\r\n\x1a\nsynthetic image bytes"
        path = "documents/withimg/attachments/pic.png"
        request = {
            "operation": "attach",
            "path": path,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        proc, doc = self.persist(request, root=root)
        self.assertEqual(doc["outcome"], "written", doc)
        expected_hash = hashlib.sha256(content).hexdigest()
        self.assertEqual(doc["version"], expected_hash)

        change_records = os.listdir(os.path.join(root, ".brain", "journal", "change_records"))
        self.assertEqual(len(change_records), 1)

        log = subprocess.run(
            ["git", "log", "--format=%B"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn(f"Brain-Path: {path} sha256={expected_hash} prior=absent", log)


class OpenIntentTests(PersistTestCase):
    """P7: a write to a path with an open intent returns refused (open_intent)."""

    def test_write_after_killed_intent_is_refused_open_intent(self):
        path = "documents/killed/killed.md"
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc = support.run_brain(
            ["persist", "--root", self.root],
            input_data=json.dumps(request),
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="after_intent:kill"),
        )
        # The process must actually have been killed by the fault (negative
        # returncode on POSIX signals a signal death), not merely returned a
        # refusal -- this is the fault actually landing, not just an
        # asserted end state.
        self.assertLess(proc.returncode, 0, proc)
        self.assertFalse(os.path.exists(os.path.join(self.root, path)))

        proc2, doc2 = self.persist(request)
        self.assertEqual(doc2, {"outcome": "refused", "reason": "open_intent"})
        self.assertEqual(proc2.returncode, 2)

    def test_intent_record_carries_temp_path_and_backup_path(self):
        """F4: C6 step 2 lists run_id, op_key, path, expected_prior,
        intended_sha256, temp path, backup path -- the record written
        carried only the first five. Both the temp path and the backup path
        are decided before step 2 runs (before either file exists), so both
        must be computed and recorded at intent-write time. A create has no
        backup path (only update backs up); an update has both."""
        create_path = "documents/intentcreate/intentcreate.md"
        create_request = {
            "operation": "create",
            "path": create_path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc = support.run_brain(
            ["persist", "--root", self.root],
            input_data=json.dumps(create_request),
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="after_intent:kill"),
        )
        self.assertLess(proc.returncode, 0, proc)
        intent_dir = os.path.join(self.root, ".brain", "journal", "intents")
        intent_files = os.listdir(intent_dir)
        self.assertEqual(len(intent_files), 1)
        with open(os.path.join(intent_dir, intent_files[0]), "r", encoding="utf-8") as fh:
            create_intent = json.load(fh)
        self.assertIn("temp_path", create_intent)
        self.assertTrue(create_intent["temp_path"])
        self.assertIn("backup_path", create_intent)
        self.assertIsNone(create_intent["backup_path"])
        os.remove(os.path.join(intent_dir, intent_files[0]))

        update_path = "documents/intentupdate/intentupdate.md"
        created = self._create(update_path)
        update_request = {
            "operation": "update",
            "path": update_path,
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        proc = support.run_brain(
            ["persist", "--root", self.root],
            input_data=json.dumps(update_request),
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="after_intent:kill"),
        )
        self.assertLess(proc.returncode, 0, proc)
        intent_files = os.listdir(intent_dir)
        self.assertEqual(len(intent_files), 1)
        with open(os.path.join(intent_dir, intent_files[0]), "r", encoding="utf-8") as fh:
            update_intent = json.load(fh)
        self.assertIn("temp_path", update_intent)
        self.assertTrue(update_intent["temp_path"])
        self.assertIn("backup_path", update_intent)
        self.assertTrue(update_intent["backup_path"])

    def _create(self, path):
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        _, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)
        return doc


class ReviewRequiredTests(PersistTestCase):
    """P8: a non-reviewed persist of a document with origin: unknown is
    refused (review_required); the reviewed-write branch is unreachable in
    this story (A4)."""

    def test_create_with_origin_unknown_is_refused_review_required(self):
        fm = dict(NOTE_FRONTMATTER)
        fm["origin"] = "unknown"
        request = {
            "operation": "create",
            "path": "documents/unk/unk.md",
            "frontmatter": fm,
            "body": "Body.\n",
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc, {"outcome": "refused", "reason": "review_required"})
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(os.path.join(self.root, "documents/unk/unk.md")))


class UnsupportedVersionAndJournalTests(PersistTestCase):
    """P11: writes to unsupported_version documents are refused; writes are
    refused journal_missing when the journal is absent."""

    def test_update_of_unsupported_version_document_is_refused(self):
        path = "documents/future/future.md"
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write('---\nkb: 2\nid: "x"\n---\nBody.\n')
        request = {
            "operation": "update",
            "path": path,
            "expected_version": "0" * 64,
            "frontmatter": {"status": "complete"},
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc, {"outcome": "refused", "reason": "unsupported_version"})
        self.assertEqual(proc.returncode, 2)

    def test_write_refused_journal_missing_when_journal_absent(self):
        no_journal_root = tempfile.mkdtemp(prefix="brain-persist-nojournal-")
        self.addCleanup(shutil.rmtree, no_journal_root, ignore_errors=True)
        for zone in ("inbox", "raw", "documents", "wiki"):
            os.makedirs(os.path.join(no_journal_root, zone), exist_ok=True)
        request = {
            "operation": "create",
            "path": "documents/nojournal/nojournal.md",
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc, doc = self.persist(request, root=no_journal_root)
        self.assertEqual(doc, {"outcome": "refused", "reason": "journal_missing"})
        self.assertEqual(proc.returncode, 2)


class CommitTrailerTests(PersistTestCase):
    """P1/A5: in a git brain, a successful create leaves one commit whose
    final trailer paragraph declares Brain-Op, Brain-Run, Brain-Key and
    Brain-Path with the path and hash, plus Brain-Grant for an agent-authored
    document (C7, A5)."""

    def _log(self, root):
        return subprocess.run(
            ["git", "log", "--format=%B"], cwd=root, capture_output=True, text=True, check=True
        ).stdout

    def test_create_commit_carries_consistent_trailers(self):
        root = self.git_root()
        fm = dict(NOTE_FRONTMATTER)
        fm["authored_by"] = "agent"
        request = {
            "operation": "create",
            "path": "documents/withgit/withgit.md",
            "frontmatter": fm,
            "body": "Body.\n",
        }
        proc, doc = self.persist(request, root=root)
        self.assertEqual(doc["outcome"], "written", doc)

        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(count, "2")  # fixture's initial commit, then this one

        log = self._log(root)
        self.assertIn("Brain-Op: create", log)
        self.assertIn(f"Brain-Path: documents/withgit/withgit.md sha256={doc['version']} prior=absent", log)
        self.assertTrue(any(line.startswith("Brain-Run:") for line in log.splitlines()))
        self.assertTrue(any(line.startswith("Brain-Key:") for line in log.splitlines()))
        self.assertTrue(any(line.startswith("Brain-Grant:") and "maintenance=agent by=agent" in line for line in log.splitlines()))

    def test_commit_never_includes_unrelated_uncommitted_changes(self):
        """P12: a brain commit never sweeps in an unrelated uncommitted
        working-tree change."""
        root = self.git_root()
        unrelated = os.path.join(root, "documents", "unrelated.md")
        with open(unrelated, "w", encoding="utf-8") as fh:
            fh.write("Not part of this operation.\n")

        request = {
            "operation": "create",
            "path": "documents/related/related.md",
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc, doc = self.persist(request, root=root)
        self.assertEqual(doc["outcome"], "written", doc)

        committed_files = subprocess.run(
            ["git", "show", "--stat", "--format=", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("related.md", committed_files)
        self.assertNotIn("unrelated.md", committed_files)

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("unrelated.md", status)


class FaultInjectionTests(PersistTestCase):
    """P5 (reachable half only, A1): kill at after_temp and after_apply
    leaves the target at either its prior bytes or the intended bytes, never
    truncated. P6: fail at before_change_record and before_commit yields
    written_incomplete naming exactly the pending steps, and index never
    appears in the pending list (A3)."""

    def _run_with_fault(self, request, fault, root=None):
        root = root or self.root
        return support.run_brain(
            ["persist", "--root", root],
            input_data=json.dumps(request),
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT=fault),
        )

    def test_kill_after_temp_leaves_no_target_or_prior_bytes_only(self):
        path = "documents/killtemp/killtemp.md"
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc = self._run_with_fault(request, "after_temp:kill")
        self.assertLess(proc.returncode, 0, proc)  # confirms the kill landed
        full = os.path.join(self.root, path)
        # Prior state was "absent"; after_temp fires before the hard link
        # (step 5), so the target must still be absent -- never a truncated
        # partial write.
        self.assertFalse(os.path.exists(full))

    def test_kill_after_apply_leaves_intended_bytes_never_truncated(self):
        path = "documents/killapply/killapply.md"
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc = self._run_with_fault(request, "after_apply:kill")
        self.assertLess(proc.returncode, 0, proc)  # confirms the kill landed
        full = os.path.join(self.root, path)
        self.assertTrue(os.path.isfile(full))
        with open(full, "rb") as fh:
            content = fh.read()
        # after_apply fires once the hard link has already landed the full
        # intended bytes -- content must be complete, never a partial write.
        self.assertIn(b"Body.\n", content)
        self.assertIn(b"kb: 1", content)

    def test_kill_after_apply_on_update_leaves_prior_or_intended_never_truncated(self):
        create_request = {
            "operation": "create",
            "path": "documents/killupdate/killupdate.md",
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc, created = self.persist(create_request)
        self.assertEqual(created["outcome"], "written", created)
        full = os.path.join(self.root, "documents/killupdate/killupdate.md")
        with open(full, "rb") as fh:
            prior_bytes = fh.read()

        update_request = {
            "operation": "update",
            "path": "documents/killupdate/killupdate.md",
            "expected_version": created["version"],
            "frontmatter": {"status": "complete"},
        }
        proc = self._run_with_fault(update_request, "after_apply:kill")
        self.assertLess(proc.returncode, 0, proc)
        with open(full, "rb") as fh:
            after = fh.read()
        self.assertTrue(len(after) == len(prior_bytes) or b"status: \"complete\"" in after)
        self.assertNotEqual(after, b"")

    def test_fail_before_change_record_yields_written_incomplete_pending_both(self):
        path = "documents/failbcr/failbcr.md"
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc = self._run_with_fault(request, "before_change_record:fail")
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "written_incomplete", doc)
        self.assertEqual(doc["pending"], ["change_record", "commit"])
        self.assertNotIn("index", doc["pending"])
        full = os.path.join(self.root, path)
        self.assertTrue(os.path.isfile(full))

    def test_fail_before_commit_yields_written_incomplete_pending_commit_only(self):
        path = "documents/failbc/failbc.md"
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        proc = self._run_with_fault(request, "before_commit:fail")
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "written_incomplete", doc)
        self.assertEqual(doc["pending"], ["commit"])
        self.assertNotIn("index", doc["pending"])

        change_records = os.listdir(os.path.join(self.root, ".brain", "journal", "change_records"))
        self.assertEqual(len(change_records), 1)


class YamlDualParseTests(PersistTestCase):
    """P9: every document created by persist re-parses to its intended
    values under YAML 1.1 (the vendored parser persist itself uses) and
    under a YAML 1.2 core-schema reader (S-G4(b)). C4 documents exactly one
    reader difference for the canonical written form: an unquoted
    `YYYY-MM-DD` date reads as a date under 1.1 and as a string under 1.2,
    and both readings are accepted (C4's *Reading values*); every other
    value the canonical form leaves unquoted (`kb: 1`, empty scalars, `[]`)
    reads identically under both."""

    def test_created_document_reparses_under_yaml_11_and_12(self):
        fm = dict(NOTE_FRONTMATTER)
        fm["origin"] = [{"ref": "https://example.invalid/source", "retrieved": "2026-02-03"}]
        fm["evidence"] = ["https://example.invalid/evidence"]
        request = {
            "operation": "create",
            "path": "documents/dual/dual.md",
            "frontmatter": fm,
            "body": "Body text.\n",
        }
        _, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)

        full = os.path.join(self.root, "documents/dual/dual.md")
        with open(full, "r", encoding="utf-8") as fh:
            text = fh.read()
        block = _frontmatter_block(text)

        under_11 = next(iter(_yaml_module.load_all(block, Loader=_yaml_module.SafeLoader)))
        under_12 = next(iter(_yaml_module.load_all(block, Loader=_YAML12_LOADER)))

        # The one documented difference (C4): an unquoted date reads as a
        # `date` object under 1.1 and as its literal string under 1.2.
        self.assertEqual(under_11["created"], datetime.date(2026, 1, 1))
        self.assertEqual(under_12["created"], "2026-01-01")
        self.assertEqual(under_11["reviewed"], datetime.date(2026, 1, 1))
        self.assertEqual(under_12["reviewed"], "2026-01-01")
        self.assertEqual(under_11["origin"][0]["retrieved"], datetime.date(2026, 2, 3))
        self.assertEqual(under_12["origin"][0]["retrieved"], "2026-02-03")

        # Every other value the canonical form emits is quoted text, `kb: 1`,
        # or `[]` -- identical under both readers.
        for field in ("kb", "id", "type", "title", "summary", "status", "kind", "authored_by", "retention", "evidence"):
            self.assertEqual(under_11[field], under_12[field], field)
        self.assertEqual(under_11["id"], doc["id"])


class FrontmatterPreservationTests(PersistTestCase):
    """P10: a single-property update on a note with comments, hand-ordered
    properties and a zero-indented list changes only that property's span
    and leaves every other byte identical; updating a property whose span
    holds a comment, and any block using anchors or JSON style, is refused
    `unsupported_frontmatter` byte-identical (S-G4(c))."""

    HAND_WRITTEN = (
        "---\n"
        "kb: 1\n"
        "id: \"01arz3ndektsv4rrffq69g5fav\"\n"
        "type: \"note\"\n"
        "title: \"Hand note\"\n"
        "# a comment sitting between two properties\n"
        "summary: \"A hand-authored note.\"\n"
        "status: \"draft\"\n"
        "created: 2026-01-01\n"
        "reviewed: 2026-01-01\n"
        "origin: \"authored\"\n"
        "evidence:\n"
        "- \"https://example.invalid/a\"\n"
        "- \"https://example.invalid/b\"\n"
        "kind: \"decision\"\n"
        "authored_by: \"human\"\n"
        "retention: \"durable\"\n"
        "---\n"
        "Body text, hand written.\n"
    )

    def _write_hand_written(self, path):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.HAND_WRITTEN)
        import hashlib

        with open(full, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_single_property_update_changes_only_that_span(self):
        path = "documents/hand/hand.md"
        version = self._write_hand_written(path)
        full = os.path.join(self.root, path)

        request = {
            "operation": "update",
            "path": path,
            "expected_version": version,
            "frontmatter": {"status": "complete"},
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)

        with open(full, "r", encoding="utf-8", newline="") as fh:
            after = fh.read()
        before_lines = self.HAND_WRITTEN.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        self.assertEqual(len(before_lines), len(after_lines))
        changed = [i for i in range(len(before_lines)) if before_lines[i] != after_lines[i]]
        self.assertEqual(changed, [7])  # only the `status:` line
        self.assertEqual(after_lines[7], 'status: "complete"\n')
        # Comment, hand order and the zero-indented list are untouched.
        self.assertIn("# a comment sitting between two properties\n", after_lines)
        self.assertIn('- "https://example.invalid/a"\n', after_lines)
        self.assertIn('- "https://example.invalid/b"\n', after_lines)

    def test_update_of_property_whose_span_holds_comment_is_refused_byte_identical(self):
        path = "documents/handcomment/handcomment.md"
        hand_written = self.HAND_WRITTEN.replace(
            'title: "Hand note"\n',
            'title: "Hand note"  # inline comment on the title itself\n',
        ).replace(
            "# a comment sitting between two properties\n", ""
        )
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(hand_written)
        import hashlib

        with open(full, "rb") as fh:
            version = hashlib.sha256(fh.read()).hexdigest()

        request = {
            "operation": "update",
            "path": path,
            "expected_version": version,
            "frontmatter": {"title": "Renamed"},
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc, {"outcome": "refused", "reason": "unsupported_frontmatter"})
        self.assertEqual(proc.returncode, 2)
        with open(full, "r", encoding="utf-8", newline="") as fh:
            after = fh.read()
        self.assertEqual(after, hand_written)

    def test_update_of_anchor_using_block_is_refused_byte_identical(self):
        path = "documents/handanchor/handanchor.md"
        # An anchor with no alias reference elsewhere: isolates anchor
        # detection specifically (C4's list -- anchors, aliases, explicit
        # tags, multiple documents, flow style -- names each independently,
        # so a fixture combining an anchor with its alias would leave the
        # anchor-only case unobserved).
        hand_written = (
            "---\n"
            "kb: 1\n"
            "id: \"01arz3ndektsv4rrffq69g5fav\"\n"
            "type: \"note\"\n"
            "title: &t \"Hand note\"\n"
            "summary: \"A hand-authored note.\"\n"
            "status: \"draft\"\n"
            "created: 2026-01-01\n"
            "reviewed: 2026-01-01\n"
            "origin: \"authored\"\n"
            "evidence: []\n"
            "kind: \"decision\"\n"
            "authored_by: \"human\"\n"
            "retention: \"durable\"\n"
            "---\n"
            "Body text, hand written.\n"
        )
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(hand_written)
        import hashlib

        with open(full, "rb") as fh:
            version = hashlib.sha256(fh.read()).hexdigest()

        request = {
            "operation": "update",
            "path": path,
            "expected_version": version,
            "frontmatter": {"status": "complete"},
        }
        proc, doc = self.persist(request)
        self.assertEqual(doc, {"outcome": "refused", "reason": "unsupported_frontmatter"})
        self.assertEqual(proc.returncode, 2)
        with open(full, "r", encoding="utf-8", newline="") as fh:
            after = fh.read()
        self.assertEqual(after, hand_written)


class MintedIdIsUlidTests(PersistTestCase):
    """X4: the id persist mints is a real ULID (C4) -- a 48-bit millisecond
    timestamp reflecting the time of minting, followed by 80 bits of
    randomness, with the first character bounded so the encoding stays
    within 128 bits -- not merely 26 characters drawn from the Crockford
    alphabet. Expected values are grounded independently: this test decodes
    the id's own bits with a decoder it writes itself (plain base32 place
    value, never calling into brain_core), and compares the decoded
    timestamp against a wall-clock window captured around the mint."""

    _CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"

    @classmethod
    def _decode_timestamp_ms(cls, ulid):
        value = 0
        for ch in ulid:
            value = (value << 5) | cls._CROCKFORD.index(ch)
        return value >> 80  # the low 80 bits are randomness; the rest is the 48-bit timestamp

    def _create(self, path):
        request = {
            "operation": "create",
            "path": path,
            "frontmatter": dict(NOTE_FRONTMATTER),
            "body": "Body.\n",
        }
        _, doc = self.persist(request)
        self.assertEqual(doc["outcome"], "written", doc)
        return doc["id"]

    def test_minted_id_timestamp_reflects_time_of_minting(self):
        import time

        before_ms = int(time.time() * 1000)
        minted = self._create("documents/ulidtime/ulidtime.md")
        after_ms = int(time.time() * 1000)

        timestamp = self._decode_timestamp_ms(minted)
        # Generous slack for clock skew and subprocess start-up, but still a
        # world away from a uniformly random 48-bit value (which would land
        # outside this window with near certainty).
        self.assertGreaterEqual(timestamp, before_ms - 1000, minted)
        self.assertLessEqual(timestamp, after_ms + 1000, minted)

    def test_minted_id_first_character_stays_within_128_bit_bound(self):
        minted = self._create("documents/ulidbound/ulidbound.md")
        # 26 base32 characters hold 130 bits; a real ULID's 128-bit value
        # forces the first character's top two bits to zero, so it is one of
        # the alphabet's first 8 symbols only.
        self.assertIn(minted[0], "01234567", minted)

    def test_minted_ids_sort_in_minting_order(self):
        import time

        first = self._create("documents/ulidseq/first.md")
        time.sleep(0.005)
        second = self._create("documents/ulidseq/second.md")
        self.assertLess(first, second)


if __name__ == "__main__":
    unittest.main()
