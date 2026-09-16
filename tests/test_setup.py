"""Tests for `brain setup` (ST-04), against docs/specification/beta.md C3/C6/C7."""

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import support

SYNTHETIC_SKILL_FILES = {
    "skills/find-knowledge/SKILL.md": "# Find knowledge\n\nSynthetic fixture skill for setup tests.\n",
}

SYNTHETIC_MODULE_FILES = {
    "modules/projects/module.json": json.dumps({"module": "projects", "folder": "projects"}) + "\n",
    "modules/projects/types/project.json": json.dumps(
        {
            "type": "project",
            "zones": ["projects"],
            "required": ["project_status"],
            "allowed_values": {"project_status": ["active", "paused", "closed"]},
            "uncertainty_values": {},
            "display_fields": ["project_status", "summary"],
            "search_fields": ["title", "summary"],
        }
    )
    + "\n",
    "modules/projects/types/decision.json": json.dumps(
        {
            "type": "decision",
            "zones": ["projects"],
            "required": ["decision_status"],
            "allowed_values": {"decision_status": ["proposed", "accepted", "superseded"]},
            "uncertainty_values": {"decided": None},
            "display_fields": ["decision_status", "decided"],
            "search_fields": ["title", "summary"],
        }
    )
    + "\n",
}


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


def build_starter(root, skill_files=None, module_files=None, with_origin=False):
    """Builds a clean, committed synthetic starter checkout at `root`: the real
    repo's code/types/vendor plus synthetic skill and module payloads."""
    for name in ("bin", "brain_core", "types", "vendor", "LICENSE"):
        src = os.path.join(support.REPO_ROOT, name)
        dst = os.path.join(root, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)

    for rel, content in (skill_files if skill_files is not None else SYNTHETIC_SKILL_FILES).items():
        support.write_file(root, rel, content)
    for rel, content in (module_files if module_files is not None else SYNTHETIC_MODULE_FILES).items():
        support.write_file(root, rel, content)

    _run_git(["init", "-q"], root)
    _run_git(["add", "-A"], root)
    _run_git(["commit", "-q", "-m", "synthetic starter fixture"], root)
    if with_origin:
        origin_path = tempfile.mkdtemp(prefix="brain-origin-")
        _run_git(["init", "-q", "--bare", origin_path], root)
        _run_git(["remote", "add", "origin", origin_path], root)
    return root


class SetupTestCase(unittest.TestCase):
    def setUp(self):
        self.starter = tempfile.mkdtemp(prefix="brain-starter-")
        self.addCleanup(shutil.rmtree, self.starter, ignore_errors=True)
        self.target_parent = tempfile.mkdtemp(prefix="brain-target-")
        self.addCleanup(shutil.rmtree, self.target_parent, ignore_errors=True)
        build_starter(self.starter)

    def target(self, name="brain"):
        return os.path.join(self.target_parent, name)

    def setup_cmd(self, target, extra=(), env=None):
        args = ["setup", "--starter", self.starter, "--target", target] + list(extra)
        return support.run_brain(args, env=env)


class RefusalTests(SetupTestCase):
    """A1: setup refuses before any target mutation on a bad request (spec Behaviour 6, C3.1)."""

    def test_refuses_nonempty_target(self):
        target = self.target()
        support.write_file(target, "existing.txt", "hi\n")
        proc = self.setup_cmd(target)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "refused")
        self.assertEqual(doc["reason"], "target_not_empty")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(os.listdir(target), ["existing.txt"])

    def test_refuses_target_nested_in_another_repo(self):
        nested = os.path.join(self.starter, "nested-brain")
        proc = self.setup_cmd(nested)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "refused")
        self.assertEqual(doc["reason"], "target_nested_in_repository")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(nested))

    def test_refuses_dirty_starter(self):
        support.write_file(self.starter, "bin/brain", "dirty\n")
        target = self.target()
        proc = self.setup_cmd(target)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "refused")
        self.assertEqual(doc["reason"], "starter_dirty")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(target))


def _hash_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class BasicSetupTests(SetupTestCase):
    """A1/A2/A7/A8: layout, dual skill copy, code/types/vendor relocation,
    setup.json record, no .git without --git (spec C2, C3.2-3, Behaviour 5)."""

    def test_setup_complete_copies_layout_and_records_hashes(self):
        target = self.target()
        proc = self.setup_cmd(target)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})
        self.assertEqual(proc.returncode, 0)

        for zone in ("inbox", "raw", "documents", "wiki"):
            self.assertTrue(os.path.isdir(os.path.join(target, zone)), zone)

        # Dual skill copy, byte-identical to the committed fixture source.
        skill_rel = "skills/find-knowledge/SKILL.md"
        with open(os.path.join(self.starter, skill_rel), "rb") as fh:
            expected_bytes = fh.read()
        for skill_root in (".claude/skills", ".agents/skills"):
            copy_path = os.path.join(target, skill_root, "find-knowledge/SKILL.md")
            self.assertTrue(os.path.isfile(copy_path), copy_path)
            with open(copy_path, "rb") as fh:
                self.assertEqual(fh.read(), expected_bytes)

        # Code and base types relocated under .brain/, independent hash check.
        for rel in ("bin/brain", "brain_core/setup.py", "types/base/note.json", "vendor/VENDORED.md"):
            src = os.path.join(self.starter, rel)
            dst = os.path.join(target, ".brain", rel)
            self.assertTrue(os.path.isfile(dst), dst)
            self.assertEqual(_hash_file(dst), _hash_file(src), rel)

        self.assertFalse(os.path.exists(os.path.join(target, ".git")))

        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["commit"], self._starter_head())
        self.assertEqual(record["modules"], [])
        self.assertTrue(record["brain_id"])
        self.assertTrue(record["created"])

        copied_by_path = {entry["path"]: entry["sha256"] for entry in record["copied"]}
        for rel, brain_path in (
            (skill_rel, ".claude/skills/find-knowledge/SKILL.md"),
            (skill_rel, ".agents/skills/find-knowledge/SKILL.md"),
            ("bin/brain", ".brain/bin/brain"),
            ("types/base/note.json", ".brain/types/base/note.json"),
        ):
            self.assertIn(brain_path, copied_by_path, brain_path)
            self.assertEqual(copied_by_path[brain_path], _hash_file(os.path.join(self.starter, rel)))

    def test_tampered_copy_mismatches_recorded_hash(self):
        """Mutation: tampering a copied file must break the independent hash
        check above, proving the assertion is sensitive."""
        target = self.target()
        self.setup_cmd(target)
        tampered = os.path.join(target, ".brain", "bin", "brain")
        with open(tampered, "a", encoding="utf-8") as fh:
            fh.write("\n# tampered\n")
        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        recorded = next(e["sha256"] for e in record["copied"] if e["path"] == ".brain/bin/brain")
        self.assertNotEqual(_hash_file(tampered), recorded)

    def _starter_head(self):
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.starter, capture_output=True, text=True, check=True
        )
        return proc.stdout.strip()


class ModuleInstallTests(SetupTestCase):
    """A1/A2 module scope: --module projects installs a nonempty synthetic
    payload's manifest/types, creates the declared folder, and records hashes;
    a requested-but-absent module refuses before any target mutation."""

    def test_installs_selected_module(self):
        target = self.target()
        proc = self.setup_cmd(target, extra=["--module", "projects"])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})

        manifest_dst = os.path.join(target, ".brain", "modules", "projects", "module.json")
        manifest_src = os.path.join(self.starter, "modules", "projects", "module.json")
        self.assertEqual(_hash_file(manifest_dst), _hash_file(manifest_src))

        for type_name in ("project", "decision"):
            dst = os.path.join(target, ".brain", "modules", "projects", "types", f"{type_name}.json")
            src = os.path.join(self.starter, "modules", "projects", "types", f"{type_name}.json")
            self.assertEqual(_hash_file(dst), _hash_file(src), type_name)

        self.assertTrue(os.path.isdir(os.path.join(target, "projects")))

        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        self.assertEqual(record["modules"], ["projects"])

    def test_second_synthetic_payload_installs_its_own_bytes(self):
        """Different nonempty synthetic payload bytes copy correctly and
        produce different hashes than the first fixture (no shared literal)."""
        alt_module_files = {
            "modules/projects/module.json": json.dumps({"module": "projects", "folder": "projects"}) + "\n",
            "modules/projects/types/project.json": json.dumps(
                {
                    "type": "project",
                    "zones": ["projects"],
                    "required": ["project_status"],
                    "allowed_values": {"project_status": ["active", "paused", "closed"]},
                    "uncertainty_values": {},
                    "display_fields": ["project_status"],
                    "search_fields": ["title"],
                }
            )
            + "\n",
        }
        alt_starter = tempfile.mkdtemp(prefix="brain-starter-alt-")
        self.addCleanup(shutil.rmtree, alt_starter, ignore_errors=True)
        build_starter(alt_starter, module_files=alt_module_files)

        target = self.target("alt")
        proc = support.run_brain(["setup", "--starter", alt_starter, "--target", target, "--module", "projects"])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})

        dst = os.path.join(target, ".brain", "modules", "projects", "types", "project.json")
        src = os.path.join(alt_starter, "modules", "projects", "types", "project.json")
        self.assertEqual(_hash_file(dst), _hash_file(src))
        self.assertNotEqual(
            _hash_file(dst),
            hashlib.sha256(SYNTHETIC_MODULE_FILES["modules/projects/types/project.json"].encode("utf-8")).hexdigest(),
        )

    def test_refuses_missing_module_before_any_target_mutation(self):
        target = self.target()
        proc = self.setup_cmd(target, extra=["--module", "nonexistent"])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "refused")
        self.assertEqual(doc["reason"], "module_not_found")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(target))


class GitTests(SetupTestCase):
    """A3/A8: --git makes a local repo with one commit carrying both
    Brain-Op: setup and a nonempty generated Brain-Run id (spec C3.4, C7);
    omitted, no .git is created (spec C3, Behaviour 3)."""

    def test_git_flag_creates_repo_with_setup_trailer(self):
        target = self.target()
        proc = self.setup_cmd(target, extra=["--git"])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})

        toplevel = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=target, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(os.path.realpath(toplevel), os.path.realpath(target))

        remotes = subprocess.run(
            ["git", "remote"], cwd=target, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(remotes, "")

        log = subprocess.run(
            ["git", "log", "--format=%B"], cwd=target, capture_output=True, text=True, check=True
        ).stdout
        commit_count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=target, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(commit_count, "1")
        self.assertIn("Brain-Op: setup", log)

        run_ids = [
            line.split("Brain-Run:", 1)[1].strip()
            for line in log.splitlines()
            if line.startswith("Brain-Run:")
        ]
        self.assertEqual(len(run_ids), 1)
        self.assertTrue(run_ids[0])

        tracked = set(
            subprocess.run(
                ["git", "ls-files"], cwd=target, capture_output=True, text=True, check=True
            ).stdout.splitlines()
        )
        self.assertNotIn(".brain/journal/epoch.json", tracked)
        self.assertIn(".brain/setup.json", tracked)
        self.assertIn(".claude/skills/find-knowledge/SKILL.md", tracked)

    def test_without_git_flag_no_repository_created(self):
        target = self.target()
        proc = self.setup_cmd(target)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})
        self.assertFalse(os.path.exists(os.path.join(target, ".git")))


class SmokeCheckTests(SetupTestCase):
    """A5/A6: real hard-link create+refuse in data and state zones, gated
    fault injection at both named check points, gate-off no-op, and the
    empty brain validating through the copied runtime (spec C3.5, C6)."""

    def test_gated_hardlink_data_fault_fails_setup(self):
        target = self.target()
        proc = self.setup_cmd(
            target,
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="setup_hardlink_data:fail"),
        )
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "setup_failed")
        self.assertEqual(doc["check"], "hardlink_data")
        self.assertEqual(proc.returncode, 3)

    def test_gated_hardlink_state_fault_fails_setup(self):
        target = self.target()
        proc = self.setup_cmd(
            target,
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="setup_hardlink_state:fail"),
        )
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "setup_failed")
        self.assertEqual(doc["check"], "hardlink_state")
        self.assertEqual(proc.returncode, 3)

    def test_ungated_fault_value_has_no_effect(self):
        target = self.target()
        proc = self.setup_cmd(target, env=dict(os.environ, BRAIN_FAULT="setup_hardlink_data:fail"))
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})

    def test_ordinary_setup_exercises_real_hardlink_success_in_both_zones(self):
        target = self.target()
        proc = self.setup_cmd(target)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})
        # Smoke probes are cleaned up; no leftover artifacts under either zone.
        self.assertEqual(os.listdir(os.path.join(target, "documents")), [])
        self.assertFalse(os.path.exists(os.path.join(target, ".brain", "state", ".setup-smoke")))

    def test_empty_brain_validates_through_copied_runtime(self):
        target = self.target()
        self.setup_cmd(target)
        proc = subprocess.run(
            [
                "python3",
                "-B",
                "-S",
                "-E",
                os.path.join(target, ".brain", "bin", "brain"),
                "validate",
                "--root",
                target,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc["outcome"], "valid")
        self.assertEqual(proc.returncode, 0)


class JournalAndVendorTests(SetupTestCase):
    """A7: journal epoch names the setup brain id; vendored licence copied
    and hashed (spec C2, C1 vendored-dependency record)."""

    def test_journal_epoch_names_setup_brain_id(self):
        target = self.target()
        self.setup_cmd(target)
        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            setup_record = json.load(fh)
        with open(os.path.join(target, ".brain", "journal", "epoch.json"), encoding="utf-8") as fh:
            epoch = json.load(fh)
        self.assertEqual(epoch["brain_id"], setup_record["brain_id"])

    def test_vendor_licence_copied_and_hashed(self):
        target = self.target()
        self.setup_cmd(target)
        dst = os.path.join(target, ".brain", "vendor", "PyYAML-LICENSE")
        src = os.path.join(self.starter, "vendor", "PyYAML-LICENSE")
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(_hash_file(dst), _hash_file(src))
        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        paths = {e["path"] for e in record["copied"]}
        self.assertIn(".brain/vendor/PyYAML-LICENSE", paths)


class LayoutCompletenessTests(SetupTestCase):
    """A1/C2: every C2 folder exists even when the starter carries
    no skill or module payload for it, and a custom type added afterward is
    tracked under --git while journal/state stay ignored."""

    def test_skill_and_custom_type_folders_exist_without_any_payload(self):
        empty_starter = tempfile.mkdtemp(prefix="brain-starter-empty-")
        self.addCleanup(shutil.rmtree, empty_starter, ignore_errors=True)
        build_starter(empty_starter, skill_files={}, module_files={})

        target = self.target("empty")
        proc = support.run_brain(["setup", "--starter", empty_starter, "--target", target, "--git"])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})

        for rel in (".claude/skills", ".agents/skills", ".brain/types/custom", "projects"):
            path = os.path.join(target, rel)
            self.assertTrue(os.path.isdir(path), rel)
            self.assertEqual(os.listdir(path), [], rel)

        support.write_file(target, ".brain/types/custom/recipe.json", '{"type": "recipe"}\n')
        subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "add custom type"],
            cwd=target,
            check=True,
            capture_output=True,
            text=True,
            env=dict(
                os.environ,
                GIT_AUTHOR_NAME="t",
                GIT_AUTHOR_EMAIL="t@example.invalid",
                GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@example.invalid",
            ),
        )
        tracked = set(
            subprocess.run(
                ["git", "ls-files"], cwd=target, capture_output=True, text=True, check=True
            ).stdout.splitlines()
        )
        self.assertIn(".brain/types/custom/recipe.json", tracked)
        self.assertNotIn(".brain/journal/epoch.json", tracked)


class StarterUrlTests(SetupTestCase):
    """G2/A2: first configured origin URL wins, with no insteadOf rewriting;
    other remotes are ignored; absent origin falls back to an escaped
    canonical file: URI (spec C3)."""

    def _setup_json(self, target):
        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_single_origin_url_used_verbatim(self):
        starter = tempfile.mkdtemp(prefix="brain-starter-origin-")
        self.addCleanup(shutil.rmtree, starter, ignore_errors=True)
        build_starter(starter)
        _run_git(["remote", "add", "origin", "https://example.invalid/brain.git"], starter)

        target = self.target("origin")
        support.run_brain(["setup", "--starter", starter, "--target", target])
        self.assertEqual(self._setup_json(target)["starter_url"], "https://example.invalid/brain.git")

    def test_first_of_multiple_origin_urls_wins(self):
        starter = tempfile.mkdtemp(prefix="brain-starter-multi-origin-")
        self.addCleanup(shutil.rmtree, starter, ignore_errors=True)
        build_starter(starter)
        _run_git(["remote", "add", "origin", "https://example.invalid/first.git"], starter)
        _run_git(["remote", "set-url", "--add", "origin", "https://example.invalid/second.git"], starter)

        target = self.target("multi-origin")
        support.run_brain(["setup", "--starter", starter, "--target", target])
        self.assertEqual(self._setup_json(target)["starter_url"], "https://example.invalid/first.git")

    def test_other_named_remote_does_not_affect_fallback(self):
        starter = tempfile.mkdtemp(prefix="brain-starter-upstream-")
        self.addCleanup(shutil.rmtree, starter, ignore_errors=True)
        build_starter(starter)
        _run_git(["remote", "add", "upstream", "https://example.invalid/upstream.git"], starter)

        target = self.target("upstream-only")
        support.run_brain(["setup", "--starter", starter, "--target", target])
        self.assertEqual(self._setup_json(target)["starter_url"], pathlib.Path(os.path.realpath(starter)).as_uri())

    def test_insteadof_rewriting_is_not_applied(self):
        """`git remote get-url` would apply a configured insteadOf rewrite;
        the recorded starter_url must stay the literal configured value."""
        starter = tempfile.mkdtemp(prefix="brain-starter-insteadof-")
        self.addCleanup(shutil.rmtree, starter, ignore_errors=True)
        build_starter(starter)
        _run_git(["remote", "add", "origin", "https://example.invalid/first.git"], starter)
        _run_git(
            ["config", "url.ssh://git@rewritten.invalid/.insteadOf", "https://example.invalid/"],
            starter,
        )
        rewritten = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=starter, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertNotEqual(rewritten, "https://example.invalid/first.git", "fixture did not exercise insteadOf")

        target = self.target("insteadof")
        support.run_brain(["setup", "--starter", starter, "--target", target])
        self.assertEqual(self._setup_json(target)["starter_url"], "https://example.invalid/first.git")

    def test_absent_origin_falls_back_to_escaped_file_uri(self):
        starter_parent = tempfile.mkdtemp(prefix="brain-starter-space-")
        self.addCleanup(shutil.rmtree, starter_parent, ignore_errors=True)
        starter = os.path.join(starter_parent, "with space #tag")
        os.makedirs(starter)
        build_starter(starter)

        target = self.target("no-origin")
        support.run_brain(["setup", "--starter", starter, "--target", target])
        self.assertEqual(self._setup_json(target)["starter_url"], pathlib.Path(os.path.realpath(starter)).as_uri())


class VersionSmokeCheckTests(SetupTestCase):
    """A8: C3.5's supported Python/git version checks run and pass in this
    real, supported environment; see VersionCheckSensitivityTests below for
    the failing-branch coverage via a disposable-copy mutation."""

    def test_current_environment_passes_both_version_checks(self):
        target = self.target()
        proc = self.setup_cmd(target)
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})


# Test-side only: launches the unmodified bin/brain CLI in its own real
# subprocess (the agreed Seam 1), with an os.link audit observer installed
# by the subprocess's own bootstrap code — not by patching or importing
# brain_core internals, and not part of any product module.
_AUDIT_WRAPPER = """
import json, runpy, sys
_events = []
def _hook(event, args):
    if event == "os.link":
        _events.append((str(args[0]), str(args[1])))
sys.addaudithook(_hook)
sys.argv = {argv!r}
try:
    runpy.run_path({brain_path!r}, run_name="__main__")
except SystemExit as exc:
    _rc = exc.code or 0
else:
    _rc = 0
sys.stderr.write("AUDIT-LINK-EVENTS:" + json.dumps(_events) + "\\n")
sys.exit(_rc)
"""


def _run_brain_with_link_audit(args):
    script = _AUDIT_WRAPPER.format(argv=[support.BRAIN] + list(args), brain_path=support.BRAIN)
    proc = subprocess.run(
        [sys.executable, "-B", "-S", "-E", "-c", script],
        cwd=support.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    events = []
    for line in proc.stderr.splitlines():
        if line.startswith("AUDIT-LINK-EVENTS:"):
            events = json.loads(line[len("AUDIT-LINK-EVENTS:") :])
    return proc, events


class HardlinkObservationTests(SetupTestCase):
    """A5: the CLI itself performs real os.link calls at both smoke-check
    locations — one create plus one collision retry each — run as a genuine
    subprocess (spec Seam 1) with a test-side audit observer, no mocking, no
    new product API."""

    def test_real_hardlink_calls_observed_at_both_locations(self):
        target = self.target()
        proc, events = _run_brain_with_link_audit(
            ["setup", "--starter", self.starter, "--target", target]
        )
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})
        self.assertEqual(proc.returncode, 0)

        data_probe = os.path.join(target, "documents", ".setup-smoke")
        state_probe = os.path.join(target, ".brain", "state", ".setup-smoke")
        data_events = [e for e in events if e[1].startswith(data_probe)]
        state_events = [e for e in events if e[1].startswith(state_probe)]
        self.assertEqual(len(data_events), 2, events)
        self.assertEqual(len(state_events), 2, events)
        for src, dst in data_events + state_events:
            self.assertTrue(src.endswith(os.path.join(".setup-smoke", "source")), src)
            self.assertTrue(dst.endswith(os.path.join(".setup-smoke", "link")), dst)
            self.assertEqual(os.path.dirname(src), os.path.dirname(dst))


class ModuleFolderTests(unittest.TestCase):
    """C15/C3 (owner-approved amendment): a module manifest naming a
    reserved, hidden, multi-component or parent-escaping folder is refused
    before any mutation, using the same admissible-folder rules as module
    detection (brain_core.typedefs._valid_module_folder). Uses only the
    committed `projects` fixture, with its own `folder` mutated per case;
    starter, target and the escape target all live under one owned
    TemporaryDirectory so cleanup only ever touches fixture-owned paths."""

    def _starter_with_projects_folder(self, owned_dir, folder):
        starter = os.path.join(owned_dir, "starter")
        os.makedirs(starter)
        module_files = dict(SYNTHETIC_MODULE_FILES)
        module_files["modules/projects/module.json"] = json.dumps({"module": "projects", "folder": folder}) + "\n"
        build_starter(starter, module_files=module_files)
        return starter

    def test_refuses_parent_escaping_folder_absent_target(self):
        with tempfile.TemporaryDirectory(prefix="brain-modulefolder-") as owned:
            starter = self._starter_with_projects_folder(owned, "../escaped-here")
            target = os.path.join(owned, "target-parent", "brain")
            escape_target = os.path.join(os.path.dirname(target), os.pardir, "escaped-here")

            proc = support.run_brain(["setup", "--starter", starter, "--target", target, "--module", "projects"])
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "refused", "reason": "invalid_module_folder"})
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(target))
            self.assertFalse(os.path.exists(escape_target))

    def test_refuses_parent_escaping_folder_existing_empty_target(self):
        with tempfile.TemporaryDirectory(prefix="brain-modulefolder-") as owned:
            starter = self._starter_with_projects_folder(owned, "../escaped-here")
            target_parent = os.path.join(owned, "target-parent")
            target = os.path.join(target_parent, "brain")
            os.makedirs(target)
            escape_target = os.path.join(target_parent, os.pardir, "escaped-here")
            before = support.list_paths(target_parent)

            proc = support.run_brain(["setup", "--starter", starter, "--target", target, "--module", "projects"])
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "refused", "reason": "invalid_module_folder"})
            self.assertFalse(os.path.exists(escape_target))
            self.assertEqual(support.list_paths(target_parent), before)

    def test_refuses_hidden_and_reserved_folder_names(self):
        for bad_folder in (".hidden", "documents", "wiki"):
            with self.subTest(folder=bad_folder):
                with tempfile.TemporaryDirectory(prefix="brain-modulefolder-") as owned:
                    starter = self._starter_with_projects_folder(owned, bad_folder)
                    target = os.path.join(owned, "target-parent", "brain")
                    proc = support.run_brain(
                        ["setup", "--starter", starter, "--target", target, "--module", "projects"]
                    )
                    doc = support.parse_single_json(proc.stdout)
                    self.assertEqual(doc, {"outcome": "refused", "reason": "invalid_module_folder"})
                    self.assertFalse(os.path.exists(target))

    def test_refuses_missing_folder_key(self):
        with tempfile.TemporaryDirectory(prefix="brain-modulefolder-") as owned:
            starter = os.path.join(owned, "starter")
            os.makedirs(starter)
            module_files = dict(SYNTHETIC_MODULE_FILES)
            module_files["modules/projects/module.json"] = json.dumps({"module": "projects"}) + "\n"
            build_starter(starter, module_files=module_files)
            target = os.path.join(owned, "target-parent", "brain")

            proc = support.run_brain(["setup", "--starter", starter, "--target", target, "--module", "projects"])
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "refused", "reason": "invalid_module_folder"})
            self.assertFalse(os.path.exists(target))

    def test_valid_projects_module_still_installs(self):
        with tempfile.TemporaryDirectory(prefix="brain-modulefolder-") as owned:
            starter = self._starter_with_projects_folder(owned, "projects")
            target = os.path.join(owned, "target-parent", "brain")
            proc = support.run_brain(["setup", "--starter", starter, "--target", target, "--module", "projects"])
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "setup_complete"})
            self.assertTrue(os.path.isdir(os.path.join(target, "projects")))


class MalformedModuleManifestTests(unittest.TestCase):
    """C1 (docs/specification/beta.md:163-164): a selected module's
    `module.json` that is not valid JSON must not escape the C1 output
    envelope. Uses only the committed `projects` fixture, with
    its manifest file replaced by malformed bytes; starter and target live
    under one owned TemporaryDirectory so cleanup only ever touches
    fixture-owned paths."""

    def _starter_with_malformed_manifest(self, owned_dir):
        starter = os.path.join(owned_dir, "starter")
        os.makedirs(starter)
        module_files = dict(SYNTHETIC_MODULE_FILES)
        module_files["modules/projects/module.json"] = "not json at all {"
        build_starter(starter, module_files=module_files)
        return starter

    def test_malformed_selected_module_manifest_returns_single_error_json(self):
        with tempfile.TemporaryDirectory(prefix="brain-malformedmanifest-") as owned:
            starter = self._starter_with_malformed_manifest(owned)
            target = os.path.join(owned, "target-parent", "brain")

            proc = support.run_brain(["setup", "--starter", starter, "--target", target, "--module", "projects"])
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "error"})
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(os.path.exists(target))


class NonUtf8ModuleManifestTests(unittest.TestCase):
    """C1 (docs/specification/beta.md:163-164): a selected module's
    `module.json` that cannot be decoded as UTF-8 must not escape the C1
    output envelope, the same rule as the malformed-JSON case above. Uses
    only the committed `projects` fixture, with its manifest file replaced
    by non-UTF-8 bytes; starter and target live under one owned
    TemporaryDirectory so cleanup only ever touches fixture-owned paths."""

    def _starter_with_binary_manifest(self, owned_dir):
        starter = os.path.join(owned_dir, "starter")
        os.makedirs(starter)
        build_starter(starter)
        support.write_file(starter, "modules/projects/module.json", b"\xff\xfe{bad", binary=True)
        _run_git(["add", "-A"], starter)
        _run_git(["commit", "-q", "--amend", "-m", "synthetic starter fixture"], starter)
        return starter

    def test_non_utf8_selected_module_manifest_returns_single_error_json(self):
        with tempfile.TemporaryDirectory(prefix="brain-nonutf8manifest-") as owned:
            starter = self._starter_with_binary_manifest(owned)
            target = os.path.join(owned, "target-parent", "brain")

            proc = support.run_brain(["setup", "--starter", starter, "--target", target, "--module", "projects"])
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "error"})
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(os.path.exists(target))


class NonObjectModuleManifestTests(unittest.TestCase):
    """C1 (docs/specification/beta.md:163-164): a selected module's
    `module.json` that is valid UTF-8 and valid JSON but not a JSON object
    (a list, string, null or number) must not escape the C1 output
    envelope, the same rule as the malformed-JSON and non-UTF-8 cases
    above. Uses only the committed `projects` fixture, with its manifest
    file replaced by non-object JSON content; starter and target live
    under one owned TemporaryDirectory so cleanup only ever touches
    fixture-owned paths."""

    def _starter_with_manifest_content(self, owned_dir, content):
        starter = os.path.join(owned_dir, "starter")
        os.makedirs(starter)
        module_files = dict(SYNTHETIC_MODULE_FILES)
        module_files["modules/projects/module.json"] = content
        build_starter(starter, module_files=module_files)
        return starter

    def test_non_object_json_manifests_return_single_error_json(self):
        for content in ("[1, 2, 3]", '"just a string"', "null", "42"):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory(prefix="brain-nonobjectmanifest-") as owned:
                    starter = self._starter_with_manifest_content(owned, content)
                    target = os.path.join(owned, "target-parent", "brain")

                    proc = support.run_brain(
                        ["setup", "--starter", starter, "--target", target, "--module", "projects"]
                    )
                    doc = support.parse_single_json(proc.stdout)
                    self.assertEqual(doc, {"outcome": "error"})
                    self.assertEqual(proc.returncode, 1)
                    self.assertFalse(os.path.exists(target))


class SetupModuleLoadingBoundaryTests(unittest.TestCase):
    """C1 (docs/specification/beta.md:163-164): a structurally broken
    installation -- one missing `brain_core/` or unable to load it -- must
    not escape the setup-scoped C1 output envelope. Builds disposable
    copies of the real runtime tree (`bin/`, `brain_core/`, `types/`,
    `vendor/`) in a fixture-owned temporary directory outside every
    repository, cleaned up by that fixture; never mutates this checkout."""

    def _disposable_runtime(self, owned_dir, include_brain_core, break_import=False):
        runtime = os.path.join(owned_dir, "runtime")
        os.makedirs(runtime)
        names = ["bin", "vendor"]
        if include_brain_core:
            names.append("brain_core")
        if os.path.isdir(os.path.join(support.REPO_ROOT, "types")):
            names.append("types")
        for name in names:
            shutil.copytree(
                os.path.join(support.REPO_ROOT, name),
                os.path.join(runtime, name),
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        if break_import:
            setup_path = os.path.join(runtime, "brain_core", "setup.py")
            with open(setup_path, encoding="utf-8") as fh:
                original = fh.read()
            with open(setup_path, "w", encoding="utf-8") as fh:
                fh.write('raise RuntimeError("disposable fixture: unloadable module")\n' + original)
        return runtime

    def _run_disposable_setup(self, runtime):
        brain_cli = os.path.join(runtime, "bin", "brain")
        return subprocess.run(
            [
                sys.executable, "-B", "-S", "-E", brain_cli,
                "setup", "--starter", "/nonexistent", "--target", os.path.join(runtime, "brain-target"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_missing_brain_core_stays_in_output_envelope(self):
        with tempfile.TemporaryDirectory(prefix="brain-disposable-runtime-") as owned:
            runtime = self._disposable_runtime(owned, include_brain_core=False)
            proc = self._run_disposable_setup(runtime)
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "error"})
            self.assertEqual(proc.returncode, 1)

    def test_unloadable_brain_core_stays_in_output_envelope(self):
        with tempfile.TemporaryDirectory(prefix="brain-disposable-runtime-") as owned:
            runtime = self._disposable_runtime(owned, include_brain_core=True, break_import=True)
            proc = self._run_disposable_setup(runtime)
            doc = support.parse_single_json(proc.stdout)
            self.assertEqual(doc, {"outcome": "error"})
            self.assertEqual(proc.returncode, 1)


class TrackedContentOnlyTests(SetupTestCase):
    """A2/C2: only content tracked at the starter's pinned HEAD is copied and
    recorded — an ignored, untracked file must never appear in the brain or
    the setup record, even though it doesn't make the starter "dirty"
    (spec C2's system-shipped/tracked-source row, Behaviour 1-2)."""

    def test_gitignored_untracked_file_is_never_copied(self):
        starter = tempfile.mkdtemp(prefix="brain-starter-ignored-")
        self.addCleanup(shutil.rmtree, starter, ignore_errors=True)
        build_starter(starter)
        support.write_file(starter, ".gitignore", "skills/*/IGNORED.md\n")
        _run_git(["add", ".gitignore"], starter)
        _run_git(["commit", "-q", "-m", "ignore rule"], starter)
        support.write_file(starter, "skills/find-knowledge/IGNORED.md", "UNCOMMITTED-IGNORED-BYTES\n")
        # An ignored file does not make the starter dirty.
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=starter, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(status.strip(), "")

        target = self.target()
        proc = support.run_brain(["setup", "--starter", starter, "--target", target])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_complete"})

        for skill_root in (".claude/skills", ".agents/skills"):
            self.assertFalse(
                os.path.exists(os.path.join(target, skill_root, "find-knowledge", "IGNORED.md"))
            )
        with open(os.path.join(target, ".brain", "setup.json"), encoding="utf-8") as fh:
            record = json.load(fh)
        paths = {e["path"] for e in record["copied"]}
        self.assertFalse(any("IGNORED.md" in p for p in paths), paths)


class GitCheckpointOrderingTests(SetupTestCase):
    """C3 step 4 then step 5: the --git checkpoint commit exists even when
    the smoke check that follows it then fails."""

    def test_checkpoint_commit_exists_after_gated_smoke_failure(self):
        target = self.target()
        proc = self.setup_cmd(
            target,
            extra=["--git"],
            env=dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT="setup_hardlink_data:fail"),
        )
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "setup_failed", "check": "hardlink_data"})
        self.assertEqual(proc.returncode, 3)

        self.assertTrue(os.path.isdir(os.path.join(target, ".git")))
        log = subprocess.run(
            ["git", "log", "--format=%B"], cwd=target, capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("Brain-Op: setup", log)
        self.assertTrue(any(line.startswith("Brain-Run:") for line in log.splitlines()))


class VendoredDependencyTests(SetupTestCase):
    """C1/C3: a wrong vendored PyYAML version in a committed starter makes
    the copied runtime's own validate call report error/vendored_dependency
    and exit 1 — setup must return that outcome unmasked, not collapse it
    into setup_failed."""

    def test_wrong_vendored_version_reports_error_not_setup_failed(self):
        starter = tempfile.mkdtemp(prefix="brain-starter-badvendor-")
        self.addCleanup(shutil.rmtree, starter, ignore_errors=True)
        build_starter(starter)
        init_path = os.path.join(starter, "vendor", "yaml", "__init__.py")
        with open(init_path, encoding="utf-8") as fh:
            content = fh.read()
        mutated = content.replace("__version__ = '6.0.3'", "__version__ = '5.0.0'")
        self.assertNotEqual(mutated, content, "vendored __version__ literal not found to mutate")
        with open(init_path, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        _run_git(["add", "-A"], starter)
        _run_git(["commit", "-q", "-m", "break vendored version"], starter)

        target = self.target()
        proc = support.run_brain(["setup", "--starter", starter, "--target", target])
        doc = support.parse_single_json(proc.stdout)
        self.assertEqual(doc, {"outcome": "error", "reason": "vendored_dependency"})
        self.assertEqual(proc.returncode, 1)


def _real_git_version():
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True)
    import re

    match = re.search(r"(\d+)\.(\d+)", proc.stdout)
    return int(match.group(1)), int(match.group(2))


class VersionCheckSensitivityTests(unittest.TestCase):
    """A8 (spec Rules: "every check is shown to fail under a named
    injection"): proves the git/python version checks are load-bearing, via
    a landed source mutation in an actual disposable copy of the product
    code — never the live checkout, never a fake git executable on PATH
    (real Git and filesystem only, no new fault API, per spec Rules/C3)."""

    def setUp(self):
        self.disposable = tempfile.mkdtemp(prefix="brain-disposable-setup-")
        self.addCleanup(shutil.rmtree, self.disposable, ignore_errors=True)
        for name in ("bin", "brain_core", "types", "vendor"):
            shutil.copytree(
                os.path.join(support.REPO_ROOT, name),
                os.path.join(self.disposable, name),
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        self.starter = tempfile.mkdtemp(prefix="brain-starter-disposable-")
        self.addCleanup(shutil.rmtree, self.starter, ignore_errors=True)
        build_starter(self.starter)
        self.target_parent = tempfile.mkdtemp(prefix="brain-target-disposable-")
        self.addCleanup(shutil.rmtree, self.target_parent, ignore_errors=True)

    def _run(self, target_name="brain"):
        target = os.path.join(self.target_parent, target_name)
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                os.path.join(self.disposable, "bin", "brain"),
                "setup",
                "--starter",
                self.starter,
                "--target",
                target,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return support.parse_single_json(proc.stdout), proc.returncode

    def _raise_min_git_above_installed(self):
        setup_path = os.path.join(self.disposable, "brain_core", "setup.py")
        with open(setup_path, encoding="utf-8") as fh:
            src = fh.read()
        installed_major, installed_minor = _real_git_version()
        raised = f"MIN_GIT = ({installed_major}, {installed_minor + 1})"
        new_src = src.replace("MIN_GIT = (2, 30)", raised)
        self.assertNotEqual(new_src, src, "MIN_GIT constant not found to mutate")
        with open(setup_path, "w", encoding="utf-8") as fh:
            fh.write(new_src)

    def test_git_version_check_fails_setup_when_minimum_exceeds_installed(self):
        """Landed mutation: raise MIN_GIT above the real, installed git
        version in the disposable copy only; the real git binary on PATH is
        unmodified and unmocked."""
        self._raise_min_git_above_installed()
        doc, code = self._run()
        self.assertEqual(doc, {"outcome": "setup_failed", "check": "git_version"})
        self.assertEqual(code, 3)

    def test_deleting_the_version_check_breaks_that_sensitivity(self):
        """Same mutation as above, plus deleting the check's call in
        _run_smoke_checks: the assertion above must then fail, proving the
        check (not something else) is what made it fail."""
        self._raise_min_git_above_installed()
        setup_path = os.path.join(self.disposable, "brain_core", "setup.py")
        with open(setup_path, encoding="utf-8") as fh:
            src = fh.read()
        stripped = src.replace(
            "    version_check = _check_versions()\n    if version_check is not None:\n        return version_check\n",
            "",
        )
        self.assertNotEqual(stripped, src, "version-check call site not found to remove")
        with open(setup_path, "w", encoding="utf-8") as fh:
            fh.write(stripped)

        doc, code = self._run()
        self.assertEqual(doc, {"outcome": "setup_complete"})
        self.assertEqual(code, 0)

    def test_python_version_check_fails_setup_when_minimum_unmet(self):
        setup_path = os.path.join(self.disposable, "brain_core", "setup.py")
        with open(setup_path, encoding="utf-8") as fh:
            src = fh.read()
        new_src = src.replace("MIN_PYTHON = (3, 11)", "MIN_PYTHON = (99, 0)")
        self.assertNotEqual(new_src, src)
        with open(setup_path, "w", encoding="utf-8") as fh:
            fh.write(new_src)

        doc, code = self._run()
        self.assertEqual(doc, {"outcome": "setup_failed", "check": "python_version"})
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
