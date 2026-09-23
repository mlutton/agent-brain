"""Tests for `brain read` (ST-05), against docs/specification/beta.md C4, C7,
C8 (the publication rule), C10 and C13a, and Behaviour 29, 30 and 66.

Every scenario runs `bin/brain` as a subprocess against a synthetic git brain in
a fresh temporary directory outside the repository; no test imports a product
module. Fixtures are literal text written with plain file operations and plain
git. Publication records are hand-crafted commits: no delivered command
produces one, so each fixture's C7 consistency is this file's responsibility,
and one criterion deliberately builds an inconsistent commit. Every expected
value is a literal authored here, a SHA-256 taken over this file's own literal
bytes, or an id printed by `git`; none is copied from what `read` printed.
"""

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest

from tests import support
from tests.test_persist import NOTE_FRONTMATTER, build_brain

_TIMEOUT = 120

ID_ONE = "01j8k3m5n7p9q2r4s6t8v0w1x2"
ID_TWO = "01j8k3m5n7p9q2r4s6t8v0w1x3"
ID_THREE = "01j8k3m5n7p9q2r4s6t8v0w1x4"
ID_FOUR = "01j8k3m5n7p9q2r4s6t8v0w1x5"
ID_FIVE = "01j8k3m5n7p9q2r4s6t8v0w1x6"
ID_SIX = "01j8k3m5n7p9q2r4s6t8v0w1x7"
for _id in (ID_ONE, ID_TWO, ID_THREE, ID_FOUR, ID_FIVE, ID_SIX):
    assert len(_id) == 26

_GIT_ENV = dict(
    GIT_AUTHOR_NAME="fixture",
    GIT_AUTHOR_EMAIL="fixture@example.invalid",
    GIT_COMMITTER_NAME="fixture",
    GIT_COMMITTER_EMAIL="fixture@example.invalid",
)


def sha(data):
    """SHA-256 over bytes this file authored; independent of the product."""
    return hashlib.sha256(data).hexdigest()


def doc(id_, title, body, *, type_="note", origin='"authored"', evidence="[]", extra=(), kb="1", eol="\n"):
    """A literal managed-document fixture. `origin` and `evidence` are YAML text."""
    lines = [
        "---",
        f"kb: {kb}",
        f'id: "{id_}"',
        f'type: "{type_}"',
        f'title: "{title}"',
        'summary: "Fixture summary."',
        'status: "draft"',
        "created: 2026-01-01",
        "reviewed: 2026-01-01",
        f"origin: {origin}",
        f"evidence: {evidence}",
        'kind: "decision"',
        'authored_by: "human"',
        'retention: "durable"',
    ]
    lines.extend(extra)
    lines.append("---")
    return eol.join(lines) + eol + body


class ReadTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="brain-read-")
        # Registered before anything is built, so a failing test leaves nothing.
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        build_brain(self.root, git=True)

    # -- building the fixture ----------------------------------------------

    def full(self, rel):
        return os.path.join(self.root, rel)

    def write(self, rel, content):
        """Writes `content` (str or bytes) exactly, with no newline translation."""
        support.write_file(self.root, rel, content, binary=isinstance(content, bytes))

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=True,
            env=dict(os.environ, **_GIT_ENV),
        ).stdout

    def commit(self, paths, message="Plain edit"):
        self.git("add", "--", *paths)
        self.git("commit", "-q", "-m", message, "--", *paths)
        return self.git("rev-parse", "HEAD").strip()

    def commit_file(self, rel, content):
        self.write(rel, content)
        return self.commit([rel])

    def publication_commit(self, op, files, declared, message_body="Publish"):
        """Writes `files` ({path: content}) and commits them with a C7 trailer
        paragraph declaring `declared`, a list of (path, sha256, prior, role)."""
        for rel, content in files.items():
            self.write(rel, content)
        lines = [f"Brain-Op: {op}", "Brain-Run: fixture-run"]
        for path, digest, prior, role in declared:
            entry = f"Brain-Path: {path} sha256={digest} prior={prior}"
            lines.append(entry + (f" role={role}" if role else ""))
        message = message_body + "\n\n" + "\n".join(lines) + "\n"
        self.git("add", "--", *files)
        self.git("commit", "-q", "-m", message, "--", *files)
        return self.git("rev-parse", "HEAD").strip()

    # -- observing ---------------------------------------------------------

    def read(self, request, env=None):
        proc = support.run_brain(
            ["read", "--root", self.root],
            input_data=json.dumps(request),
            env=env,
            timeout=_TIMEOUT,
        )
        return proc, support.parse_single_json(proc.stdout)


class OrdinarySuccessTests(ReadTestCase):
    """R1: ordinary success; defeats echo, constant and a wrong version derivation."""

    def test_two_published_notes_read_by_id_and_by_path_return_the_other_key_and_a_full_bytes_version(self):
        body = "The same body in both files.\n"
        alpha = doc(ID_ONE, "Alpha title", body)
        beta = doc(ID_TWO, "Beta title", body)
        self.commit_file("documents/alpha/alpha.md", alpha)
        self.commit_file("documents/beta/beta.md", beta)
        # Identical bodies, different frontmatter: a version over the body alone
        # would give one value here, and C4 requires two.
        self.assertNotEqual(sha(alpha.encode()), sha(beta.encode()))

        cases = [
            ({"id": ID_ONE}, "documents/alpha/alpha.md", ID_ONE, alpha),
            ({"path": "documents/alpha/alpha.md"}, "documents/alpha/alpha.md", ID_ONE, alpha),
            ({"id": ID_TWO}, "documents/beta/beta.md", ID_TWO, beta),
            ({"path": "documents/beta/beta.md"}, "documents/beta/beta.md", ID_TWO, beta),
        ]
        for request, path, id_, text in cases:
            with self.subTest(request=request):
                proc, result = self.read(dict(request, max_bytes=4096))
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["path"], path)
                self.assertEqual(result["id"], id_)
                self.assertEqual(result["version"], sha(text.encode()))

    def test_version_is_taken_over_the_bytes_on_disk_with_crlf_and_no_trailing_newline(self):
        crlf = doc(ID_THREE, "Crlf title", "Line one\r\nLine two, no final newline", eol="\r\n")
        self.assertFalse(crlf.endswith("\n"))
        self.commit_file("documents/crlf/crlf.md", crlf)
        # The same text with LF endings hashes differently, so a version taken
        # over normalised text cannot equal the one asserted below.
        self.assertNotEqual(sha(crlf.encode()), sha(crlf.replace("\r\n", "\n").encode()))
        proc, result = self.read({"path": "documents/crlf/crlf.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["id"], ID_THREE)
        self.assertEqual(result["version"], sha(crlf.encode()))


class ByteBudgetTests(ReadTestCase):
    """R2: `excerpt` is the body, bounded to `max_bytes` bytes without splitting a character."""

    BODY = "abc\u00e9defg\n"  # ten bytes: the second character is two bytes long

    def test_budget_counts_body_bytes_and_never_splits_a_character(self):
        body_size = len(self.BODY.encode("utf-8"))
        self.assertEqual(body_size, 10)
        text = doc(ID_ONE, "Budget note", self.BODY)
        self.assertGreater(len(text.encode("utf-8")), body_size)  # the file is bigger than its body
        self.commit_file("documents/budget/budget.md", text)

        # (max_bytes, excerpt, truncated), each authored from BODY above.
        cases = [
            (0, "", True),
            (4, "abc", True),  # the cut lands inside the two-byte character
            (5, "abc\u00e9", True),  # the cut lands just after it
            (9, "abc\u00e9defg", True),  # one byte short of the whole body
            (10, self.BODY, False),  # exactly the body: not truncated
            (11, self.BODY, False),
            (4096, self.BODY, False),
        ]
        for max_bytes, excerpt, truncated in cases:
            with self.subTest(max_bytes=max_bytes):
                proc, result = self.read({"path": "documents/budget/budget.md", "max_bytes": max_bytes})
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["excerpt"], excerpt)
                self.assertIs(result["truncated"], truncated)


class FrontmatterTests(ReadTestCase):
    """R3: an ordinary note reads its parsed properties."""

    def test_frontmatter_carries_the_parsed_properties_and_not_a_slice_of_the_body(self):
        text = doc(
            ID_ONE,
            "Kestrel migration plan",
            "Prose that repeats none of the property values.\n",
            extra=["tags:", '  - "nesting"', '  - "wading"'],
        )
        self.commit_file("documents/plan/plan.md", text)
        proc, result = self.read({"path": "documents/plan/plan.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        # Authored from the fixture above. `created` and `reviewed` are unquoted
        # dates in the file and read back as their ISO text.
        self.assertEqual(
            result["frontmatter"],
            {
                "kb": 1,
                "id": ID_ONE,
                "type": "note",
                "title": "Kestrel migration plan",
                "summary": "Fixture summary.",
                "status": "draft",
                "created": "2026-01-01",
                "reviewed": "2026-01-01",
                "origin": "authored",
                "evidence": [],
                "kind": "decision",
                "authored_by": "human",
                "retention": "durable",
                "tags": ["nesting", "wading"],
            },
        )
        self.assertNotIn("Kestrel", result["excerpt"])


class NoteRoleTests(ReadTestCase):
    """R4: an authored note reads `role: note`, and `owner` is absent."""

    def test_an_ordinary_note_reads_role_note_with_no_owner_key(self):
        self.commit_file("documents/plain/plain.md", doc(ID_ONE, "Plain note", "Body.\n"))
        proc, result = self.read({"path": "documents/plain/plain.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "note")
        self.assertNotIn("owner", result)  # `owner?` belongs to a retained file naming its document


class NotFoundTests(ReadTestCase):
    """R8: a missing id or path is `not_found` (exit 0); an existing one is `ok`."""

    def test_a_missing_id_and_a_missing_path_are_not_found_and_existing_ones_are_ok(self):
        self.commit_file("documents/here/here.md", doc(ID_ONE, "Here", "Body.\n"))
        for request in ({"id": ID_TWO}, {"path": "documents/gone/gone.md"}):
            with self.subTest(missing=request):
                proc, result = self.read(dict(request, max_bytes=4096))
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "not_found", result)
                for key in ("excerpt", "frontmatter", "version"):
                    self.assertNotIn(key, result)
        for request in ({"id": ID_ONE}, {"path": "documents/here/here.md"}):
            with self.subTest(existing=request):
                proc, result = self.read(dict(request, max_bytes=4096))
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "ok", result)

    def test_paths_outside_the_data_zones_are_not_found_even_when_a_file_is_there(self):
        self.commit_file("documents/here/here.md", doc(ID_ONE, "Here", "Body.\n"))
        outside = os.path.join(os.path.dirname(self.root), os.path.basename(self.root) + "-outside.md")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("outside the brain\n")
        os.symlink(outside, self.full("documents/here/link.md"))
        requests = [
            "../" + os.path.basename(outside),  # a file beside the brain
            outside,  # the same, as an absolute path
            ".brain/journal/epoch.json",  # generated state, present on disk
            "documents/here/link.md",  # a link to a file outside the brain
            "documents/here",  # a folder
        ]
        for path in requests:
            with self.subTest(path=path):
                proc, result = self.read({"path": path, "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "not_found", result)
                self.assertNotIn("excerpt", result)


class InvalidTests(ReadTestCase):
    """R9: a malformed file is `invalid` with exit 0 (validate's exit 3 is not read's)."""

    def test_malformed_files_are_invalid_with_exit_zero_and_a_well_formed_file_is_ok(self):
        malformed = {
            "documents/bad/unclosed.md": "---\nkb: 1\ntitle: \"never closed\"\nBody.\n",
            "documents/bad/yaml.md": "---\nkb: 1\ntitle: [unbalanced\n---\nBody.\n",
            "documents/bad/duplicate.md": "---\nkb: 1\ntitle: \"one\"\ntitle: \"two\"\n---\nBody.\n",
        }
        for rel, text in malformed.items():
            self.write(rel, text)
        self.write("documents/bad/binary.md", b"---\nkb: 1\n---\n\xff\xfe not utf-8\n")
        self.write("documents/good/good.md", doc(ID_ONE, "Well formed", "Body.\n"))
        self.commit(list(malformed) + ["documents/bad/binary.md", "documents/good/good.md"])
        for rel in list(malformed) + ["documents/bad/binary.md"]:
            with self.subTest(malformed=rel):
                proc, result = self.read({"path": rel, "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "invalid", result)
        proc, result = self.read({"path": "documents/good/good.md", "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)


class UnsupportedVersionTests(ReadTestCase):
    """R10: `kb: 2` is `unsupported_version` (exit 0); `kb: 1` is `ok`."""

    def test_a_kb_2_document_is_unsupported_version_and_a_kb_1_document_is_ok(self):
        self.commit_file("documents/newer/newer.md", doc(ID_ONE, "Newer format", "Body.\n", kb="2"))
        self.commit_file("documents/older/older.md", doc(ID_TWO, "Current format", "Body.\n", kb="1"))
        proc, result = self.read({"path": "documents/newer/newer.md", "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unsupported_version", result)
        proc, result = self.read({"path": "documents/older/older.md", "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)


class HeadingRefusalTests(ReadTestCase):
    """R15: the presence of a `heading` key is refused, whatever its value."""

    def test_any_heading_key_is_refused_and_the_same_request_without_it_is_ok(self):
        self.commit_file("documents/heads/heads.md", doc(ID_ONE, "Headings", "# One\n\nText.\n"))
        base = {"path": "documents/heads/heads.md", "max_bytes": 4096}
        for value in ("One", "", None):
            with self.subTest(heading=value):
                proc, result = self.read(dict(base, heading=value))
                self.assertEqual(proc.returncode, 2, proc)
                self.assertEqual(result, {"outcome": "refused", "reason": "unsupported_heading"})
        # The twin, in the same run: the request minus the key is an ordinary read.
        proc, result = self.read(base)
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)


def _unused_local_port():
    """A port on 127.0.0.1 with nothing listening: bound, read back, released."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class EvidenceTests(ReadTestCase):
    """R14: each evidence reference reports whether it resolves; nothing is fetched."""

    def test_internal_references_resolve_and_an_external_url_is_returned_verbatim_and_never_fetched(self):
        # Nothing listens on this port, so an attempted fetch would fail loudly
        # (the run would report `error`) rather than silently succeed.
        url = f"http://127.0.0.1:{_unused_local_port()}/Report?Section=2#Frag"
        evidence = f'["{ID_TWO}", "{ID_THREE}", "{url}"]'
        self.commit_file("documents/cited/cited.md", doc(ID_ONE, "Cites three things", "Body.\n", evidence=evidence))
        self.commit_file("documents/target/target.md", doc(ID_TWO, "A resolvable target", "Body.\n"))
        started = time.monotonic()
        proc, result = self.read({"id": ID_ONE, "max_bytes": 4096})
        elapsed = time.monotonic() - started
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(
            result["evidence"],
            [
                {"target": ID_TWO, "exists": True},  # an id some document carries
                {"target": ID_THREE, "exists": False},  # an id nothing carries
                {"target": url, "exists": None},  # external: byte-identical, and unknown rather than false
            ],
        )
        self.assertLess(elapsed, 5.0)  # no network round trip fits in this bound


class OriginTests(ReadTestCase):
    """R20: `origin` in each of Behaviour 11's three shapes."""

    def test_origin_reads_as_references_with_dates_or_authored_or_unknown(self):
        references = "\n".join(
            [
                "",
                '  - ref: "https://example.invalid/source"',
                "    retrieved: 2026-03-04",
                '  - ref: "https://example.invalid/other"',
                "    retrieved:",
            ]
        )
        review = ["needs_review:", '  - "origin"']
        self.commit_file("documents/refs/refs.md", doc(ID_ONE, "Sourced", "Body.\n", origin=references))
        self.commit_file("documents/mine/mine.md", doc(ID_TWO, "Written here", "Body.\n", origin='"authored"'))
        self.commit_file(
            "documents/dunno/dunno.md", doc(ID_THREE, "Unknown source", "Body.\n", origin='"unknown"', extra=review)
        )
        self.commit_file("documents/loose/loose.md", "Just a note, no frontmatter.\n")
        expected = {
            "documents/refs/refs.md": [
                {"ref": "https://example.invalid/source", "retrieved": "2026-03-04"},
                {"ref": "https://example.invalid/other", "retrieved": None},  # an empty date is unknown
            ],
            "documents/mine/mine.md": "authored",
            "documents/dunno/dunno.md": "unknown",
            "documents/loose/loose.md": "unknown",  # an unmanaged note records no origin at all
        }
        for path, origin in expected.items():
            with self.subTest(path=path):
                proc, result = self.read({"path": path, "max_bytes": 4096})
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["origin"], origin)


class SupportTests(ReadTestCase):
    """R17: `support` is not delivered, and its key is absent (never `{}` or `null`)."""

    def test_support_key_is_absent(self):
        self.commit_file("documents/plain/plain.md", doc(ID_ONE, "Plain", "Body.\n"))
        proc, result = self.read({"path": "documents/plain/plain.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertNotIn("support", result)


class LabelTests(ReadTestCase):
    """R13: an uncommitted file reads `changed_out_of_band`, in both git states."""

    def test_a_modified_tracked_file_and_an_untracked_file_are_labelled_and_a_committed_file_is_not(self):
        self.commit_file("documents/clean/clean.md", doc(ID_ONE, "Committed", "Body.\n"))
        self.commit_file("documents/edited/edited.md", doc(ID_TWO, "Committed then edited", "Body.\n"))
        # State one: a tracked file modified after its commit.
        self.write("documents/edited/edited.md", doc(ID_TWO, "Committed then edited", "Body, edited by hand.\n"))
        # State two: a new file git has never seen.
        self.write("documents/fresh/fresh.md", doc(ID_THREE, "Never committed", "Body.\n"))
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all").splitlines(), [
            " M documents/edited/edited.md",
            "?? documents/fresh/fresh.md",
        ])
        expected = {
            "documents/clean/clean.md": {},
            "documents/edited/edited.md": {"changed_out_of_band": True},
            "documents/fresh/fresh.md": {"changed_out_of_band": True},
        }
        for path, labels in expected.items():
            with self.subTest(path=path):
                proc, result = self.read({"path": path, "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["labels"], labels)


WITHHELD_KEYS = ("excerpt", "frontmatter", "evidence", "origin", "version")


class JournalMissingTests(ReadTestCase):
    """R21: C8 rule 7c. Uncommitted with the journal gone is `unverified`, not `changed_out_of_band`."""

    def test_an_uncommitted_file_is_unverified_when_the_journal_is_gone_and_labelled_when_it_is_present(self):
        self.commit_file("documents/kept/kept.md", doc(ID_ONE, "Committed", "Body.\n"))
        self.write("documents/new/new.md", doc(ID_TWO, "Not committed", "Body.\n"))

        # The twin: journal present, so the file is an out-of-band change.
        proc, result = self.read({"path": "documents/new/new.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["labels"], {"changed_out_of_band": True})

        shutil.rmtree(self.full(".brain/journal"))
        proc, result = self.read({"path": "documents/new/new.md", "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["reason"], "journal_missing")
        self.assertNotIn("labels", result)
        for key in WITHHELD_KEYS:
            self.assertNotIn(key, result)

        # A committed file does not depend on the journal (rule 7a).
        proc, result = self.read({"path": "documents/kept/kept.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)

    def test_journal_directory_without_epoch_does_not_verify_an_uncommitted_note(self):
        path = "documents/new/note.md"
        self.write(path, doc(ID_ONE, "Uncommitted", "Body.\n"))
        proc, present = self.read({"path": path, "max_bytes": 4096})
        self.assertEqual((proc.returncode, present["outcome"]), (0, "ok"))
        os.remove(self.full(".brain/journal/epoch.json"))
        self.assertTrue(os.path.isdir(self.full(".brain/journal")))
        proc, missing = self.read({"path": path, "max_bytes": 4096})
        self.assertEqual((proc.returncode, missing),
                         (0, {"outcome": "unverified", "path": path, "reason": "journal_missing"}))


class UnevaluablePublicationTests(ReadTestCase):
    """R18: when the publication rule cannot be evaluated, read is never `ok`."""

    PATH = "documents/kept/kept.md"

    def test_missing_path_in_non_git_brain_has_the_base_response_shape(self):
        shutil.rmtree(self.full(".git"))
        proc, result = self.read({"path": "documents/gone.md", "max_bytes": 4096})
        self.assertEqual((proc.returncode, result),
                         (0, {"outcome": "unverified", "reason": "git_unavailable"}))

    def shim_git(self, script):
        """Puts a `git` first on PATH and returns the environment that finds it
        and the log the shim appends to. `script` is the shim's body; it may use
        `$REAL_GIT` (the real binary, resolved before PATH changes) and `$LOG`."""
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        shim_dir = tempfile.mkdtemp(prefix="brain-read-shim-")
        self.addCleanup(shutil.rmtree, shim_dir, ignore_errors=True)
        log = os.path.join(shim_dir, "calls.log")
        with open(os.path.join(shim_dir, "git"), "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\nREAL_GIT="{real_git}"\nLOG="{log}"\n{script}')
        os.chmod(os.path.join(shim_dir, "git"), 0o755)
        env = dict(os.environ, PATH=shim_dir + os.pathsep + os.environ["PATH"])
        return env, log

    def calls(self, log):
        if not os.path.exists(log):
            return []
        with open(log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def assert_never_ok(self, env):
        for by in ({"path": self.PATH}, {"id": ID_ONE}):
            with self.subTest(by=by):
                proc, result = self.read(dict(by, max_bytes=4096), env=env)
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "unverified", result)
                self.assertEqual(result["reason"], "git_unavailable")
                for key in WITHHELD_KEYS:
                    self.assertNotIn(key, result)

    def test_a_status_that_fails_with_empty_output_is_never_read_as_a_clean_tree(self):
        self.commit_file(self.PATH, doc(ID_ONE, "Committed", "Body.\n"))
        request = {"path": self.PATH, "max_bytes": 4096}

        # The twin: the same brain under real git.
        proc, result = self.read(request)
        self.assertEqual(result["outcome"], "ok", result)

        # A `git` first on PATH that hands every call to the real git except
        # `status`, which it fails with no output. Repository discovery, the
        # history walk and every other call therefore succeed, so the only way
        # to reach the wrong answer is to read a failed `git status`'s empty
        # output as a clean tree, which is the failure this criterion names. The
        # subcommand is the first argument that is not an option, because the
        # product may put options ahead of it.
        env, log = self.shim_git(
            'for arg in "$@"; do\n'
            '  case "$arg" in\n'
            "    -*) ;;\n"
            '    status) echo "fail status" >> "$LOG"; exit 1 ;;\n'
            '    *) echo "pass $arg" >> "$LOG"; break ;;\n'
            "  esac\n"
            "done\n"
            'exec "$REAL_GIT" "$@"\n'
        )
        self.assert_never_ok(env)
        # The injection landed where it was aimed: git was found and used for
        # other calls, and `status` is the call that failed.
        calls = self.calls(log)
        self.assertIn("pass rev-parse", calls)
        self.assertIn("fail status", calls)

    def test_a_git_that_fails_every_call_is_never_read_as_published(self):
        self.commit_file(self.PATH, doc(ID_ONE, "Committed", "Body.\n"))
        proc, result = self.read({"path": self.PATH, "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)

        # Git is present but unusable from the first call: the brain is not
        # even recognised as a repository. This is a different condition from
        # the one above, not the criterion itself.
        env, log = self.shim_git('echo "fail $1" >> "$LOG"\nexit 1\n')
        self.assert_never_ok(env)
        self.assertIn("fail rev-parse", self.calls(log))


class UnpublishedTests(ReadTestCase):
    """R11: a file named by an open intent is `unpublished`, and rule 1 precedes parsing."""

    PATH = "documents/interrupted/interrupted.md"
    BODY = "Persisted body, exactly as sent.\n"

    def persist_with_fault(self, fault):
        request = {
            "operation": "create",
            "path": self.PATH,
            "frontmatter": dict(NOTE_FRONTMATTER, title="Interrupted note"),
            "body": self.BODY,
        }
        env = dict(os.environ, BRAIN_TEST_FAULTS="1", BRAIN_FAULT=fault)
        return support.run_brain(
            ["persist", "--root", self.root], input_data=json.dumps(request), env=env, timeout=_TIMEOUT
        )

    def intent_files(self):
        directory = self.full(".brain/journal/intents")
        return sorted(os.listdir(directory)) if os.path.isdir(directory) else []

    def test_a_half_written_file_with_an_open_intent_is_unpublished_not_invalid(self):
        proc = self.persist_with_fault("after_apply:kill")
        # The injection landed: the child died by SIGKILL, the target is on
        # disk, and the intent is still open.
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc)
        self.assertTrue(os.path.isfile(self.full(self.PATH)))
        self.assertEqual(len(self.intent_files()), 1)
        # Now leave the target half-written: it no longer parses at all.
        self.write(self.PATH, b'---\nkb: 1\nid: "')
        for extra in ({}, {"include_unverified": True}):
            with self.subTest(extra=extra):
                proc, result = self.read(dict({"path": self.PATH, "max_bytes": 4096}, **extra))
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "unpublished", result)
                for key in WITHHELD_KEYS:
                    self.assertNotIn(key, result)

    def test_the_same_document_reads_ok_once_the_intent_is_finished(self):
        proc = self.persist_with_fault("before_commit:fail")
        persisted = support.parse_single_json(proc.stdout)
        self.assertEqual(persisted["outcome"], "written_incomplete", persisted)
        self.assertEqual(len(self.intent_files()), 1)  # the injection landed: the intent is open
        for by in ({"path": self.PATH}, {"id": persisted["id"]}):
            with self.subTest(before_recovery=by):
                proc, result = self.read(dict(by, max_bytes=4096))
                self.assertEqual(result["outcome"], "unpublished", result)
                for key in WITHHELD_KEYS:
                    self.assertNotIn(key, result)
        recovered = support.run_brain(["recover", "--root", self.root], timeout=_TIMEOUT)
        self.assertEqual(recovered.returncode, 0, recovered)
        self.assertEqual(self.intent_files(), [])
        proc, result = self.read({"path": self.PATH, "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["excerpt"], self.BODY)  # the literal sent in the request
        self.assertEqual(result["frontmatter"]["title"], "Interrupted note")

    def test_deleted_intent_named_path_is_still_unpublished(self):
        proc = self.persist_with_fault("after_apply:kill")
        self.assertEqual(proc.returncode, -signal.SIGKILL, proc)
        os.remove(self.full(self.PATH))
        proc, result = self.read({"path": self.PATH, "max_bytes": 4096})
        self.assertEqual((proc.returncode, result),
                         (0, {"outcome": "unpublished", "path": self.PATH}))


ORIGINAL_TEXT = "# Source article\n\nThe kept original, byte for byte.\n"


def processed_document(id_, original_rel, original_text, title="Processed paper", body="Processed body.\n"):
    """A `type: document` fixture pointing at its original by a path relative to its own folder."""
    return doc(
        id_,
        title,
        body,
        type_="document",
        extra=[
            'source_identity: "https://example.invalid/paper"',
            f'original: "{original_rel}"',
            f'original_sha256: "{sha(original_text.encode())}"',
        ],
    )


class DocumentRoleTests(ReadTestCase):
    """R5 and R12: `role: document` comes from a publication record, never from `type`."""

    DOC = "documents/paper/paper.md"
    ORIGINAL = "documents/paper/original/paper-source.md"

    def ingest(self):
        text = processed_document(ID_ONE, "original/paper-source.md", ORIGINAL_TEXT)
        self.publication_commit(
            "ingest",
            {self.DOC: text, self.ORIGINAL: ORIGINAL_TEXT},
            [
                (self.DOC, sha(text.encode()), "absent", "document"),
                (self.ORIGINAL, sha(ORIGINAL_TEXT.encode()), "absent", "original"),
            ],
        )
        return text

    def test_a_document_with_an_ingest_record_reads_as_a_document_and_one_without_is_unverified(self):
        self.ingest()
        proc, result = self.read({"path": self.DOC, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "document")
        # The twin: the same frontmatter type, committed plainly, no record.
        self.commit_file("documents/bare/bare.md", processed_document(ID_TWO, "original/x.md", ORIGINAL_TEXT))
        proc, result = self.read({"path": "documents/bare/bare.md", "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["reason"], "no_ingest_record")

    def edit_and_commit_out_of_band(self):
        """A recorded document whose bytes were then changed and committed
        plainly: no `Brain-Op` trailer, so neither a brain write nor an
        audit-labelled edit. Returns the edited bytes."""
        self.ingest()
        edited = processed_document(
            ID_ONE, "original/paper-source.md", ORIGINAL_TEXT, body="Changed by hand after ingest.\n"
        )
        self.commit_file(self.DOC, edited)
        return edited

    def test_the_out_of_band_edit_fixture_is_committed_and_leaves_the_ingest_record_in_history(self):
        # The fixture behind the next test, checked apart from it: an expected
        # failure would hide a fixture that never built what it claims.
        edited = self.edit_and_commit_out_of_band()
        with open(self.full(self.DOC), "rb") as fh:
            self.assertEqual(fh.read(), edited.encode())
        recorded = processed_document(ID_ONE, "original/paper-source.md", ORIGINAL_TEXT)
        self.assertNotEqual(sha(edited.encode()), sha(recorded.encode()))
        self.assertEqual(self.git("status", "--porcelain", "--", self.DOC), "")
        self.assertIn("Brain-Op: ingest", self.git("log", "--format=%B", "--", self.DOC))
        self.assertEqual(self.git("log", "--format=%s", "-1"), "Plain edit\n")

    def test_a_document_edited_and_committed_out_of_band_after_ingest_is_unverified(self):
        # C8 rule 5 requires a published document's current bytes to descend from
        # its ingest or adopt commit, with later changes only through brain
        # writes; otherwise `unverified` with `no_ingest_record`.
        self.edit_and_commit_out_of_band()
        proc, result = self.read({"path": self.DOC, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["reason"], "no_ingest_record")
        for key in WITHHELD_KEYS:
            self.assertNotIn(key, result)

    def test_include_unverified_gives_the_content_and_its_absence_gives_none(self):
        self.commit_file(
            "documents/bare/bare.md",
            processed_document(ID_TWO, "original/x.md", ORIGINAL_TEXT, title="Unrecorded paper", body="Unrecorded body.\n"),
        )
        path = "documents/bare/bare.md"
        proc, result = self.read({"path": path, "max_bytes": 4096})
        self.assertEqual(result["outcome"], "unverified", result)
        for key in WITHHELD_KEYS:
            self.assertNotIn(key, result)
        proc, result = self.read({"path": path, "max_bytes": 4096, "include_unverified": True})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["excerpt"], "Unrecorded body.\n")
        self.assertEqual(result["frontmatter"]["title"], "Unrecorded paper")
        for key in ("version", "evidence", "origin"):
            self.assertIn(key, result)


class DescentTests(ReadTestCase):
    PATH = "documents/lineage/doc.md"

    def recorded(self):
        original = doc(ID_ONE, "Recorded", "First body.\n", type_="document")
        self.publication_commit("ingest", {self.PATH: original},
                                [(self.PATH, sha(original.encode()), "absent", "document")])
        return original

    def test_record_survives_long_unrelated_history(self):
        self.recorded()
        for _ in range(1100):
            self.git("commit", "--allow-empty", "-q", "-m", "Unrelated checkpoint")
        result = self.outcome()
        self.assertEqual((result["outcome"], result["role"]), ("ok", "document"))

    def outcome(self):
        proc, result = self.read({"path": self.PATH, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        return result

    def test_untouched_and_consistent_update_descend(self):
        original = self.recorded()
        self.assertEqual((self.outcome()["outcome"], self.outcome()["role"]), ("ok", "document"))
        changed = doc(ID_ONE, "Recorded", "Second body.\n", type_="document")
        self.publication_commit("update", {self.PATH: changed},
                                [(self.PATH, sha(changed.encode()), sha(original.encode()), None)])
        self.assertEqual(self.outcome()["outcome"], "ok")

    def test_inconsistent_update_and_audit_do_not_extend_descent(self):
        for operation, declared_hash in (("update", "0" * 64), ("audit", None)):
            with self.subTest(operation=operation):
                base = self.git("rev-parse", "HEAD").strip()
                original = self.recorded()
                changed = doc(ID_ONE, "Recorded", "Second body.\n", type_="document")
                self.publication_commit(operation, {self.PATH: changed},
                                        [(self.PATH, declared_hash or sha(changed.encode()),
                                          sha(original.encode()), None)])
                try:
                    result = self.outcome()
                    self.assertEqual((result["outcome"], result.get("reason")), ("unverified", "no_ingest_record"))
                    for key in WITHHELD_KEYS:
                        self.assertNotIn(key, result)
                finally:
                    self.git("reset", "--hard", base)

    def test_update_prior_need_not_chain_to_previous_blob(self):
        self.recorded()
        changed = doc(ID_ONE, "Recorded", "Second body.\n", type_="document")
        self.publication_commit("update", {self.PATH: changed},
                                [(self.PATH, sha(changed.encode()), "f" * 64, None)])
        self.assertEqual(self.outcome()["outcome"], "ok")

    def test_record_role_survives_type_change_and_malformed_bytes_fail_before_parse(self):
        for content in (doc(ID_ONE, "Recorded", "Second body.\n"), "---\nkb: [broken\n---\n"):
            with self.subTest(content=content[:20]):
                base = self.git("rev-parse", "HEAD").strip()
                self.recorded()
                self.commit_file(self.PATH, content)
                try:
                    result = self.outcome()
                    self.assertEqual((result["outcome"], result.get("reason")), ("unverified", "no_ingest_record"))
                finally:
                    self.git("reset", "--hard", base)

    def test_latest_retry_or_new_ingest_starts_descent_afresh(self):
        for change_again in (False, True):
            with self.subTest(change_again=change_again):
                base = self.git("rev-parse", "HEAD").strip()
                self.recorded()
                hand_edit = doc(ID_ONE, "Recorded", "Hand edit.\n", type_="document")
                self.commit_file(self.PATH, hand_edit)
                current = doc(ID_ONE, "Recorded", "New ingest.\n", type_="document") if change_again else hand_edit
                if change_again:
                    self.publication_commit("ingest", {self.PATH: current},
                                            [(self.PATH, sha(current.encode()), sha(hand_edit.encode()), "document")])
                else:
                    message = ("Retry\n\nBrain-Op: ingest\nBrain-Run: fixture-run\n"
                               f"Brain-Path: {self.PATH} sha256={sha(current.encode())} "
                               f"prior={sha(current.encode())} role=document\n")
                    self.git("commit", "-q", "--allow-empty", "-m", message)
                try:
                    self.assertEqual(self.outcome()["outcome"], "ok")
                finally:
                    self.git("reset", "--hard", base)

    def test_uncommitted_edit_keeps_document_published_with_label(self):
        self.recorded()
        self.write(self.PATH, doc(ID_ONE, "Recorded", "Hand edit.\n", type_="document"))
        result = self.outcome()
        self.assertEqual((result["outcome"], result.get("role"), result.get("labels")),
                         ("ok", "document", {"changed_out_of_band": True}))


class MergeDescentTests(ReadTestCase):
    PATH = DescentTests.PATH
    recorded = DescentTests.recorded
    outcome = DescentTests.outcome
    def branch(self):
        self.recorded()
        primary = self.git("branch", "--show-current").strip()
        self.git("checkout", "-q", "-b", "fixture-side")
        return primary

    def merge(self):
        self.git("merge", "-q", "--no-ff", "-m", "Join histories", "fixture-side")

    def test_side_plain_edit_kept_by_merge_breaks_descent(self):
        primary = self.branch()
        self.commit_file(self.PATH, doc(ID_ONE, "Recorded", "Side edit.\n", type_="document"))
        self.git("checkout", "-q", primary)
        self.merge()
        self.assertEqual(self.outcome()["outcome"], "unverified")

    def test_side_plain_edit_discarded_by_merge_is_not_judged(self):
        primary = self.branch()
        self.commit_file(self.PATH, doc(ID_ONE, "Recorded", "Side edit.\n", type_="document"))
        self.git("checkout", "-q", primary)
        main = doc(ID_ONE, "Recorded", "Main edit.\n", type_="document")
        self.publication_commit("update", {self.PATH: main},
                                [(self.PATH, sha(main.encode()), "f" * 64, None)])
        self.git("merge", "-q", "-s", "ours", "--no-ff", "-m", "Discard side edit", "fixture-side")
        self.assertEqual(self.outcome()["outcome"], "ok")

    def test_merge_introducing_new_blob_breaks_descent(self):
        primary = self.branch()
        self.commit_file(self.PATH, doc(ID_ONE, "Recorded", "Side edit.\n", type_="document"))
        self.git("checkout", "-q", primary)
        main = doc(ID_ONE, "Recorded", "Main edit.\n", type_="document")
        self.publication_commit("update", {self.PATH: main},
                                [(self.PATH, sha(main.encode()), "f" * 64, None)])
        proc = subprocess.run(["git", "merge", "--no-ff", "--no-commit", "fixture-side"],
                              cwd=self.root, capture_output=True, text=True, env=dict(os.environ, **_GIT_ENV))
        self.assertNotEqual(proc.returncode, 0)
        self.write(self.PATH, doc(ID_ONE, "Recorded", "Merge edit.\n", type_="document"))
        self.git("add", "--", self.PATH)
        self.git("commit", "-q", "-m", "Resolve with new bytes")
        self.assertEqual(self.outcome()["outcome"], "unverified")

    def test_side_brain_write_kept_by_merge_descends(self):
        primary = self.branch()
        changed = doc(ID_ONE, "Recorded", "Side edit.\n", type_="document")
        self.publication_commit("update", {self.PATH: changed},
                                [(self.PATH, sha(changed.encode()), "f" * 64, None)])
        self.git("checkout", "-q", primary)
        self.merge()
        self.assertEqual(self.outcome()["outcome"], "ok")

    def test_equal_parent_blobs_require_both_lineages_to_descend(self):
        primary = self.branch()
        changed = doc(ID_ONE, "Recorded", "Same bytes.\n", type_="document")
        self.commit_file(self.PATH, changed)
        self.git("checkout", "-q", primary)
        self.publication_commit("update", {self.PATH: changed},
                                [(self.PATH, sha(changed.encode()), "f" * 64, None)])
        self.merge()
        self.assertEqual(self.outcome()["outcome"], "unverified")


class DescendingReferrerTests(ReadTestCase):
    DOC = "documents/referred/doc.md"

    def test_out_of_band_document_taints_original_and_attachment_but_descending_twin_does_not(self):
        for field in ("original", "attachments"):
            with self.subTest(field=field):
                base = self.git("rev-parse", "HEAD").strip()
                target = "documents/referred/source.md"
                self.commit_file(target, "Source bytes.\n")
                extra = ['original: "source.md"'] if field == "original" else ['attachments: ["source.md"]']
                first = doc(ID_ONE, "Referrer", "First body.\n", type_="document", extra=extra)
                self.publication_commit("ingest", {self.DOC: first},
                                        [(self.DOC, sha(first.encode()), "absent", "document")])
                proc, control = self.read({"path": target, "max_bytes": 4096})
                self.assertEqual((proc.returncode, control["outcome"]), (0, "ok"))
                second = doc(ID_ONE, "Referrer", "Hand edit.\n", type_="document", extra=extra)
                self.commit_file(self.DOC, second)
                try:
                    proc, result = self.read({"path": target, "max_bytes": 4096})
                    self.assertEqual((proc.returncode, result["outcome"], result.get("hint")),
                                     (0, "unverified", "suspected_ingestion"))
                finally:
                    self.git("reset", "--hard", base)


ORIGINAL_KB2 = '---\nkb: 2\ntitle: "Foreign format original"\n---\nOriginal body.\n'
ORIGINAL_BROKEN = "---\nkb: [unclosed\n---\nOriginal body.\n"


def _as_bytes(content):
    return content if isinstance(content, bytes) else content.encode("utf-8")


class BundleTestCase(ReadTestCase):
    """Adds a hand-crafted publication commit for one processed document, its
    original and its attachments. C7 consistency is this helper's job: each
    declared hash is taken over the literal bytes written, and the keyword
    switches break exactly one clause on purpose."""

    def bundle(
        self,
        folder,
        id_,
        *,
        original_text=ORIGINAL_TEXT,
        attachments=None,
        op="ingest",
        undeclared=None,
        wrong_hash=None,
        precommitted=(),
        prior=None,
    ):
        base = f"documents/{folder}"
        doc_rel = f"{base}/{folder}.md"
        original_rel = f"{base}/original/source.md"
        attachment_rels = {f"{base}/attachments/{name}": content for name, content in (attachments or {}).items()}
        extra = [
            'source_identity: "https://example.invalid/paper"',
            'original: "original/source.md"',
            f'original_sha256: "{sha(_as_bytes(original_text))}"',
        ]
        if attachment_rels:
            extra.append("attachments:")
            extra.extend(f'  - "attachments/{name}"' for name in attachments)
        document_text = doc(id_, f"Bundle {folder}", "Processed body.\n", type_="document", extra=extra)
        contents = {doc_rel: document_text, original_rel: original_text, **attachment_rels}
        declared = []
        for rel, content in contents.items():
            role = "document" if rel == doc_rel else "original" if rel == original_rel else "attachment"
            digest = sha(b"not these bytes") if wrong_hash == rel else sha(_as_bytes(content))
            declared.append((rel, digest, (prior or {}).get(rel, "absent"), role))
        for rel in precommitted:
            self.commit_file(rel, contents[rel])
        files = {rel: content for rel, content in contents.items() if rel not in precommitted}
        files.update(undeclared or {})
        commit = self.publication_commit(op, files, declared)
        return {"doc": doc_rel, "original": original_rel, "attachments": list(attachment_rels), "commit": commit}


class RetainedOriginalTests(BundleTestCase):
    """R6: `role: original` from the publication record and only from the record."""

    def test_an_original_is_read_as_evidence_whatever_it_holds_and_with_no_frontmatter_interpretation(self):
        for index, text in enumerate((ORIGINAL_KB2, ORIGINAL_BROKEN)):
            folder = f"paper{index}"
            with self.subTest(original=text.splitlines()[1]):
                made = self.bundle(folder, f"01j8k3m5n7p9q2r4s6t8v0w1y{index}", original_text=text)
                proc, result = self.read({"path": made["original"], "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["role"], "original")
                self.assertEqual(result["owner"], made["doc"])
                self.assertEqual(result["integrity"], "retained")
                self.assertEqual(result["recorded_sha256"], sha(text.encode()))
                self.assertEqual(result["publication_commit"], made["commit"])  # `git rev-parse HEAD`
                self.assertEqual(result["version"], sha(text.encode()))
                self.assertNotIn("frontmatter", result)
                # The whole file is the excerpt, its own `---` line included.
                self.assertTrue(result["excerpt"].startswith("---"))
                self.assertEqual(result["excerpt"], text)
                self.assertIs(result["truncated"], False)

    def test_altered_bytes_read_retained_changed_with_the_same_recorded_hash_and_commit(self):
        altered = "# Source article\n\nEdited afterwards, by hand.\n"
        for label, commit_it in (("uncommitted", False), ("committed out of band", True)):
            with self.subTest(edit=label):
                folder = "changed" + ("c" if commit_it else "u")
                made = self.bundle(folder, ID_TWO if commit_it else ID_THREE)
                self.write(made["original"], altered)
                later = self.commit([made["original"]], "Edit without a trailer") if commit_it else None
                if commit_it:
                    self.assertNotEqual(later, made["commit"])
                proc, result = self.read({"path": made["original"], "max_bytes": 4096})
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["role"], "original")
                self.assertEqual(result["integrity"], "retained_changed")
                self.assertEqual(result["recorded_sha256"], sha(ORIGINAL_TEXT.encode()))
                self.assertEqual(result["publication_commit"], made["commit"])
                self.assertEqual(result["version"], sha(altered.encode()))
                self.assertEqual(result["excerpt"], altered)

    def test_each_original_names_the_document_that_references_it_when_a_commit_declares_two(self):
        first = processed_document(ID_ONE, "original/a.md", "First original.\n", title="First")
        second = processed_document(ID_TWO, "original/b.md", "Second original.\n", title="Second")
        files = {
            "documents/one/one.md": first,
            "documents/one/original/a.md": "First original.\n",
            "documents/two/two.md": second,
            "documents/two/original/b.md": "Second original.\n",
        }
        declared = [
            ("documents/one/one.md", sha(first.encode()), "absent", "document"),
            ("documents/two/two.md", sha(second.encode()), "absent", "document"),
            ("documents/one/original/a.md", sha(b"First original.\n"), "absent", "original"),
            ("documents/two/original/b.md", sha(b"Second original.\n"), "absent", "original"),
        ]
        self.publication_commit("ingest", files, declared)
        for original, owner in (
            ("documents/one/original/a.md", "documents/one/one.md"),
            ("documents/two/original/b.md", "documents/two/two.md"),
        ):
            with self.subTest(original=original):
                proc, result = self.read({"path": original, "max_bytes": 4096})
                self.assertEqual(result["role"], "original", result)
                self.assertEqual(result["owner"], owner)

    def test_role_never_comes_from_a_reference_an_unverified_referrer_makes_its_files_unverified(self):
        # No publication record anywhere below: everything is committed plainly.
        base = "documents/ref1"
        unrecorded = doc(
            ID_ONE,
            "Unrecorded bundle",
            "Body.\n",
            type_="document",
            extra=[
                'source_identity: "https://example.invalid/paper"',
                'original: "original/source.md"',
                f'original_sha256: "{sha(ORIGINAL_TEXT.encode())}"',
                "attachments:",
                '  - "attachments/pic.bin"',
            ],
        )
        self.commit_file(f"{base}/ref1.md", unrecorded)
        self.commit_file(f"{base}/original/source.md", ORIGINAL_TEXT)
        self.commit_file(f"{base}/attachments/pic.bin", b"\x89PNG\r\n\x1a\n binary")
        for rel in (f"{base}/original/source.md", f"{base}/attachments/pic.bin"):
            with self.subTest(referenced=rel):
                proc, result = self.read({"path": rel, "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "unverified", result)  # not an `error`, not `ok`
                self.assertEqual(result["hint"], "suspected_ingestion")
                self.assertNotIn("role", result)
        proc, result = self.read({"path": f"{base}/ref1.md", "max_bytes": 4096})
        self.assertEqual(result["outcome"], "unverified", result)
        # Asked for, an unverified original's text is given, and it is still unverified.
        proc, result = self.read(
            {"path": f"{base}/original/source.md", "max_bytes": 4096, "include_unverified": True}
        )
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["excerpt"], ORIGINAL_TEXT)

    def test_role_never_comes_from_a_reference_a_published_note_leaves_its_target_a_note(self):
        note = doc(ID_TWO, "A note that mentions an original", "Body.\n", extra=['original: "original/y.md"'])
        self.commit_file("documents/ref2/ref2.md", note)
        self.commit_file("documents/ref2/original/y.md", "Just another note.\n")
        proc, result = self.read({"path": "documents/ref2/original/y.md", "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "note")

    def test_a_trailer_that_is_not_consistent_establishes_no_role_whichever_clause_it_breaks(self):
        original = "documents/{}/original/source.md"
        cases = {
            # C7 clause 1: a data-zone path the commit changes must be declared.
            "tb1": dict(undeclared={"documents/tb1/stray.md": "Changed, but never declared.\n"}),
            # Clause 2: a declared path's content must match its declared hash.
            "tb2": dict(wrong_hash=original.format("tb2")),
            # Clause 3: a declared path the commit does not change must declare `prior` equal to its hash.
            "tb3": dict(precommitted=[original.format("tb3")], prior={original.format("tb3"): sha(b"older bytes")}),
        }
        for index, (folder, breaks) in enumerate(cases.items()):
            with self.subTest(folder=folder):
                made = self.bundle(folder, f"01j8k3m5n7p9q2r4s6t8v0w1z{index}", **breaks)
                proc, result = self.read({"path": made["original"], "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "unverified", result)
                self.assertEqual(result["hint"], "suspected_ingestion")
                self.assertNotIn("role", result)
                proc, result = self.read({"path": made["doc"], "max_bytes": 4096})
                self.assertEqual(result["outcome"], "unverified", result)  # the document has no record either

    def test_a_commit_that_does_not_change_an_original_may_declare_it_when_prior_equals_its_hash(self):
        # The consistent counterpart of clause 3 above: an ingest retry declares
        # roles for bytes that were already committed.
        rel = "documents/tb4/original/source.md"
        made = self.bundle("tb4", ID_SIX, precommitted=[rel], prior={rel: sha(ORIGINAL_TEXT.encode())})
        proc, result = self.read({"path": made["original"], "max_bytes": 4096})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "original")
        self.assertEqual(result["publication_commit"], made["commit"])


class RetainedAttachmentTests(BundleTestCase):
    """R7: `role: attachment`, decided by the record and never by the file extension."""

    NOTES = doc(ID_FIVE, "Attached but shaped like a note", "Notes kept as an attachment.\n")

    def test_a_markdown_file_declared_as_an_attachment_reads_as_one_and_is_not_matched_by_its_own_id(self):
        made = self.bundle("att1", ID_ONE, attachments={"notes.md": self.NOTES})
        (attachment,) = made["attachments"]
        proc, result = self.read({"path": attachment, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "attachment")
        self.assertEqual(result["owner"], made["doc"])
        self.assertEqual(result["integrity"], "retained")
        self.assertEqual(result["recorded_sha256"], sha(self.NOTES.encode()))
        self.assertEqual(result["publication_commit"], made["commit"])
        self.assertNotIn("frontmatter", result)
        self.assertEqual(result["excerpt"], self.NOTES)  # the frontmatter block is part of the bytes
        # Its own `id` line does not make it findable: it is never indexed as a note.
        proc, result = self.read({"id": ID_FIVE, "max_bytes": 4096})
        self.assertEqual(result["outcome"], "not_found", result)

    def test_an_attachment_needs_a_consistent_record_like_an_original_does(self):
        attachment = "documents/{}/attachments/notes.md"
        cases = {
            "att2": dict(undeclared={"documents/att2/stray.md": "Changed, but never declared.\n"}),
            "att3": dict(wrong_hash=attachment.format("att3")),
            "att4": dict(precommitted=[attachment.format("att4")], prior={attachment.format("att4"): sha(b"older bytes")}),
        }
        for index, (folder, breaks) in enumerate(cases.items()):
            with self.subTest(folder=folder):
                made = self.bundle(folder, f"01j8k3m5n7p9q2r4s6t8v0w1a{index}", attachments={"notes.md": self.NOTES}, **breaks)
                (path,) = made["attachments"]
                proc, result = self.read({"path": path, "max_bytes": 4096})
                self.assertEqual(result["outcome"], "unverified", result)
                self.assertEqual(result["hint"], "suspected_ingestion")
                self.assertNotIn("role", result)


class RetainedMissingTests(BundleTestCase):
    """R16: a retained path whose file is gone is still reported, positively.

    Content fields for this state are not delivered by this story, so none is asserted."""

    def test_a_missing_retained_path_reports_role_owner_recorded_hash_and_commit(self):
        notes = "# Attached notes\n\nSoon to be deleted.\n"
        for label, commit_it in (("deleted, uncommitted", False), ("deleted, committed out of band", True)):
            with self.subTest(deletion=label):
                folder = "gone" + ("c" if commit_it else "u")
                made = self.bundle(folder, ID_TWO if commit_it else ID_THREE, attachments={"notes.md": notes})
                (attachment,) = made["attachments"]
                # The twin: while the file is there it is retained.
                proc, result = self.read({"path": attachment, "max_bytes": 4096})
                self.assertEqual(result["integrity"], "retained", result)
                os.remove(self.full(attachment))
                if commit_it:
                    self.git("add", "--", attachment)
                    self.git("commit", "-q", "-m", "Remove without a trailer", "--", attachment)
                proc, result = self.read({"path": attachment, "max_bytes": 4096})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["path"], attachment)
                self.assertEqual(result["role"], "attachment")
                self.assertEqual(result["owner"], made["doc"])
                self.assertEqual(result["integrity"], "retained_missing")
                self.assertEqual(result["recorded_sha256"], sha(notes.encode()))
                self.assertEqual(result["publication_commit"], made["commit"])


class BinaryAttachmentTests(BundleTestCase):
    """R19: a retained attachment that is not text has `excerpt: null` and `truncated: null`."""

    BLOB = b"abcd\xff\xfe\x00\x01 and a binary tail"  # its first four bytes alone are valid UTF-8
    TEXT = "# Attached notes\n"

    def test_non_utf8_bytes_read_as_null_whatever_the_budget_and_text_still_reads_as_text(self):
        made = self.bundle("bin1", ID_ONE, attachments={"blob.bin": self.BLOB, "notes.md": self.TEXT})
        blob, notes = made["attachments"]
        for max_bytes in (4, 4096):
            with self.subTest(max_bytes=max_bytes):
                proc, result = self.read({"path": blob, "max_bytes": max_bytes})
                self.assertEqual(proc.returncode, 0, proc)
                self.assertEqual(result["outcome"], "ok", result)
                self.assertEqual(result["role"], "attachment")
                self.assertEqual(result["integrity"], "retained")
                self.assertIn("excerpt", result)
                self.assertIsNone(result["excerpt"])  # not "", and not a lossy decode
                self.assertIn("truncated", result)
                self.assertIsNone(result["truncated"])  # not applicable
                self.assertEqual(result["version"], sha(self.BLOB))
        # The twin: a text attachment under the same four-byte budget is cut like any text.
        proc, result = self.read({"path": notes, "max_bytes": 4})
        self.assertEqual(result["excerpt"], "# At")
        self.assertIs(result["truncated"], True)


class ReferrerLocalityTests(ReadTestCase):
    """C8 rule 6 states no locality condition: whether a referrer taints a file
    depends on its reference RESOLVING to that file, not on where the referrer
    sits. C5 makes `original` relative to the document's own folder, which
    admits `../`, so a referrer outside the target's ancestor folders is an
    ordinary case and not an exotic one (agent-brain #44)."""

    TARGET = "documents/shared/pic.md"
    TARGET_TEXT = "Just a picture placeholder.\n"

    def referrer(self, id_, original):
        """An unrecorded `type: document` — rule 5 makes it unverified — whose
        `original` names `original` relative to its own folder."""
        return doc(
            id_,
            "Unrecorded bundle",
            "Body.\n",
            type_="document",
            extra=[
                'source_identity: "https://example.invalid/paper"',
                f'original: "{original}"',
                f'original_sha256: "{sha(self.TARGET_TEXT.encode())}"',
            ],
        )

    def test_a_referrer_outside_the_targets_ancestor_folders_makes_it_unverified(self):
        # `wiki/r.md` is in no ancestor folder of `documents/shared/pic.md`; its
        # `original` resolves to it all the same.
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        self.commit_file("wiki/r.md", self.referrer(ID_ONE, "../documents/shared/pic.md"))
        proc, result = self.read({"path": self.TARGET, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["hint"], "suspected_ingestion")
        self.assertNotIn("role", result)

    def test_the_control_a_co_located_referrer_still_makes_the_same_target_unverified(self):
        # The same target, reached by a referrer sitting beside it. This is what
        # makes the pair a locality finding: it must not change.
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        self.commit_file("documents/shared/s.md", self.referrer(ID_TWO, "pic.md"))
        proc, result = self.read({"path": self.TARGET, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["hint"], "suspected_ingestion")
        self.assertNotIn("role", result)

    def test_a_reference_that_resolves_elsewhere_leaves_the_target_a_published_note(self):
        # The negative twin of the first case: a referrer outside the ancestor
        # folders whose `original` resolves to some other path taints nothing.
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        self.commit_file("wiki/r.md", self.referrer(ID_THREE, "../documents/shared/other.md"))
        proc, result = self.read({"path": self.TARGET, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "note")

    def test_a_hidden_referrer_is_not_scanned_and_leaves_the_target_a_published_note(self):
        # A committed `documents/shared/.ref.md` naming `pic.md`. C5a's scan
        # skips every name beginning with a dot, so read never considers it a
        # rule 6 referrer at all. Pinned here because this diff CHANGED it: the
        # folder listing the search used before scanned hidden names, and the
        # scan does not (agent-brain #55).
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        self.commit_file("documents/shared/.ref.md", self.referrer(ID_FOUR, "pic.md"))
        # The referrer really is committed, so nothing else could be deciding this.
        self.assertEqual(self.git("status", "--porcelain", "--", "documents/shared/.ref.md"), "")
        proc, result = self.read({"path": self.TARGET, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "note")


class RetainedEvidenceIsNeverAReferrerTests(BundleTestCase):
    """C8 rule 2 precedes rule 5 in the first-match order: a retained original
    or attachment is "decided by its recorded role, whatever its current
    bytes", and is "never a candidate and never a note"; C13a says it is
    "never parsed as frontmatter". So retained evidence can never be the
    unverified `document` rule 6 looks for, however its bytes happen to read
    (agent-brain #44, from the review of PR #56)."""

    TARGET = "documents/shared/pic.md"
    TARGET_TEXT = "Just a picture placeholder.\n"

    def impersonator(self):
        """Bytes that parse as an unrecorded `type: document` whose `original`
        resolves to TARGET from a bundle's `original/` or `attachments/`
        folder. The record says this file is evidence; only its bytes claim to
        be a document."""
        return doc(
            ID_TWO,
            "Impersonating bytes",
            "Body.\n",
            type_="document",
            extra=[
                'source_identity: "https://example.invalid/paper"',
                'original: "../../shared/pic.md"',
                f'original_sha256: "{sha(self.TARGET_TEXT.encode())}"',
            ],
        )

    def assert_target_is_an_untainted_note(self):
        proc, result = self.read({"path": self.TARGET, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertEqual(result["role"], "note")
        return result

    def test_a_retained_original_whose_bytes_read_as_a_document_taints_nothing(self):
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        made = self.bundle("paper", ID_ONE, original_text=self.impersonator())
        # The record answers for the source itself, so the fixture is the one
        # the finding describes: evidence by record, document by bytes.
        proc, source = self.read({"path": made["original"], "max_bytes": 4096})
        self.assertEqual(source["outcome"], "ok", source)
        self.assertEqual(source["role"], "original")
        self.assertEqual(source["integrity"], "retained")
        self.assertEqual(source["excerpt"], self.impersonator())
        self.assert_target_is_an_untainted_note()

    def test_a_retained_attachment_whose_bytes_read_as_a_document_taints_nothing(self):
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        made = self.bundle("paper", ID_ONE, attachments={"ref.md": self.impersonator()})
        attachment = made["attachments"][0]
        self.assertEqual(attachment, "documents/paper/attachments/ref.md")
        proc, source = self.read({"path": attachment, "max_bytes": 4096})
        self.assertEqual(source["outcome"], "ok", source)
        self.assertEqual(source["role"], "attachment")
        self.assertEqual(source["integrity"], "retained")
        self.assert_target_is_an_untainted_note()

    def test_the_control_the_same_bytes_at_an_unrecorded_path_still_taint(self):
        # The same impersonating text, with no record naming it, sitting where
        # round 1's widened search is what finds it: a cross-tree referrer that
        # IS an unrecorded document must still make the target unverified. This
        # is what separates the fix from a retreat toward the ancestor folders.
        self.commit_file(self.TARGET, self.TARGET_TEXT)
        self.commit_file("documents/paper/attachments/ref.md", self.impersonator())
        proc, result = self.read({"path": self.TARGET, "max_bytes": 4096})
        self.assertEqual(proc.returncode, 0, proc)
        self.assertEqual(result["outcome"], "unverified", result)
        self.assertEqual(result["hint"], "suspected_ingestion")
        self.assertNotIn("role", result)


class AbsentPathTests(BundleTestCase):
    def intent_for(self, path):
        intent = {
            "run_id": "fixture-run", "op_key": "fixture-op", "operation": "attach",
            "path": path, "expected_prior": "absent", "intended_sha256": "0" * 64,
            "temp_path": "fixture-temp", "backup_path": None,
        }
        self.write(".brain/journal/intents/fixture.json", json.dumps(intent))

    def test_retained_attachment_named_by_intent_is_never_indexed_by_id(self):
        attachment = doc(ID_TWO, "Retained bytes", "Attachment body.\n")
        made = self.bundle("indexed", ID_ONE, attachments={"ref.md": attachment})
        target = made["attachments"][0]
        self.intent_for(target)
        proc, by_path = self.read({"path": target, "max_bytes": 4096})
        self.assertEqual((proc.returncode, by_path),
                         (0, {"outcome": "unpublished", "path": target}))
        proc, by_id = self.read({"id": ID_TWO, "max_bytes": 4096})
        self.assertEqual((proc.returncode, by_id), (0, {"outcome": "not_found"}))

    def test_missing_retained_path_yields_to_open_intent_and_plain_missing_path_is_not_found(self):
        made = self.bundle("absent", ID_ONE, attachments={"notes.md": "Attachment bytes.\n"})
        target = made["attachments"][0]
        os.remove(self.full(target))
        proc, retained = self.read({"path": target, "max_bytes": 4096})
        self.assertEqual((proc.returncode, retained["outcome"], retained["integrity"]),
                         (0, "ok", "retained_missing"))
        self.intent_for(target)
        proc, unpublished = self.read({"path": target, "max_bytes": 4096})
        self.assertEqual((proc.returncode, unpublished),
                         (0, {"outcome": "unpublished", "path": target}))
        proc, missing = self.read({"path": "documents/absent/other.md", "max_bytes": 4096})
        self.assertEqual((proc.returncode, missing), (0, {"outcome": "not_found"}))


if __name__ == "__main__":
    unittest.main()
