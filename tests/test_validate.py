"""Tests for `brain validate` (ST-01), against docs/specification/beta.md."""

import collections
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from typing import ClassVar

from tests import support


def make_frontmatter(fields, needs_review=()):
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {value}")
    if needs_review:
        lines.append("needs_review:")
        for item in needs_review:
            lines.append(f'  - "{item}"')
    lines.append("---")
    return "\n".join(lines) + "\n"


def note_doc(body="Body text.\n", needs_review=("created", "reviewed", "summary", "kind", "origin"), **overrides):
    fields = {
        "kb": "1",
        "id": '"01arz3ndektsv4rrffq69g5fav"',
        "type": '"note"',
        "title": '"Sample Note"',
        "summary": '""',
        "status": '"draft"',
        "created": None,
        "reviewed": None,
        "origin": '"unknown"',
        "evidence": "[]",
        "kind": '"unknown"',
        "authored_by": '"human"',
        "retention": '"durable"',
    }
    fields.update(overrides)
    return make_frontmatter(fields, needs_review) + body


class TempBrainTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="brain-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def write(self, rel_path, content, **kwargs):
        return support.write_file(self.tmpdir, rel_path, content, **kwargs)

    def validate(self, expect_paths, restrict=None, restore=None, prove_denial=None):
        """Runs `brain validate`, asserting the rules every call site needs: the
        brain's files, directories and hashes are unchanged, stdout is one JSON
        object, and the report's document paths are exactly expect_paths."""
        before_hashes = support.hash_tree(self.tmpdir)
        before_paths = support.list_paths(self.tmpdir)
        restricted = []
        if restrict is not None:
            self.addCleanup(restore)
            restricted = restrict()
            if prove_denial is not None:
                try:
                    prove_denial()
                except PermissionError:
                    pass
                else:
                    self.skipTest("permission restriction did not deny the required access")
            else:
                for path, _mode in restricted:
                    try:
                        if os.path.isdir(path):
                            os.listdir(path)
                        else:
                            with open(path, "rb"):
                                pass
                    except PermissionError:
                        continue
                    self.skipTest(f"permission restriction did not deny access to {path}")
        proc = support.run_brain(["validate", "--root", self.tmpdir])
        report = support.parse_single_json(proc.stdout)
        restricted_modes = [(path, os.stat(path).st_mode) for path, _mode in restricted]
        if restore is not None:
            restore()
        after_hashes = support.hash_tree(self.tmpdir)
        after_paths = support.list_paths(self.tmpdir)
        self.assertEqual(before_hashes, after_hashes)
        self.assertEqual(before_paths, after_paths)
        self.assertEqual(restricted_modes, [(path, mode) for path, mode in restricted])
        self.assertEqual(support.document_paths(report), set(expect_paths))
        return proc, report


class TestReportShapeAndCounts(TempBrainTestCase):
    def test_report_shape_and_counts(self):
        self.write("documents/valid.md", note_doc())
        self.write("documents/bad.md", "no frontmatter here\n")  # unmanaged
        self.write("documents/broken.md", "---\nkb: 1\nid: 1\n---\nbody\n")  # invalid (malformed base fields missing etc)
        self.write("documents/future.md", note_doc(kb="2", needs_review=()))
        # version is the SHA-256 of the raw file bytes, never a CRLF-normalised
        # copy: this fixture keeps its CRLF line endings verbatim.
        self.write("documents/crlf.md", note_doc().replace("\n", "\r\n"), newline="")

        before = support.hash_tree(self.tmpdir)
        _proc, report = self.validate(
            expect_paths={
                "documents/valid.md",
                "documents/bad.md",
                "documents/broken.md",
                "documents/future.md",
                "documents/crlf.md",
            }
        )
        after = support.hash_tree(self.tmpdir)
        self.assertEqual(before, after)

        self.assertEqual(set(report.keys()), {"outcome", "counts", "definitions", "skipped", "documents"})
        self.assertIn(report["outcome"], ("valid", "invalid"))
        count_sum = sum(report["counts"].values())
        self.assertEqual(count_sum, len(report["documents"]))
        self.assertEqual(report["counts"]["retained"], 0)

        for doc in report["documents"]:
            self.assertEqual(
                set(doc.keys()),
                {"path", "status", "frontmatter", "frontmatter_features", "id", "type", "version", "errors"},
                doc["path"],
            )
            full = os.path.join(self.tmpdir, doc["path"])
            with open(full, "rb") as fh:
                expected_hash = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(doc["version"], expected_hash)


class TestScanScopeAndOrder(TempBrainTestCase):
    def test_scan_scope_and_order(self):
        self.write("documents/a-b.md", "unmanaged\n")
        self.write("documents/a/b.md", "unmanaged\n")
        self.write("documents/nested/deep/doc.md", "unmanaged\n")
        self.write("wiki/page.md", "unmanaged\n")
        self.write("inbox/skip.md", "unmanaged\n")
        self.write("raw/skip.md", "unmanaged\n")
        self.write(".hidden/skip.md", "unmanaged\n")
        self.write("documents/.hidden.md", "unmanaged\n")
        self.write("documents/UPPER.MD", "unmanaged\n")
        self.write("documents/note.txt", "not markdown\n")
        self.write("root.md", "unmanaged\n")

        self.write(".brain/modules/projects/module.json", json.dumps({"module": "projects", "folder": "projects"}))
        self.write("projects/demo/decision.md", "unmanaged\n")

        target = self.write("documents/link_target.md", "unmanaged\n")
        link_path = os.path.join(self.tmpdir, "documents", "a_link.md")
        os.symlink(target, link_path)

        # A hidden name is skipped silently even when it is a symlink: the
        # hidden rule takes precedence over the symlink rule.
        hidden_link_path = os.path.join(self.tmpdir, "documents", ".hidden_link.md")
        os.symlink(target, hidden_link_path)

        expected_paths = {
            "documents/a-b.md",
            "documents/a/b.md",
            "documents/nested/deep/doc.md",
            "wiki/page.md",
            "documents/link_target.md",
            "projects/demo/decision.md",
        }
        _proc, report = self.validate(expect_paths=expected_paths)

        paths = [d["path"] for d in report["documents"]]
        self.assertEqual(paths, sorted(paths))

        # expect_paths in self.validate() above already pins the exact
        # document-path set, so inbox/, raw/, hidden entries, root.md,
        # documents/note.txt, documents/UPPER.MD and the symlinks are proven
        # absent by that equality, not by a further subset/intersection check.
        idx_ab_dash = paths.index("documents/a-b.md")
        idx_a_slash_b = paths.index("documents/a/b.md")
        self.assertLess(idx_ab_dash, idx_a_slash_b)

        self.assertEqual(report["skipped"], [{"path": "documents/a_link.md", "reason": "symlink"}])


class TestScanEdgeCases(TempBrainTestCase):
    def test_symlinked_zone_root_is_listed_in_skipped(self):
        real_wiki = os.path.join(self.tmpdir, "real_wiki")
        os.makedirs(real_wiki)
        support.write_file(self.tmpdir, "real_wiki/page.md", "unmanaged\n")
        os.symlink(real_wiki, os.path.join(self.tmpdir, "wiki"))
        os.makedirs(os.path.join(self.tmpdir, "documents"))

        _proc, report = self.validate(expect_paths=set())
        self.assertEqual(report["skipped"], [{"path": "wiki", "reason": "symlink"}])

    def test_dangling_and_file_target_zone_root_symlinks_are_listed(self):
        # A zone-root symlink is listed under skipped whatever its target is
        # -- dangling, or pointing at a plain file -- never only when the
        # target happens to be a directory.
        os.symlink(os.path.join(self.tmpdir, "does-not-exist"), os.path.join(self.tmpdir, "wiki"))
        a_file = self.write("not_a_dir.md", "unmanaged\n")
        os.symlink(a_file, os.path.join(self.tmpdir, "documents"))

        _proc, report = self.validate(expect_paths=set())
        self.assertEqual(
            report["skipped"],
            [{"path": "documents", "reason": "symlink"}, {"path": "wiki", "reason": "symlink"}],
        )

    def test_module_json_folder_validation(self):
        # documents: reuses the reserved name and must not duplicate the
        # documents/ zone or scan it twice; escaping ("..") and reserved
        # names (inbox) must not be scanned; a hidden folder name is ignored.
        self.write("documents/base.md", "unmanaged\n")
        self.write("inbox/leaked.md", "unmanaged\n")
        self.write(".hidden_module/leaked.md", "unmanaged\n")

        self.write(".brain/modules/dup_documents/module.json", json.dumps({"module": "dup_documents", "folder": "documents"}))
        self.write(".brain/modules/escape/module.json", json.dumps({"module": "escape", "folder": ".."}))
        self.write(".brain/modules/reserved_inbox/module.json", json.dumps({"module": "reserved_inbox", "folder": "inbox"}))
        self.write(".brain/modules/hidden/module.json", json.dumps({"module": "hidden", "folder": ".hidden_module"}))

        # A module.json whose top-level value is not an object is also simply
        # not installed, whatever JSON scalar or collection it holds.
        self.write(".brain/modules/non_object_list/module.json", "[]")
        self.write(".brain/modules/non_object_string/module.json", '"projects"')
        self.write(".brain/modules/non_object_number/module.json", "42")
        self.write(".brain/modules/non_object_null/module.json", "null")

        # An invalid module.json is simply not installed: it is not a
        # validate error in its own right.
        _proc, report = self.validate(expect_paths={"documents/base.md"})
        self.assertEqual(report["outcome"], "valid")
        self.assertEqual(report["definitions"], [])


class TestExitCodesAndRefusals(TempBrainTestCase):
    def test_valid_brain_exit_zero(self):
        self.write("documents/ok.md", note_doc())
        proc, report = self.validate(expect_paths={"documents/ok.md"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report["outcome"], "valid")

    def test_empty_brain_exit_zero(self):
        os.makedirs(os.path.join(self.tmpdir, "documents"))
        proc, report = self.validate(expect_paths=set())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report["outcome"], "valid")
        self.assertEqual(report["counts"]["valid"], 0)
        self.assertEqual(report["documents"], [])

    def test_invalid_brain_exit_three(self):
        self.write("documents/bad.md", "---\nkb: 1\n---\nbody\n")
        proc, report = self.validate(expect_paths={"documents/bad.md"})
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(report["outcome"], "invalid")

    def test_missing_root(self):
        proc = support.run_brain(["validate"])
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report["outcome"], "refused")

    def test_nonexistent_root(self):
        proc = support.run_brain(["validate", "--root", os.path.join(self.tmpdir, "does-not-exist")])
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report["outcome"], "refused")

    def test_file_root(self):
        file_path = self.write("not-a-dir.txt", "hello")
        proc = support.run_brain(["validate", "--root", file_path])
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report["outcome"], "refused")

    def test_unknown_subcommand(self):
        proc = support.run_brain(["frobnicate", "--root", self.tmpdir])
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report["outcome"], "refused")

    def test_known_but_not_implemented_subcommand_is_refused(self):
        proc = support.run_brain(["setup", "--root", self.tmpdir])
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(report, {"outcome": "refused", "reason": "not_implemented"})


class TestUnmanagedClasses(TempBrainTestCase):
    def test_unmanaged_classes(self):
        self.write("documents/no_block.md", "Just prose, no frontmatter.\n")
        self.write("documents/no_kb.md", "---\ntitle: \"x\"\n---\nbody\n")
        self.write("documents/empty_block.md", "---\n---\nbody\n")
        self.write("documents/comment_only.md", "---\n# just a comment\n---\nbody\n")

        _proc, report = self.validate(
            expect_paths={
                "documents/no_block.md",
                "documents/no_kb.md",
                "documents/empty_block.md",
                "documents/comment_only.md",
            }
        )
        for path in (
            "documents/no_block.md",
            "documents/no_kb.md",
            "documents/empty_block.md",
            "documents/comment_only.md",
        ):
            doc = support.doc_by_path(report, path)
            self.assertEqual(doc["status"], "unmanaged", path)
            self.assertEqual(doc["errors"], [], path)
            if path == "documents/no_block.md":
                self.assertEqual(doc["frontmatter"], "none", path)


class TestKbReading(TempBrainTestCase):
    def test_kb_reading(self):
        self.write("documents/managed.md", note_doc())
        self.write("documents/v2.md", note_doc(kb="2", needs_review=()))
        self.write("documents/v0.md", note_doc(kb="0", needs_review=()))
        self.write("documents/vneg.md", note_doc(kb="-3", needs_review=()))
        self.write("documents/bool_kb.md", note_doc(kb="true", needs_review=()))
        self.write("documents/str_kb.md", note_doc(kb='"1"', needs_review=()))
        self.write("documents/float_kb.md", note_doc(kb="1.0", needs_review=()))
        self.write("documents/empty_kb.md", note_doc(kb=None, needs_review=()))

        _proc, report = self.validate(
            expect_paths={
                "documents/managed.md",
                "documents/v2.md",
                "documents/v0.md",
                "documents/vneg.md",
                "documents/bool_kb.md",
                "documents/str_kb.md",
                "documents/float_kb.md",
                "documents/empty_kb.md",
            }
        )

        self.assertEqual(support.doc_by_path(report, "documents/managed.md")["status"], "valid")

        for path in ("documents/v2.md", "documents/v0.md", "documents/vneg.md"):
            doc = support.doc_by_path(report, path)
            self.assertEqual(doc["status"], "unsupported_version", path)
            self.assertEqual(doc["errors"], [], path)

        for path in ("documents/bool_kb.md", "documents/str_kb.md", "documents/float_kb.md", "documents/empty_kb.md"):
            doc = support.doc_by_path(report, path)
            self.assertEqual(doc["status"], "invalid", path)
            support.assert_errors(self, doc, [(None, "malformed")], path)

        self.assertEqual(report["outcome"], "invalid")

    def test_unsupported_version_alone_does_not_make_outcome_invalid(self):
        self.write("documents/v2.md", note_doc(kb="2", needs_review=()))
        _proc, report = self.validate(expect_paths={"documents/v2.md"})
        self.assertEqual(report["outcome"], "valid")
        self.assertEqual(support.doc_by_path(report, "documents/v2.md")["status"], "unsupported_version")


class TestMalformedConditions(TempBrainTestCase):
    def assert_malformed(self, path):
        _proc, report = self.validate(expect_paths={path})
        doc = support.doc_by_path(report, path)
        support.assert_errors(self, doc, [(None, "malformed")], path)
        self.assertEqual(doc["status"], "invalid", path)

    def test_unclosed_block(self):
        self.write("documents/unclosed.md", "---\nkb: 1\ntitle: \"x\"\nbody without closing\n")
        self.assert_malformed("documents/unclosed.md")

    def test_ambiguous_colon(self):
        self.write("documents/colon.md", "---\nkb: 1\ntitle: a: b\n---\nbody\n")
        self.assert_malformed("documents/colon.md")

    def test_top_level_list(self):
        self.write("documents/list.md", "---\n- kb\n- 1\n---\nbody\n")
        self.assert_malformed("documents/list.md")

    def test_duplicate_keys(self):
        self.write(
            "documents/dup.md",
            '---\nkb: 1\ntitle: "same"\ntitle: "same"\n---\nbody\n',
        )
        self.assert_malformed("documents/dup.md")

    def test_merge_key_overrides_are_not_duplicates(self):
        # A merged key that an explicit key overrides, or that two merge
        # sources both provide, is standard YAML 1.1 merge behaviour, not a
        # duplicate-key collision -- only two of the mapping's own explicit
        # keys ever collide.
        top_level_override = "---\nkb: 1\nbase: &b\n  c: 1\nc: 2\n<<: *b\n---\nbody\n"
        self.write("documents/merge_top_level_override.md", top_level_override)

        nested_override = (
            "---\nkb: 1\nnested:\n  base: &b\n    x: 1\n  y: 2\n  <<: *b\n---\nbody\n"
        )
        self.write("documents/merge_nested_override.md", nested_override)

        multi_merge_shared_key = (
            "---\nkb: 1\nb1: &b1\n  x: 1\nb2: &b2\n  x: 2\n<<: [*b1, *b2]\n---\nbody\n"
        )
        self.write("documents/merge_multi_shared_key.md", multi_merge_shared_key)

        merged_title = '---\nkb: 1\nmeta: &m\n  title: "Base Title"\n<<: *m\n---\nbody\n'
        self.write("documents/merge_merged_title.md", merged_title)

        _proc, report = self.validate(
            expect_paths={
                "documents/merge_top_level_override.md",
                "documents/merge_nested_override.md",
                "documents/merge_multi_shared_key.md",
                "documents/merge_merged_title.md",
            }
        )
        # None of these fixtures declare the field-contract fields (only kb
        # plus merge scaffolding), so a non-malformed parse leaves exactly the
        # required-field "missing" errors -- proving the merge itself never
        # trips the malformed path. TestMergeAndUncertaintyRegressions below
        # proves the actual override/preservation values with complete
        # documents.
        all_required_missing = [
            (field, "missing")
            for field in (
                "id",
                "type",
                "title",
                "summary",
                "status",
                "created",
                "reviewed",
                "origin",
                "evidence",
                "kind",
                "authored_by",
                "retention",
            )
        ]
        # merge_merged_title.md merges "title" in from its source, so title
        # is genuinely supplied and is not missing there.
        title_supplied_missing = [pair for pair in all_required_missing if pair[0] != "title"]
        for path, expected in (
            ("documents/merge_top_level_override.md", all_required_missing),
            ("documents/merge_nested_override.md", all_required_missing),
            ("documents/merge_multi_shared_key.md", all_required_missing),
            ("documents/merge_merged_title.md", title_supplied_missing),
        ):
            doc = support.doc_by_path(report, path)
            support.assert_errors(self, doc, expected, path)
            self.assertNotEqual(doc["frontmatter"], "malformed", path)

    def test_invalid_utf8(self):
        self.write("documents/badutf8.md", b"---\nkb: 1\n---\n\xff\xfe\n", binary=True)
        self.assert_malformed("documents/badutf8.md")

    def test_bad_calendar_date(self):
        self.write("documents/baddate.md", "---\nkb: 1\ncreated: 2026-02-30\n---\nbody\n")
        self.assert_malformed("documents/baddate.md")

    def test_python_tuple_tag(self):
        self.write("documents/tuple.md", "---\nkb: 1\nx: !!python/tuple [1, 2]\n---\nbody\n")
        self.assert_malformed("documents/tuple.md")

    def test_bom_and_crlf_recognised(self):
        content = "﻿---\r\nkb: 1\r\n---\r\nbody\r\n".encode()
        self.write("documents/bomcrlf.md", content, binary=True)
        _proc, report = self.validate(expect_paths={"documents/bomcrlf.md"})
        doc = support.doc_by_path(report, "documents/bomcrlf.md")
        self.assertNotEqual(doc["frontmatter"], "malformed")
        support.assert_errors(
            self,
            doc,
            [
                (field, "missing")
                for field in (
                    "id",
                    "type",
                    "title",
                    "summary",
                    "status",
                    "created",
                    "reviewed",
                    "origin",
                    "evidence",
                    "kind",
                    "authored_by",
                    "retention",
                )
            ],
        )

    def test_unconstructible_tags_and_deep_nesting_are_malformed(self):
        self.write("documents/bad_timestamp.md", "---\nkb: 1\nx: !!timestamp nope\n---\nbody\n")
        self.write("documents/bad_bool.md", "---\nkb: 1\nx: !!bool maybe\n---\nbody\n")
        deep = "x: " + "[" * 3000 + "]" * 3000
        self.write("documents/deep_nesting.md", f"---\nkb: 1\n{deep}\n---\nbody\n")

        proc, report = self.validate(
            expect_paths={"documents/bad_timestamp.md", "documents/bad_bool.md", "documents/deep_nesting.md"}
        )
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(report["outcome"], "invalid")
        for path in ("documents/bad_timestamp.md", "documents/bad_bool.md", "documents/deep_nesting.md"):
            self.assert_malformed_from_report(report, path)

    def assert_malformed_from_report(self, report, path):
        doc = support.doc_by_path(report, path)
        support.assert_errors(self, doc, [(None, "malformed")], path)
        self.assertEqual(doc["status"], "invalid", path)


class TestUnsupportedForEditing(TempBrainTestCase):
    def test_features_are_not_errors(self):
        cases = {
            "anchor_alias": ("a: &x \"v\"\nb: *x", {"anchor", "alias"}),
            "explicit_tag": ("tagged: !!str \"v\"", {"tag"}),
            # A merge key requires an alias, so the block is valid YAML and
            # unsupported_for_editing, never malformed.
            "merge_key": ("base: &b {c: 1}\n<<: *b", {"anchor", "alias"}),
        }
        for name, (extra, expected_features) in cases.items():
            path = f"documents/{name}_managed.md"
            fields = {
                "kb": "1",
                "id": '"01arz3ndektsv4rrffq69g5fav"',
                "type": '"note"',
                "title": '"Sample"',
                "summary": '""',
                "status": '"draft"',
                "created": None,
                "reviewed": None,
                "origin": '"unknown"',
                "evidence": "[]",
                "kind": '"unknown"',
                "authored_by": '"human"',
                "retention": '"durable"',
            }
            text = "---\n" + "\n".join(
                f"{k}: {v}" if v is not None else f"{k}:" for k, v in fields.items()
            )
            text += "\nneeds_review:\n"
            for item in ("created", "reviewed", "summary", "kind", "origin"):
                text += f'  - "{item}"\n'
            text += extra + "\n---\nbody\n"
            self.write(path, text)

        json_style = '---\n{"title": "x"}\n---\nbody\n'
        self.write("documents/json_style_unmanaged.md", json_style)

        json_style_managed = json.dumps(
            {
                "kb": 1,
                "id": "01arz3ndektsv4rrffq69g5fav",
                "type": "note",
                "title": "Sample",
                "summary": "",
                "status": "draft",
                "created": None,
                "reviewed": None,
                "origin": "unknown",
                "evidence": [],
                "kind": "unknown",
                "authored_by": "human",
                "retention": "durable",
                "needs_review": ["created", "reviewed", "summary", "kind", "origin"],
            }
        )
        self.write("documents/json_style_managed.md", f"---\n{json_style_managed}\n---\nbody\n")

        all_paths = {f"documents/{name}_managed.md" for name in cases} | {
            "documents/json_style_unmanaged.md",
            "documents/json_style_managed.md",
        }
        _proc, report = self.validate(expect_paths=all_paths)
        for name, (extra, expected_features) in cases.items():
            path = f"documents/{name}_managed.md"
            doc = support.doc_by_path(report, path)
            self.assertEqual(doc["frontmatter"], "unsupported_for_editing", path)
            self.assertEqual(set(doc["frontmatter_features"]), expected_features, path)
            self.assertEqual(doc["errors"], [], path)

        doc = support.doc_by_path(report, "documents/json_style_unmanaged.md")
        self.assertEqual(doc["status"], "unmanaged")
        self.assertEqual(doc["frontmatter"], "unsupported_for_editing")
        self.assertIn("flow_mapping", doc["frontmatter_features"])
        self.assertEqual(doc["errors"], [])

        doc = support.doc_by_path(report, "documents/json_style_managed.md")
        self.assertEqual(doc["status"], "valid", doc["errors"])
        self.assertEqual(doc["frontmatter"], "unsupported_for_editing")
        self.assertIn("flow_mapping", doc["frontmatter_features"])
        self.assertEqual(doc["errors"], [])

    def test_multiple_documents_is_not_malformed(self):
        # A "--- " line with a trailing space does not close the frontmatter
        # block (only an exact "---" line does), but YAML still reads it as a
        # second-document separator, so the block holds two YAML documents.
        base = note_doc()
        closing_idx = base.rindex("\n---\n")
        text = base[:closing_idx] + '\n--- \nextra: "ignored second document"' + base[closing_idx:]
        self.write("documents/multi_doc.md", text)

        _proc, report = self.validate(expect_paths={"documents/multi_doc.md"})
        doc = support.doc_by_path(report, "documents/multi_doc.md")
        self.assertEqual(doc["status"], "valid", doc["errors"])
        self.assertEqual(doc["frontmatter"], "unsupported_for_editing")
        self.assertIn("multiple_documents", doc["frontmatter_features"])
        self.assertEqual(doc["errors"], [])


class TestAmbiguousScalars(TempBrainTestCase):
    def test_ambiguous_scalars(self):
        self.write("documents/yes_title.md", note_doc(title="yes"))
        self.write("documents/num_title.md", note_doc(title="12"))
        self.write("documents/float_summary.md", note_doc(summary="1.5", needs_review=("created", "reviewed", "kind", "origin")))
        self.write("documents/link_title.md", note_doc(title="[[Link]]"))
        self.write("documents/yes_status.md", note_doc(status="yes"))

        self.write("documents/quoted_ok.md", note_doc(title='"yes"', status='"draft"'))

        _proc, report = self.validate(
            expect_paths={
                "documents/yes_title.md",
                "documents/num_title.md",
                "documents/float_summary.md",
                "documents/link_title.md",
                "documents/yes_status.md",
                "documents/quoted_ok.md",
            }
        )

        support.assert_errors(self, support.doc_by_path(report, "documents/yes_title.md"), [("title", "ambiguous_scalar")])
        support.assert_errors(self, support.doc_by_path(report, "documents/num_title.md"), [("title", "ambiguous_scalar")])
        support.assert_errors(self, support.doc_by_path(report, "documents/float_summary.md"), [("summary", "ambiguous_scalar")])
        support.assert_errors(self, support.doc_by_path(report, "documents/link_title.md"), [("title", "ambiguous_scalar")])
        support.assert_errors(self, support.doc_by_path(report, "documents/yes_status.md"), [("status", "ambiguous_scalar")])
        self.assertEqual(support.doc_by_path(report, "documents/quoted_ok.md")["status"], "valid")


class TestObsidianFixtureSetValid(TempBrainTestCase):
    def test_obsidian_fixtures(self):
        cases = {}

        cases["quoted_colon_title"] = note_doc(title='"a title: with colon"')
        cases["quoted_hash_value"] = note_doc(title='"a value with # hash"')
        cases["unquoted_hash_no_space"] = note_doc(title="value#nospace")
        cases["single_quoted"] = note_doc(title="'single quoted'")
        cases["double_quoted"] = note_doc(title='"double quoted"')

        text_with_comment = make_frontmatter(
            {
                "kb": "1",
                "id": '"01arz3ndektsv4rrffq69g5fav"',
                "type": '"note"',
                "title": '"With comment"',
                "summary": '""',
                "status": '"draft"',
                "created": None,
                "reviewed": None,
                "origin": '"unknown"',
                "evidence": "[]",
                "kind": '"unknown"',
                "authored_by": '"human"',
                "retention": '"durable"',
                "tags": "\n  - \"a\"\n# a comment line between properties\n  - \"b\"",
            },
            needs_review=("created", "reviewed", "summary", "kind", "origin"),
        ) + "body\n"
        cases["comment_between_properties"] = text_with_comment

        cases["indented_list"] = note_doc(evidence='\n    - "https://example.com/a"')
        cases["zero_indent_list"] = note_doc(evidence='\n- "https://example.com/a"')
        cases["empty_list"] = note_doc(evidence="[]")
        cases["unquoted_date"] = note_doc(created="2026-01-01", needs_review=("reviewed", "summary", "kind", "origin"))
        cases["quoted_date"] = note_doc(created='"2026-01-01"', needs_review=("reviewed", "summary", "kind", "origin"))
        cases["empty_values"] = note_doc()
        # An explicit "" marker is the same disclosed uncertainty as a blank
        # property, not a distinct ambiguous or missing value.
        cases["quoted_empty_string_marker"] = note_doc(created='""')

        for name, content in cases.items():
            self.write(f"documents/{name}.md", content)

        long_value = "x" * 301
        self.write(
            "documents/comment_truncated_quoted.md",
            note_doc(summary=f'"{long_value}"', needs_review=("created", "reviewed", "kind", "origin")),
        )
        self.write(
            "documents/comment_truncated_unquoted.md",
            note_doc(summary="short value # " + ("y" * 320), needs_review=("created", "reviewed", "kind", "origin")),
        )

        expect_paths = {f"documents/{name}.md" for name in cases} | {
            "documents/comment_truncated_quoted.md",
            "documents/comment_truncated_unquoted.md",
        }
        _proc, report = self.validate(expect_paths=expect_paths)

        for name in cases:
            doc = support.doc_by_path(report, f"documents/{name}.md")
            self.assertEqual(doc["status"], "valid", "{}: {!r}".format(name, doc["errors"]))

        truncated_quoted = support.doc_by_path(report, "documents/comment_truncated_quoted.md")
        support.assert_errors(self, truncated_quoted, [("summary", "too_long")])

        truncated_unquoted = support.doc_by_path(report, "documents/comment_truncated_unquoted.md")
        self.assertEqual(truncated_unquoted["errors"], [])


class TestDates(TempBrainTestCase):
    def test_dates(self):
        self.write("documents/yaml_date.md", note_doc(created="2026-01-01", needs_review=("reviewed", "summary", "kind", "origin")))
        self.write("documents/quoted_date.md", note_doc(created='"2026-01-01"', needs_review=("reviewed", "summary", "kind", "origin")))
        self.write("documents/bad_short.md", note_doc(created='"2026-9-14"', needs_review=("reviewed", "summary", "kind", "origin")))
        self.write("documents/bad_word.md", note_doc(created='"unknown"', needs_review=("reviewed", "summary", "kind", "origin")))
        self.write("documents/bad_datetime.md", note_doc(created='"2026-09-14T10:00:00Z"', needs_review=("reviewed", "summary", "kind", "origin")))
        self.write("documents/bad_datetime_unquoted.md", note_doc(created="2026-09-14T10:00:00Z", needs_review=("reviewed", "summary", "kind", "origin")))
        # Fullwidth digits are not ASCII: Python's \d matches them under the
        # default (non-ASCII) regex flags, so this must still be bad_format,
        # not a coerced calendar date.
        self.write(
            "documents/fullwidth_digits.md",
            note_doc(created='"２０２６-09-14"', needs_review=("reviewed", "summary", "kind", "origin")),
        )

        _proc, report = self.validate(
            expect_paths={
                "documents/yaml_date.md",
                "documents/quoted_date.md",
                "documents/bad_short.md",
                "documents/bad_word.md",
                "documents/bad_datetime.md",
                "documents/bad_datetime_unquoted.md",
                "documents/fullwidth_digits.md",
            }
        )

        for path in ("documents/yaml_date.md", "documents/quoted_date.md"):
            doc = support.doc_by_path(report, path)
            self.assertEqual(doc["errors"], [], path)

        for path in (
            "documents/bad_short.md",
            "documents/bad_word.md",
            "documents/bad_datetime.md",
            "documents/bad_datetime_unquoted.md",
            "documents/fullwidth_digits.md",
        ):
            doc = support.doc_by_path(report, path)
            support.assert_errors(self, doc, [("created", "bad_format")], path)


class TestFieldContractRejections(TempBrainTestCase):
    def test_missing_required_fields(self):
        required = [
            "kb",
            "id",
            "type",
            "title",
            "summary",
            "status",
            "created",
            "reviewed",
            "origin",
            "evidence",
            "kind",
            "authored_by",
            "retention",
        ]
        base_fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"note"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"unknown"',
            "authored_by": '"human"',
            "retention": '"durable"',
        }
        # created/reviewed/summary declare an empty-value uncertainty marker (C5),
        # but that marker means "the editor wrote the property with no value" --
        # it never applies to a property that was never written at all. Deleting
        # one of these three keys entirely is "missing", exactly like any other
        # required field, not a disclosable uncertainty. needs_review is
        # built per fixture so a deleted field is never listed as disclosed.
        default_uncertain = {"created", "reviewed", "summary", "kind", "origin"}
        for field in required:
            fields = dict(base_fields)
            del fields[field]
            needs_review = tuple(sorted(default_uncertain - {field}))
            text = make_frontmatter(fields, needs_review=needs_review) + "body\n"
            self.write(f"documents/missing_{field}.md", text)

        _proc, report = self.validate(expect_paths={f"documents/missing_{field}.md" for field in required})
        for field in required:
            doc = support.doc_by_path(report, f"documents/missing_{field}.md")
            if field == "kb":
                self.assertEqual(doc["status"], "unmanaged", field)
                self.assertEqual(doc["errors"], [], field)
            else:
                support.assert_errors(self, doc, [(field, "missing")], field)

    def test_not_allowed_enums(self):
        self.write("documents/bad_status.md", note_doc(status='"weird"'))
        self.write("documents/bad_kind.md", note_doc(kind='"weird"', needs_review=("created", "reviewed", "summary", "origin")))
        self.write("documents/bad_authored_by.md", note_doc(authored_by='"weird"'))
        self.write("documents/bad_retention.md", note_doc(retention='"weird"'))

        _proc, report = self.validate(
            expect_paths={
                "documents/bad_status.md",
                "documents/bad_kind.md",
                "documents/bad_authored_by.md",
                "documents/bad_retention.md",
            }
        )
        support.assert_errors(self, support.doc_by_path(report, "documents/bad_status.md"), [("status", "not_allowed")])
        support.assert_errors(self, support.doc_by_path(report, "documents/bad_kind.md"), [("kind", "not_allowed")])
        support.assert_errors(
            self, support.doc_by_path(report, "documents/bad_authored_by.md"), [("authored_by", "not_allowed")]
        )
        support.assert_errors(
            self, support.doc_by_path(report, "documents/bad_retention.md"), [("retention", "not_allowed")]
        )

    def test_too_long_summary(self):
        self.write(
            "documents/summary_300.md",
            note_doc(summary='"%s"' % ("é" * 300), needs_review=("created", "reviewed", "kind", "origin")),
        )
        self.write(
            "documents/summary_301.md",
            note_doc(summary='"%s"' % ("é" * 301), needs_review=("created", "reviewed", "kind", "origin")),
        )
        _proc, report = self.validate(expect_paths={"documents/summary_300.md", "documents/summary_301.md"})
        self.assertEqual(support.doc_by_path(report, "documents/summary_300.md")["errors"], [])
        support.assert_errors(
            self, support.doc_by_path(report, "documents/summary_301.md"), [("summary", "too_long")]
        )

    def test_document_type_required_fields(self):
        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"document"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"source"',
            "authored_by": '"human"',
            "retention": '"durable"',
        }
        text = make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "origin")) + "body\n"
        self.write("documents/doc_missing_source.md", text)

        # source_identity must be non-empty text (C5a) -- an explicitly empty
        # value is "missing" too, not silently accepted.
        empty_source_fields = dict(fields)
        empty_source_fields.update(
            {"source_identity": None, "original": '"original/a.md"', "original_sha256": '"%s"' % ("a" * 64)}
        )
        empty_source_text = make_frontmatter(empty_source_fields, needs_review=("created", "reviewed", "summary", "origin")) + "body\n"
        self.write("documents/doc_empty_source.md", empty_source_text)

        _proc, report = self.validate(
            expect_paths={"documents/doc_missing_source.md", "documents/doc_empty_source.md"}
        )
        doc = support.doc_by_path(report, "documents/doc_missing_source.md")
        support.assert_errors(
            self,
            doc,
            [("source_identity", "missing"), ("original", "missing"), ("original_sha256", "missing")],
        )
        support.assert_errors(
            self, support.doc_by_path(report, "documents/doc_empty_source.md"), [("source_identity", "missing")]
        )

    def test_malformed_original_sha256(self):
        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"document"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"source"',
            "authored_by": '"human"',
            "retention": '"durable"',
            "source_identity": '"https://example.com/a"',
            "original": '"original/a.md"',
            "original_sha256": '"not-a-hash"',
        }
        text = make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "origin")) + "body\n"
        self.write("documents/doc_bad_hash.md", text)
        _proc, report = self.validate(expect_paths={"documents/doc_bad_hash.md"})
        doc = support.doc_by_path(report, "documents/doc_bad_hash.md")
        support.assert_errors(self, doc, [("original_sha256", "bad_format")])

    def test_unknown_type(self):
        self.write("documents/unknown_type.md", note_doc(type='"gadget"'))
        _proc, report = self.validate(expect_paths={"documents/unknown_type.md"})
        doc = support.doc_by_path(report, "documents/unknown_type.md")
        support.assert_errors(self, doc, [("type", "unknown_type")])

    def test_wrong_zone(self):
        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"document"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"source"',
            "authored_by": '"human"',
            "retention": '"durable"',
            "source_identity": '"https://example.com/a"',
            "original": '"original/a.md"',
            "original_sha256": '"%s"' % ("a" * 64),
        }
        text = make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "origin")) + "body\n"
        self.write("wiki/misplaced_document.md", text)
        _proc, report = self.validate(expect_paths={"wiki/misplaced_document.md"})
        doc = support.doc_by_path(report, "wiki/misplaced_document.md")
        support.assert_errors(self, doc, [("type", "wrong_zone")])

    def test_evidence_bad_format(self):
        self.write("documents/bad_evidence.md", note_doc(evidence='["ftp://x", "[[Link]]", "0123456789012345678901234"]'))
        _proc, report = self.validate(expect_paths={"documents/bad_evidence.md"})
        doc = support.doc_by_path(report, "documents/bad_evidence.md")
        support.assert_errors(
            self, doc, [("evidence", "bad_format"), ("evidence", "bad_format"), ("evidence", "bad_format")]
        )

    def test_id_bad_format(self):
        self.write("documents/upper_id.md", note_doc(id='"01ARZ3NDEKTSV4RRFFQ69G5FAV"'))
        self.write("documents/u_id.md", note_doc(id='"01arz3ndektsv4rrffq69g5fau"'))
        _proc, report = self.validate(expect_paths={"documents/upper_id.md", "documents/u_id.md"})
        support.assert_errors(self, support.doc_by_path(report, "documents/upper_id.md"), [("id", "bad_format")])
        support.assert_errors(self, support.doc_by_path(report, "documents/u_id.md"), [("id", "bad_format")])

    def test_needs_review_bad_format(self):
        text = note_doc(needs_review=("created", "created", "reviewed", "summary", "kind", "origin"))
        self.write("documents/dup_needs_review.md", text)
        self.write("documents/empty_origin_list.md", note_doc(origin="[]"))

        _proc, report = self.validate(
            expect_paths={"documents/dup_needs_review.md", "documents/empty_origin_list.md"}
        )
        support.assert_errors(
            self, support.doc_by_path(report, "documents/dup_needs_review.md"), [("needs_review", "bad_format")]
        )
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/empty_origin_list.md"),
            [("origin", "bad_format"), ("needs_review", "needs_review_mismatch")],
        )

    def test_origin_entry_missing_ref_is_bad_format(self):
        # An origin list entry without ref is bad_format, never silently
        # accepted.
        self.write("documents/origin_no_ref.md", note_doc(origin='\n  - retrieved: "2026-01-01"'))
        _proc, report = self.validate(expect_paths={"documents/origin_no_ref.md"})
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/origin_no_ref.md"),
            [("origin", "bad_format"), ("needs_review", "needs_review_mismatch")],
        )

    def test_three_faults_one_file(self):
        text = note_doc(status='"weird"', evidence='["ftp://bad"]', id='"TOO-SHORT"')
        self.write("documents/three_faults.md", text)
        _proc, report = self.validate(expect_paths={"documents/three_faults.md"})
        doc = support.doc_by_path(report, "documents/three_faults.md")
        support.assert_errors(
            self, doc, [("status", "not_allowed"), ("evidence", "bad_format"), ("id", "bad_format")]
        )


class TestUncertaintyAndNeedsReview(TempBrainTestCase):
    def test_valid_uncertainty_combinations(self):
        self.write("documents/all_uncertain.md", note_doc())

        shuffled = note_doc(needs_review=("origin", "kind", "created", "summary", "reviewed"))
        self.write("documents/shuffled.md", shuffled)

        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"document"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '\n  - ref: "https://example.com/a"\n    retrieved: "2026-01-01"',
            "evidence": "[]",
            "kind": '"source"',
            "authored_by": '"human"',
            "retention": '"durable"',
            "source_identity": '"https://example.com/a"',
            "original": '"original/a.md"',
            "original_sha256": '"%s"' % ("a" * 64),
        }
        known_origin_text = make_frontmatter(fields, needs_review=("created", "reviewed", "summary")) + "body\n"
        self.write("documents/known_origin_empty_created.md", known_origin_text)

        _proc, report = self.validate(
            expect_paths={
                "documents/all_uncertain.md",
                "documents/shuffled.md",
                "documents/known_origin_empty_created.md",
            }
        )
        for path in ("documents/all_uncertain.md", "documents/shuffled.md", "documents/known_origin_empty_created.md"):
            doc = support.doc_by_path(report, path)
            self.assertEqual(doc["errors"], [], "{}: {!r}".format(path, doc["errors"]))

    def test_unclassified_type_and_empty_retrieved_are_uncertain(self):
        # type: unclassified is itself an uncertainty value (C5) -- it must be
        # disclosed in needs_review like any other uncertain field.
        self.write(
            "documents/unclassified_disclosed.md",
            note_doc(type='"unclassified"', needs_review=("created", "reviewed", "summary", "kind", "origin", "type")),
        )
        self.write("documents/unclassified_undisclosed.md", note_doc(type='"unclassified"'))

        # An origin entry with an empty retrieved date is uncertain even when
        # ref is a resolvable id/URL.
        fields_empty_retrieved = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"note"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '\n  - ref: "https://example.com/a"\n    retrieved:',
            "evidence": "[]",
            "kind": '"unknown"',
            "authored_by": '"human"',
            "retention": '"durable"',
        }
        self.write(
            "documents/empty_retrieved_disclosed.md",
            make_frontmatter(fields_empty_retrieved, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )
        self.write(
            "documents/empty_retrieved_undisclosed.md",
            make_frontmatter(fields_empty_retrieved, needs_review=("created", "reviewed", "summary", "kind")) + "body\n",
        )

        _proc, report = self.validate(
            expect_paths={
                "documents/unclassified_disclosed.md",
                "documents/unclassified_undisclosed.md",
                "documents/empty_retrieved_disclosed.md",
                "documents/empty_retrieved_undisclosed.md",
            }
        )
        self.assertEqual(support.doc_by_path(report, "documents/unclassified_disclosed.md")["errors"], [])
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/unclassified_undisclosed.md"),
            [("needs_review", "needs_review_mismatch")],
        )
        self.assertEqual(support.doc_by_path(report, "documents/empty_retrieved_disclosed.md")["errors"], [])
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/empty_retrieved_undisclosed.md"),
            [("needs_review", "needs_review_mismatch")],
        )

    def test_wiki_page_type_in_wiki_zone(self):
        # wiki-page's zones must stay ["wiki"], not the whole-brain wildcard
        # ["*"]: a wiki-page document outside wiki/ is wrong_zone.
        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"wiki-page"',
            "title": '"Sample"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"synthesis"',
            "authored_by": '"human"',
            "retention": '"durable"',
        }
        text = make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "origin")) + "body\n"
        self.write("wiki/page.md", text)
        self.write("documents/misplaced_wiki_page.md", text)

        _proc, report = self.validate(expect_paths={"wiki/page.md", "documents/misplaced_wiki_page.md"})
        self.assertEqual(support.doc_by_path(report, "wiki/page.md")["errors"], [])
        support.assert_errors(
            self, support.doc_by_path(report, "documents/misplaced_wiki_page.md"), [("type", "wrong_zone")]
        )

    def test_needs_review_mismatch_cases(self):
        self.write("documents/missing_origin_marker.md", note_doc(needs_review=("created", "reviewed", "summary", "kind")))
        self.write(
            "documents/extra_field.md",
            note_doc(needs_review=("created", "reviewed", "summary", "kind", "origin", "title")),
        )
        self.write(
            "documents/fresh_until_listed.md",
            note_doc(fresh_until='"2026-01-01"', needs_review=("created", "reviewed", "summary", "kind", "origin", "fresh_until")),
        )
        self.write("documents/list_absent.md", note_doc(needs_review=()))

        _proc, report = self.validate(
            expect_paths={
                "documents/missing_origin_marker.md",
                "documents/extra_field.md",
                "documents/fresh_until_listed.md",
                "documents/list_absent.md",
            }
        )
        for path in (
            "documents/missing_origin_marker.md",
            "documents/extra_field.md",
            "documents/fresh_until_listed.md",
            "documents/list_absent.md",
        ):
            doc = support.doc_by_path(report, path)
            support.assert_errors(self, doc, [("needs_review", "needs_review_mismatch")], path)


class TestCustomTypesBothWays(TempBrainTestCase):
    RECIPE_DEF = json.dumps(
        {
            "type": "recipe",
            "zones": ["*"],
            "required": [],
            "allowed_values": {"cuisine": ["italian", "french"]},
            "uncertainty_values": {"cuisine": None},
            "display_fields": ["cuisine"],
            "search_fields": ["cuisine"],
        }
    )

    RECIPE_REQUIRED_DEF = json.dumps(
        {
            "type": "recipe",
            "zones": ["*"],
            "required": ["cuisine"],
            "allowed_values": {"cuisine": ["italian", "french"]},
            "uncertainty_values": {"cuisine": None},
            "display_fields": ["cuisine"],
            "search_fields": ["cuisine"],
        }
    )

    RECIPE_EMPTY_STRING_MARKER_DEF = json.dumps(
        {
            "type": "recipe",
            "zones": ["*"],
            "required": [],
            "allowed_values": {"cuisine": ["italian", "french"]},
            "uncertainty_values": {"cuisine": ""},
            "display_fields": ["cuisine"],
            "search_fields": ["cuisine"],
        }
    )

    RECIPE_NO_UNCERTAINTY_DEF = json.dumps(
        {
            "type": "recipe",
            "zones": ["*"],
            "required": [],
            "allowed_values": {"cuisine": ["italian", "french"]},
            "display_fields": ["cuisine"],
            "search_fields": ["cuisine"],
        }
    )

    def _recipe_fields(self, **overrides):
        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"recipe"',
            "title": '"Pasta"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"unknown"',
            "authored_by": '"human"',
            "retention": '"durable"',
        }
        fields.update(overrides)
        return fields

    def test_null_marker_present_empty_is_uncertain(self):
        # cuisine: (present, explicit empty value) is the uncertainty marker
        # itself; a cuisine that is altogether absent from the frontmatter is
        # missing/not-uncertain instead -- the two are never conflated.
        self.write(".brain/types/custom/recipe.json", self.RECIPE_DEF)
        fields = self._recipe_fields(cuisine=None)
        self.write(
            "documents/cuisine_present_empty_disclosed.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin", "cuisine")) + "body\n",
        )
        self.write(
            "documents/cuisine_present_empty_undisclosed.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )

        _proc, report = self.validate(
            expect_paths={
                "documents/cuisine_present_empty_disclosed.md",
                "documents/cuisine_present_empty_undisclosed.md",
            }
        )
        self.assertEqual(support.doc_by_path(report, "documents/cuisine_present_empty_disclosed.md")["errors"], [])
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/cuisine_present_empty_undisclosed.md"),
            [("needs_review", "needs_review_mismatch")],
        )

    def test_required_field_with_null_marker_present_empty_is_not_missing(self):
        self.write(".brain/types/custom/recipe.json", self.RECIPE_REQUIRED_DEF)
        fields = self._recipe_fields(cuisine=None)
        self.write(
            "documents/required_cuisine_empty.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin", "cuisine")) + "body\n",
        )
        _proc, report = self.validate(expect_paths={"documents/required_cuisine_empty.md"})
        doc = support.doc_by_path(report, "documents/required_cuisine_empty.md")
        self.assertEqual(doc["errors"], [])

    def test_required_field_absent_is_missing_only(self):
        # cuisine is altogether absent (never written), not present with the
        # marker value -- that is exactly "missing", and absence is never
        # itself uncertain, so no needs_review error is added alongside it.
        self.write(".brain/types/custom/recipe.json", self.RECIPE_REQUIRED_DEF)
        fields = self._recipe_fields()
        self.write(
            "documents/required_cuisine_absent.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )
        _proc, report = self.validate(expect_paths={"documents/required_cuisine_absent.md"})
        doc = support.doc_by_path(report, "documents/required_cuisine_absent.md")
        support.assert_errors(self, doc, [("cuisine", "missing")])

    def test_empty_string_marker_is_uncertain(self):
        # An explicit "" uncertainty marker behaves exactly like a null
        # marker: present-and-empty is uncertain, absent is missing/not-
        # uncertain, and neither is silently dropped.
        self.write(".brain/types/custom/recipe.json", self.RECIPE_EMPTY_STRING_MARKER_DEF)
        fields = self._recipe_fields(cuisine='""')
        self.write(
            "documents/cuisine_empty_string_marker_disclosed.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin", "cuisine")) + "body\n",
        )
        self.write(
            "documents/cuisine_empty_string_marker_undisclosed.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )
        _proc, report = self.validate(
            expect_paths={
                "documents/cuisine_empty_string_marker_disclosed.md",
                "documents/cuisine_empty_string_marker_undisclosed.md",
            }
        )
        self.assertEqual(
            support.doc_by_path(report, "documents/cuisine_empty_string_marker_disclosed.md")["errors"], []
        )
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/cuisine_empty_string_marker_undisclosed.md"),
            [("needs_review", "needs_review_mismatch")],
        )

    def test_definition_without_uncertainty_values_is_valid(self):
        self.write(".brain/types/custom/recipe.json", self.RECIPE_NO_UNCERTAINTY_DEF)
        fields = self._recipe_fields(cuisine='"italian"')
        self.write(
            "documents/recipe_no_uncertainty_key.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )
        _proc, report = self.validate(expect_paths={"documents/recipe_no_uncertainty_key.md"})
        self.assertEqual(report["definitions"], [])
        doc = support.doc_by_path(report, "documents/recipe_no_uncertainty_key.md")
        self.assertEqual(doc["status"], "valid", doc["errors"])

    def test_recipe_type_valid_missing_not_allowed(self):
        self.write(".brain/types/custom/recipe.json", self.RECIPE_DEF)

        valid_fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"recipe"',
            "title": '"Pasta"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"unknown"',
            "authored_by": '"human"',
            "retention": '"durable"',
            "cuisine": '"italian"',
        }
        self.write(
            "documents/recipe_valid.md",
            make_frontmatter(valid_fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )

        # cuisine is optional here, and simply absent -- absence is never
        # uncertainty, so disclosing it in needs_review is itself the mismatch,
        # and leaving an absent optional field undisclosed is valid.
        missing_fields = dict(valid_fields)
        del missing_fields["cuisine"]
        self.write(
            "documents/recipe_missing_cuisine_disclosed.md",
            make_frontmatter(missing_fields, needs_review=("created", "reviewed", "summary", "kind", "origin", "cuisine")) + "body\n",
        )
        self.write(
            "documents/recipe_missing_cuisine_undisclosed.md",
            make_frontmatter(missing_fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )

        not_allowed_fields = dict(valid_fields)
        not_allowed_fields["cuisine"] = '"klingon"'
        self.write(
            "documents/recipe_bad_cuisine.md",
            make_frontmatter(not_allowed_fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )

        _proc, report = self.validate(
            expect_paths={
                "documents/recipe_valid.md",
                "documents/recipe_missing_cuisine_disclosed.md",
                "documents/recipe_missing_cuisine_undisclosed.md",
                "documents/recipe_bad_cuisine.md",
            }
        )
        self.assertEqual(support.doc_by_path(report, "documents/recipe_valid.md")["errors"], [])
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/recipe_missing_cuisine_disclosed.md"),
            [("needs_review", "needs_review_mismatch")],
        )
        self.assertEqual(support.doc_by_path(report, "documents/recipe_missing_cuisine_undisclosed.md")["errors"], [])
        support.assert_errors(
            self, support.doc_by_path(report, "documents/recipe_bad_cuisine.md"), [("cuisine", "not_allowed")]
        )

    def test_type_unknown_without_definition_file(self):
        fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"recipe"',
            "title": '"Pasta"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"unknown"',
            "authored_by": '"human"',
            "retention": '"durable"',
            "cuisine": '"italian"',
        }
        self.write(
            "documents/recipe_no_def.md",
            make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
        )
        _proc, report = self.validate(expect_paths={"documents/recipe_no_def.md"})
        doc = support.doc_by_path(report, "documents/recipe_no_def.md")
        support.assert_errors(self, doc, [("type", "unknown_type")])

    def test_unresolved_type_with_consistent_base_fields_reports_only_type_error(self):
        # When type does not resolve (unknown, or absent entirely) and every
        # base-uncertainty field is correctly disclosed, the type error is
        # the only error reported: a needs_review entry naming a field only a
        # type definition could declare (here, "cuisine") is not evaluated --
        # neither required nor reported as extraneous.
        # TestMergeAndUncertaintyRegressions.test_unresolved_type_needs_review
        # covers the base-field mismatch cases this ruling also governs.
        unknown_fields = {
            "kb": "1",
            "id": '"01arz3ndektsv4rrffq69g5fav"',
            "type": '"recipe"',
            "title": '"Pasta"',
            "summary": '""',
            "status": '"draft"',
            "created": None,
            "reviewed": None,
            "origin": '"unknown"',
            "evidence": "[]",
            "kind": '"unknown"',
            "authored_by": '"human"',
            "retention": '"durable"',
        }
        self.write(
            "documents/unknown_type_with_type_field_disclosed.md",
            make_frontmatter(
                unknown_fields, needs_review=("created", "reviewed", "summary", "kind", "origin", "cuisine")
            )
            + "body\n",
        )

        absent_type_fields = dict(unknown_fields)
        del absent_type_fields["type"]
        self.write(
            "documents/absent_type_with_type_field_disclosed.md",
            make_frontmatter(
                absent_type_fields, needs_review=("created", "reviewed", "summary", "kind", "origin", "cuisine")
            )
            + "body\n",
        )

        _proc, report = self.validate(
            expect_paths={
                "documents/unknown_type_with_type_field_disclosed.md",
                "documents/absent_type_with_type_field_disclosed.md",
            }
        )
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/unknown_type_with_type_field_disclosed.md"),
            [("type", "unknown_type")],
        )
        support.assert_errors(
            self,
            support.doc_by_path(report, "documents/absent_type_with_type_field_disclosed.md"),
            [("type", "missing")],
        )

    def test_definition_errors(self):
        # This file both lacks required keys and reuses the base type name.
        # C5a states no multiplicity or precedence between invalid_definition
        # and shadows_type; the expected collection pins the current
        # behaviour, where the file's shape is checked first and a failed
        # custom definition file reports exactly one definitions entry.
        self.write(".brain/types/custom/note.json", json.dumps({"type": "note", "zones": ["*"]}))
        self.write("documents/base_note.md", note_doc())

        proc, report = self.validate(expect_paths={"documents/base_note.md"})
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(report["outcome"], "invalid")
        self.assertEqual(
            collections.Counter((d["path"], d["code"]) for d in report["definitions"]),
            collections.Counter([(".brain/types/custom/note.json", "invalid_definition")]),
        )
        base_note_doc = support.doc_by_path(report, "documents/base_note.md")
        self.assertEqual(base_note_doc["status"], "valid")

    def test_shadows_type_explicit(self):
        self.write(
            ".brain/types/custom/note.json",
            json.dumps(
                {
                    "type": "note",
                    "zones": ["*"],
                    "required": [],
                    "allowed_values": {},
                    "uncertainty_values": {},
                    "display_fields": [],
                    "search_fields": [],
                }
            ),
        )
        self.write("documents/base_note.md", note_doc())
        _proc, report = self.validate(expect_paths={"documents/base_note.md"})
        self.assertEqual(
            collections.Counter((d["path"], d["code"]) for d in report["definitions"]),
            collections.Counter([(".brain/types/custom/note.json", "shadows_type")]),
        )
        self.assertEqual(support.doc_by_path(report, "documents/base_note.md")["status"], "valid")

    def test_name_mismatch_and_dual_unknown(self):
        self.write(
            ".brain/types/custom/recipe.json",
            json.dumps(
                {
                    "type": "dish",
                    "zones": ["*"],
                    "required": [],
                    "allowed_values": {},
                    "uncertainty_values": {},
                    "display_fields": [],
                    "search_fields": [],
                }
            ),
        )
        for type_name in ("recipe", "dish"):
            fields = {
                "kb": "1",
                "id": '"01arz3ndektsv4rrffq69g5fav"',
                "type": f'"{type_name}"',
                "title": '"x"',
                "summary": '""',
                "status": '"draft"',
                "created": None,
                "reviewed": None,
                "origin": '"unknown"',
                "evidence": "[]",
                "kind": '"unknown"',
                "authored_by": '"human"',
                "retention": '"durable"',
            }
            self.write(
                f"documents/{type_name}_doc.md",
                make_frontmatter(fields, needs_review=("created", "reviewed", "summary", "kind", "origin")) + "body\n",
            )
        _proc, report = self.validate(
            expect_paths={"documents/recipe_doc.md", "documents/dish_doc.md"}
        )
        self.assertEqual(
            collections.Counter((d["path"], d["code"]) for d in report["definitions"]),
            collections.Counter([(".brain/types/custom/recipe.json", "name_mismatch")]),
        )
        for type_name in ("recipe", "dish"):
            doc = support.doc_by_path(report, f"documents/{type_name}_doc.md")
            support.assert_errors(self, doc, [("type", "unknown_type")])

    def test_malformed_definition(self):
        self.write(".brain/types/custom/broken.json", "{not valid json")
        self.write("documents/base_note.md", note_doc())
        _proc, report = self.validate(expect_paths={"documents/base_note.md"})
        self.assertEqual(
            collections.Counter((d["path"], d["code"]) for d in report["definitions"]),
            collections.Counter([(".brain/types/custom/broken.json", "malformed_definition")]),
        )


class TestBaseTypesReadBesideCode(TempBrainTestCase):
    def test_tampered_brain_side_base_type_ignored(self):
        self.write(
            ".brain/types/base/note.json",
            json.dumps(
                {
                    "type": "note",
                    "zones": [],
                    "required": ["nonsense"],
                    "allowed_values": {},
                    "uncertainty_values": {},
                    "display_fields": [],
                    "search_fields": [],
                }
            ),
        )
        self.write("documents/note.md", note_doc())
        _proc, report = self.validate(expect_paths={"documents/note.md"})
        doc = support.doc_by_path(report, "documents/note.md")
        self.assertEqual(doc["status"], "valid", doc["errors"])

    def test_runs_from_copied_layout_and_different_cwd(self):
        copy_root = tempfile.mkdtemp(prefix="brain-copy-")
        self.addCleanup(shutil.rmtree, copy_root, ignore_errors=True)
        for name in ("bin", "brain_core", "vendor", "types"):
            src = os.path.join(support.REPO_ROOT, name)
            dst = os.path.join(copy_root, name)
            shutil.copytree(src, dst)

        brain_root = tempfile.mkdtemp(prefix="brain-data-")
        self.addCleanup(shutil.rmtree, brain_root, ignore_errors=True)
        support.write_file(brain_root, "documents/note.md", note_doc())

        other_cwd = tempfile.mkdtemp(prefix="other-cwd-")
        self.addCleanup(shutil.rmtree, other_cwd, ignore_errors=True)

        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-B", "-S", "-E", os.path.join(copy_root, "bin", "brain"), "validate", "--root", brain_root],
            cwd=other_cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report["outcome"], "valid")

    def test_base_allowed_values_come_from_json_not_python(self):
        # status's allowed values are read from the base type JSON, not
        # hard-coded -- adding a value to one base type file's list makes it
        # a valid status everywhere, since the base allowed values are a
        # union across every base type file.
        copy_root = tempfile.mkdtemp(prefix="brain-status-")
        self.addCleanup(shutil.rmtree, copy_root, ignore_errors=True)
        for name in ("bin", "brain_core", "vendor", "types"):
            src = os.path.join(support.REPO_ROOT, name)
            dst = os.path.join(copy_root, name)
            shutil.copytree(src, dst)

        note_type_path = os.path.join(copy_root, "types", "base", "note.json")
        with open(note_type_path, "r", encoding="utf-8") as fh:
            note_type = json.load(fh)
        note_type["allowed_values"]["status"].append("archived")
        with open(note_type_path, "w", encoding="utf-8") as fh:
            json.dump(note_type, fh)

        brain_root = tempfile.mkdtemp(prefix="brain-data-")
        self.addCleanup(shutil.rmtree, brain_root, ignore_errors=True)
        support.write_file(brain_root, "documents/note.md", note_doc(status='"archived"'))

        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-B", "-S", "-E", os.path.join(copy_root, "bin", "brain"), "validate", "--root", brain_root],
            capture_output=True,
            text=True,
            check=False,
        )
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report["outcome"], "valid")


class TestVendoredYamlRecord(TempBrainTestCase):
    EXPECTED_HASHES: ClassVar[dict] = {
        "vendor/yaml/__init__.py": "b19dfcc333d6a75dfd73073901164507252f271b41d3b5f7d85510033a0547a7",
        "vendor/yaml/composer.py": "fcaa37d16afa783594794a5ab94193dcb720f503c19ce3d59539c8311189f453",
        "vendor/yaml/constructor.py": "90d8247da78b524c10618fd0e857f54f3d97570fe91b5c5513d024ef3faf88b0",
        "vendor/yaml/cyaml.py": "e99ac01bd7c062f7557b614aff0d21997a06ed962ca185306a91bc0a20bbd87d",
        "vendor/yaml/dumper.py": "3cb72d66563064ba7b5e679477046ebf89d8399d940670c8532f3e94a7cb17ea",
        "vendor/yaml/emitter.py": "8e086d694ede170837d5b1b407b45979aff6f40762f422a65eafd08e04290a44",
        "vendor/yaml/error.py": "021f73fada072546c4f63f8cf18a7181244ce4280b09cc15cc980b2d1176171a",
        "vendor/yaml/events.py": "e74fd392c810884e2ea7e94aa3f57e9c1cbeb402319083d0c58e6a0e1282787c",
        "vendor/yaml/loader.py": "5156becc8aa6905482218abf3e04869b835226db4763645fff3438fdbd5f1cdd",
        "vendor/yaml/nodes.py": "80f28d8fca4a09d87677882bde021820d9cf39a3b11a12405226211919cf13ce",
        "vendor/yaml/parser.py": "8a55a9e6fbe0a07146cef3990c8b45a068c3e83e369e1959ad9ca30306b4a09a",
        "vendor/yaml/reader.py": "d1d9b38ab3a20c6e17a38d519ee412ecaf6b918df18c78956ac7c330d4ea08dc",
        "vendor/yaml/representer.py": "22e58ff9c016f6c1ca1274b4802a926bcf78935060e1c813c5a0f021c6d143e6",
        "vendor/yaml/resolver.py": "f4bf9561f9b89961f1503d558385fbae30d12bfed565de9bf76c33abb63620a6",
        "vendor/yaml/scanner.py": "60433788b652690c17710460da5d91e0c753d3318fd85f5e1e42862a71f25906",
        "vendor/yaml/serializer.py": "0a1b85826854d35863e31808f0668abfabdf33606e8f06bd8bb7761401e3edc0",
        "vendor/yaml/tokens.py": "953408cd2570f0c83dc2fe39f7e4e388e41eeb05738aa69196a5f6ffcf6ba79e",
        "vendor/PyYAML-LICENSE": "8d3928f9dc4490fd635707cb88eb26bd764102a7282954307d3e5167a577e8a4",
    }

    def test_vendored_yaml_record(self):
        all_files = os.listdir(os.path.join(support.REPO_ROOT, "vendor", "yaml"))
        # All 17 vendored files are .py -- a stray extra file of any kind
        # (not just a stray .py) must fail this.
        self.assertEqual(len(all_files), 17)
        self.assertTrue(all(f.endswith(".py") for f in all_files), all_files)

        vendored_md = os.path.join(support.REPO_ROOT, "vendor", "VENDORED.md")
        with open(vendored_md, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("PyYAML 6.0.3", content)
        self.assertIn("d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f", content)
        self.assertIn("https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz", content)

        for rel_path, expected_hash in self.EXPECTED_HASHES.items():
            full = os.path.join(support.REPO_ROOT, rel_path)
            with open(full, "rb") as fh:
                actual_hash = hashlib.sha256(fh.read()).hexdigest()
            self.assertEqual(actual_hash, expected_hash, rel_path)
            self.assertIn(expected_hash, content, rel_path)


class TestVendoredYamlIsTheOneLoaded(TempBrainTestCase):
    def test_decoy_package_is_not_used(self):
        copy_root = tempfile.mkdtemp(prefix="brain-decoy-")
        self.addCleanup(shutil.rmtree, copy_root, ignore_errors=True)
        for name in ("bin", "brain_core", "vendor", "types"):
            src = os.path.join(support.REPO_ROOT, name)
            dst = os.path.join(copy_root, name)
            shutil.copytree(src, dst)

        decoy_dir = os.path.join(copy_root, "bin", "yaml")
        os.makedirs(decoy_dir)
        with open(os.path.join(decoy_dir, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("__version__ = '0.0-decoy'\nraise RuntimeError('decoy yaml was imported')\n")

        brain_root = tempfile.mkdtemp(prefix="brain-data-")
        self.addCleanup(shutil.rmtree, brain_root, ignore_errors=True)
        support.write_file(brain_root, "documents/note.md", note_doc())

        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-B", os.path.join(copy_root, "bin", "brain"), "validate", "--root", brain_root],
            capture_output=True,
            text=True,
            check=False,
        )
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(report["outcome"], "valid")

    def test_altered_vendor_version_errors(self):
        copy_root = tempfile.mkdtemp(prefix="brain-altered-")
        self.addCleanup(shutil.rmtree, copy_root, ignore_errors=True)
        for name in ("bin", "brain_core", "vendor", "types"):
            src = os.path.join(support.REPO_ROOT, name)
            dst = os.path.join(copy_root, name)
            shutil.copytree(src, dst)

        init_path = os.path.join(copy_root, "vendor", "yaml", "__init__.py")
        with open(init_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        content = content.replace("__version__ = '6.0.3'", "__version__ = '6.0.3-altered'")
        with open(init_path, "w", encoding="utf-8") as fh:
            fh.write(content)

        brain_root = tempfile.mkdtemp(prefix="brain-data-")
        self.addCleanup(shutil.rmtree, brain_root, ignore_errors=True)
        support.write_file(brain_root, "documents/note.md", note_doc())

        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-B", "-S", "-E", os.path.join(copy_root, "bin", "brain"), "validate", "--root", brain_root],
            capture_output=True,
            text=True,
            check=False,
        )
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(report, {"outcome": "error", "reason": "vendored_dependency"})

    def test_missing_vendor_yaml_errors_instead_of_crashing(self):
        copy_root = tempfile.mkdtemp(prefix="brain-missing-vendor-")
        self.addCleanup(shutil.rmtree, copy_root, ignore_errors=True)
        for name in ("bin", "brain_core", "types"):
            src = os.path.join(support.REPO_ROOT, name)
            dst = os.path.join(copy_root, name)
            shutil.copytree(src, dst)
        os.makedirs(os.path.join(copy_root, "vendor"))  # vendor/yaml itself is absent

        brain_root = tempfile.mkdtemp(prefix="brain-data-")
        self.addCleanup(shutil.rmtree, brain_root, ignore_errors=True)
        support.write_file(brain_root, "documents/note.md", note_doc())

        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-B", "-S", "-E", os.path.join(copy_root, "bin", "brain"), "validate", "--root", brain_root],
            capture_output=True,
            text=True,
            check=False,
        )
        report = support.parse_single_json(proc.stdout)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(report, {"outcome": "error", "reason": "vendored_dependency"})


class TestNoGitAndNoWrites(TempBrainTestCase):
    def test_output_identical_without_git_and_with_git_history(self):
        self.write("documents/note.md", note_doc())

        env_no_git = dict(os.environ)
        env_no_git["PATH"] = ""
        proc1 = support.run_brain(["validate", "--root", self.tmpdir], env=env_no_git)
        report1 = support.parse_single_json(proc1.stdout)

        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=self.tmpdir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.tmpdir, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.tmpdir, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.tmpdir, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.tmpdir, check=True)

        proc2 = support.run_brain(["validate", "--root", self.tmpdir])
        report2 = support.parse_single_json(proc2.stdout)

        self.assertEqual(report1, report2)

        self.assertEqual(support.find_bytecode(self.tmpdir), [])
        self.assertEqual(support.find_bytecode(support.REPO_ROOT), [])


class TestMergeAndUncertaintyRegressions(TempBrainTestCase):
    """Merge-key and uncertainty-marker regression coverage: own-key-vs-
    merged-key resolution is decided by position, not node identity;
    duplicate keys inside an inline merge source are rejected exactly like
    any other mapping's own duplicate keys; the null and "" uncertainty
    markers behave identically; and needs_review evaluation of base fields
    against an unresolved type follows the literal reading of C5a's "only
    that error is reported for type-dependent rules"."""

    _OTHER_VALID_FIELDS: ClassVar[list] = [
        'id: "01arz3ndektsv4rrffq69g5fav"',
        'type: "note"',
        'summary: "S"',
        'created: "2026-01-01"',
        'reviewed: "2026-01-01"',
        'origin: "authored"',
        'evidence: []',
        'kind: "source"',
        'authored_by: "human"',
        'retention: "durable"',
    ]

    def _doc(self, extra_lines, needs_review=()):
        """A complete managed document: kb: 1, extra_lines (which must supply
        title and status themselves, directly or via merge), and every other
        required field holding an ordinary valid value."""
        lines = ["---", "kb: 1"] + list(extra_lines) + list(self._OTHER_VALID_FIELDS)
        if needs_review:
            lines.append("needs_review:")
            for item in needs_review:
                lines.append(f'  - "{item}"')
        lines.append("---")
        return "\n".join(lines) + "\nbody\n"

    def _assert_case(self, path, text, expected_errors):
        self.write(path, text)
        _proc, report = self.validate(expect_paths={path})
        support.assert_errors(self, support.doc_by_path(report, path), expected_errors, path)

    def _assert_cases(self, cases):
        """Writes every (path, text, expected_errors) case, validates once
        against the full set (so earlier cases in a multi-case test do not
        leak into a later case's expect_paths), then checks each doc."""
        for path, text, _expected in cases:
            self.write(path, text)
        _proc, report = self.validate(expect_paths={path for path, _text, _expected in cases})
        for path, _text, expected in cases:
            support.assert_errors(self, support.doc_by_path(report, path), expected, path)

    # Explicit keys override merged keys, decided by position not node identity.

    def test_explicit_key_overrides_merged_key_top_level(self):
        self._assert_case(
            "documents/r1_top_level_override.md",
            self._doc(
                [
                    'title: "T"',
                    "defaults: &d",
                    '  status: "bogus"',
                    'status: "draft"',
                    "<<: *d",
                ]
            ),
            [],
        )

    def test_explicit_key_overrides_merged_key_top_level_reverse(self):
        self._assert_case(
            "documents/r1_top_level_override_reverse.md",
            self._doc(
                [
                    'title: "T"',
                    "defaults: &d",
                    '  status: "draft"',
                    'status: "bogus"',
                    "<<: *d",
                ]
            ),
            [("status", "not_allowed")],
        )

    def test_explicit_key_overrides_merged_key_nested_chain_and_flow(self):
        # Exercises a multi-level merge chain (top merges from wrapper, which
        # itself merges from base) and a flow-style inline merge source
        # ({c: 2, <<: *b}) nested under an unused key -- both must resolve
        # without disturbing the top-level explicit override.
        self._assert_case(
            "documents/r1_nested_chain_and_flow.md",
            self._doc(
                [
                    'title: "T"',
                    "base: &b",
                    '  status: "bogus"',
                    "wrapper: &w",
                    "  <<: *b",
                    "extra: {c: 2, <<: *b}",
                    'status: "draft"',
                    "<<: *w",
                ]
            ),
            [],
        )

    def test_explicit_key_overrides_merged_key_aliased(self):
        # The explicit key is a literal alias (*st) of the same scalar node
        # used inside the merge source -- own-vs-merged is decided by the key
        # pair's position in the mapping, never by node identity, so this is
        # still a clean override, not a duplicate.
        self._assert_case(
            "documents/r1_aliased_override.md",
            self._doc(
                [
                    'title: "T"',
                    '&st status: "draft"',
                    'defaults: &d {*st : "bogus"}',
                    "<<: *d",
                ]
            ),
            [],
        )

    def test_explicit_key_overrides_merged_key_aliased_reverse(self):
        self._assert_case(
            "documents/r1_aliased_override_reverse.md",
            self._doc(
                [
                    'title: "T"',
                    '&st status: "bogus"',
                    'defaults: &d {*st : "draft"}',
                    "<<: *d",
                ]
            ),
            [("status", "not_allowed")],
        )

    # Duplicate keys inside a merge source are rejected wherever the
    # source appears, including inline (never separately constructed).

    def test_duplicate_keys_inside_inline_merge_source_top_level(self):
        self._assert_case(
            "documents/r2_inline_duplicate_top_level.md",
            self._doc(['title: "T"', '<<: {status: "bogus", status: "draft"}']),
            [(None, "malformed")],
        )

    def test_duplicate_keys_inside_inline_merge_source_sequence(self):
        self._assert_case(
            "documents/r2_inline_duplicate_sequence.md",
            self._doc(['title: "T"', 'status: "draft"', "<<: [{c: 1, c: 2}]"]),
            [(None, "malformed")],
        )

    def test_duplicate_keys_inside_inline_merge_source_nested_under_key(self):
        self._assert_case(
            "documents/r2_inline_duplicate_nested.md",
            self._doc(
                [
                    'title: "T"',
                    'status: "draft"',
                    "extra:",
                    '  <<: {status: "bogus", status: "draft"}',
                ]
            ),
            [(None, "malformed")],
        )

    def test_duplicate_keys_inside_anchored_merge_source(self):
        self._assert_case(
            "documents/r2_anchored_duplicate.md",
            self._doc(
                [
                    'title: "T"',
                    'd: &d {status: "bogus", status: "draft"}',
                    "<<: *d",
                ]
            ),
            [(None, "malformed")],
        )

    def test_inline_merge_source_without_duplicates_is_not_malformed(self):
        self._assert_case(
            "documents/r2_control_no_duplicates.md",
            self._doc(['title: "T"', '<<: {status: "draft"}']),
            [],
        )

    def test_two_merge_sources_sharing_a_key_is_not_malformed(self):
        self._assert_case(
            "documents/r2_control_shared_key.md",
            self._doc(
                [
                    'title: "T"',
                    'b1: &b1 {status: "bogus"}',
                    'b2: &b2 {status: "draft"}',
                    "<<: [*b1, *b2]",
                ]
            ),
            [("status", "not_allowed")],
        )

    # Merged fields are preserved with their correct values when not
    # explicitly overridden.

    def test_merged_field_supplies_a_missing_field(self):
        self._assert_case(
            "documents/r3_merged_title_supplies_field.md",
            self._doc(
                [
                    'status: "draft"',
                    "defaults: &d",
                    '  title: "Merged Title"',
                    "<<: *d",
                ]
            ),
            [],
        )

    def test_merged_field_not_overridden_keeps_its_value_not_allowed(self):
        self._assert_case(
            "documents/r3_merged_status_not_overridden.md",
            self._doc(
                [
                    'title: "T"',
                    "defaults: &d",
                    '  status: "bogus"',
                    "<<: *d",
                ]
            ),
            [("status", "not_allowed")],
        )

    def test_merged_field_not_overridden_keeps_its_value_ambiguous(self):
        self._assert_case(
            "documents/r3_merged_title_not_overridden.md",
            self._doc(
                [
                    'status: "draft"',
                    "defaults: &d",
                    "  title: 12",
                    "<<: *d",
                ]
            ),
            [("title", "ambiguous_scalar")],
        )

    def test_multi_merge_shared_key_first_source_wins(self):
        self._assert_case(
            "documents/r3_multi_merge_first_wins.md",
            self._doc(
                [
                    'title: "T"',
                    'b1: &b1 {status: "bogus"}',
                    'b2: &b2 {status: "draft"}',
                    "<<: [*b1, *b2]",
                ]
            ),
            [("status", "not_allowed")],
        )

    def test_multi_merge_shared_key_first_source_wins_reversed(self):
        self._assert_case(
            "documents/r3_multi_merge_first_wins_reversed.md",
            self._doc(
                [
                    'title: "T"',
                    'b1: &b1 {status: "bogus"}',
                    'b2: &b2 {status: "draft"}',
                    "<<: [*b2, *b1]",
                ]
            ),
            [],
        )

    # The null and "" uncertainty markers behave identically, for an
    # optional and a required type-declared field.

    WIDGET_OPTIONAL_DEF = json.dumps(
        {
            "type": "widget_optional",
            "zones": ["*"],
            "required": [],
            "allowed_values": {},
            "uncertainty_values": {"alpha": None, "beta": ""},
            "display_fields": [],
            "search_fields": [],
        }
    )
    WIDGET_REQUIRED_DEF = json.dumps(
        {
            "type": "widget_required",
            "zones": ["*"],
            "required": ["alpha", "beta"],
            "allowed_values": {},
            "uncertainty_values": {"alpha": None, "beta": ""},
            "display_fields": [],
            "search_fields": [],
        }
    )

    def _widget_doc(self, type_name, extra_lines, needs_review=()):
        lines = ["---", "kb: 1", f'type: "{type_name}"', 'title: "T"', 'status: "draft"'] + list(extra_lines) + [
            'id: "01arz3ndektsv4rrffq69g5fav"',
            'summary: "S"',
            'created: "2026-01-01"',
            'reviewed: "2026-01-01"',
            'origin: "authored"',
            'evidence: []',
            'kind: "source"',
            'authored_by: "human"',
            'retention: "durable"',
        ]
        if needs_review:
            lines.append("needs_review:")
            for item in needs_review:
                lines.append(f'  - "{item}"')
        lines.append("---")
        return "\n".join(lines) + "\nbody\n"

    def test_null_and_empty_string_markers_are_uncertain(self):
        self.write(".brain/types/custom/widget_optional.json", self.WIDGET_OPTIONAL_DEF)
        self.write(".brain/types/custom/widget_required.json", self.WIDGET_REQUIRED_DEF)

        # present null/tilde/empty-string values are uncertain, whichever
        # marker the field declares, for an optional and a required field.
        present_uncertain_cases = [
            ("r4_alpha_optional_null", "widget_optional", "alpha:"),
            ("r4_alpha_optional_tilde", "widget_optional", "alpha: ~"),
            ("r4_alpha_optional_empty_string", "widget_optional", 'alpha: ""'),
            ("r4_beta_optional_null", "widget_optional", "beta:"),
            ("r4_beta_optional_tilde", "widget_optional", "beta: ~"),
            ("r4_beta_optional_empty_string", "widget_optional", 'beta: ""'),
            ("r4_alpha_required_null", "widget_required", "alpha:"),
            ("r4_alpha_required_empty_string", "widget_required", 'alpha: ""'),
            ("r4_beta_required_null", "widget_required", "beta:"),
            ("r4_beta_required_empty_string", "widget_required", 'beta: ""'),
        ]
        cases = []
        for name, type_name, field_line in present_uncertain_cases:
            field = field_line.split(":")[0]
            other_field = "beta" if field == "alpha" else "alpha"
            other_line = f'{other_field}: "known"'
            cases.append(
                (
                    f"documents/{name}_listed.md",
                    self._widget_doc(type_name, [field_line, other_line], needs_review=(field,)),
                    [],
                )
            )
            cases.append(
                (
                    f"documents/{name}_not_listed.md",
                    self._widget_doc(type_name, [field_line, other_line]),
                    [("needs_review", "needs_review_mismatch")],
                )
            )

        # a non-empty value is not uncertain.
        cases.append(
            (
                "documents/r4_alpha_non_empty_listed.md",
                self._widget_doc("widget_optional", ['alpha: "known"', 'beta: "known"'], needs_review=("alpha",)),
                [("needs_review", "needs_review_mismatch")],
            )
        )
        cases.append(
            (
                "documents/r4_alpha_non_empty_not_listed.md",
                self._widget_doc("widget_optional", ['alpha: "known"', 'beta: "known"']),
                [],
            )
        )

        # absent required -> missing; absent optional -> valid; absent but
        # listed -> mismatch.
        cases.append(
            (
                "documents/r4_required_absent.md",
                self._widget_doc("widget_required", []),
                [("alpha", "missing"), ("beta", "missing")],
            )
        )
        cases.append(("documents/r4_optional_absent.md", self._widget_doc("widget_optional", []), []))
        cases.append(
            (
                "documents/r4_optional_absent_but_listed.md",
                self._widget_doc("widget_optional", [], needs_review=("alpha",)),
                [("needs_review", "needs_review_mismatch")],
            )
        )

        self._assert_cases(cases)

    # Base-field needs_review rules apply even when the type is unresolved;
    # entries naming a non-base field are not evaluated; at most one
    # needs_review_mismatch is ever reported.

    def _unresolved_type_doc(self, type_line, extra_lines, needs_review=()):
        lines = ["---", "kb: 1"]
        if type_line is not None:
            lines.append(type_line)
        lines += [
            'id: "01arz3ndektsv4rrffq69g5fav"',
            'title: "T"',
            'status: "draft"',
        ]
        lines += extra_lines
        if needs_review:
            lines.append("needs_review:")
            for item in needs_review:
                lines.append(f'  - "{item}"')
        lines.append("---")
        return "\n".join(lines) + "\nbody\n"

    def test_unresolved_type_needs_review(self):
        base_valid = [
            'summary: "S"',
            'created: "2026-01-01"',
            'reviewed: "2026-01-01"',
            'origin: "authored"',
            'evidence: []',
            'kind: "source"',
            'authored_by: "human"',
            'retention: "durable"',
        ]

        self._assert_cases(
            [
                # unknown type, empty created not listed -> both the type
                # error and the base-field mismatch are reported.
                (
                    "documents/r5_unknown_type_created_uncertain_not_listed.md",
                    self._unresolved_type_doc(
                        'type: "dish"',
                        ['summary: "S"', "created:"] + base_valid[2:],
                    ),
                    [("type", "unknown_type"), ("needs_review", "needs_review_mismatch")],
                ),
                # unknown type, empty created correctly listed -> only the
                # type error.
                (
                    "documents/r5_unknown_type_created_uncertain_listed.md",
                    self._unresolved_type_doc(
                        'type: "dish"',
                        ['summary: "S"', "created:"] + base_valid[2:],
                        needs_review=("created",),
                    ),
                    [("type", "unknown_type")],
                ),
                # unknown type, duplicate needs_review entries -> the shape
                # rule fires independently of the type error, and only one
                # needs_review error is ever reported alongside it.
                (
                    "documents/r5_unknown_type_duplicate_needs_review.md",
                    self._unresolved_type_doc('type: "dish"', base_valid, needs_review=("created", "created")),
                    [("type", "unknown_type"), ("needs_review", "bad_format")],
                ),
                # unknown type, only a custom field (cuisine) listed, no base
                # uncertainty present -> the cuisine entry is not evaluated
                # at all.
                (
                    "documents/r5_unknown_type_custom_field_only_listed.md",
                    self._unresolved_type_doc('type: "dish"', base_valid, needs_review=("cuisine",)),
                    [("type", "unknown_type")],
                ),
                # unknown type, kind listed but kind holds a non-uncertainty
                # value ("source") -> an entry naming a base field that is
                # not actually uncertain is a mismatch, exactly like the
                # resolved-type rule.
                (
                    "documents/r5_unknown_type_kind_listed_but_not_uncertain.md",
                    self._unresolved_type_doc('type: "dish"', base_valid, needs_review=("kind",)),
                    [("type", "unknown_type"), ("needs_review", "needs_review_mismatch")],
                ),
                # absent type, empty summary not listed -> missing (not
                # unknown_type) plus the base-field mismatch.
                (
                    "documents/r5_absent_type_summary_uncertain_not_listed.md",
                    self._unresolved_type_doc(None, ['summary: ""'] + base_valid[1:]),
                    [("type", "missing"), ("needs_review", "needs_review_mismatch")],
                ),
                # type: 12 (ambiguous scalar, unresolved) with a consistent
                # list -> only the ambiguous_scalar error.
                (
                    "documents/r5_ambiguous_type_consistent_list.md",
                    self._unresolved_type_doc("type: 12", base_valid),
                    [("type", "ambiguous_scalar")],
                ),
            ]
        )


class TestMergeFlatteningReuse(TempBrainTestCase):
    """A mapping that holds a merge key and is reached more than once --
    constructed and also merged, merged from two places, merged through an
    aliased sequence, or nested inside another merge source -- loads with the
    values stock PyYAML SafeLoader gives it. Duplicate explicit keys stay
    malformed in every mapping, however often it is reached.

    Each accepted fixture's comment records the value `yaml.safe_load` gives
    for the block. Each rejected fixture's comment records that stock loads it
    last-wins, so rejecting it is this loader's own duplicate-key rule (C4)."""

    _OTHER_VALID_FIELDS: ClassVar[list] = [
        'id: "01arz3ndektsv4rrffq69g5fav"',
        'type: "note"',
        'summary: "S"',
        'created: "2026-01-01"',
        'reviewed: "2026-01-01"',
        'origin: "authored"',
        'evidence: []',
        'kind: "source"',
        'authored_by: "human"',
        'retention: "durable"',
    ]

    def _doc(self, extra_lines):
        """A complete managed document: kb: 1, extra_lines (which supply title
        and status, directly or via merge), and every other required field
        holding an ordinary valid value."""
        lines = ["---", "kb: 1", *extra_lines, *self._OTHER_VALID_FIELDS, "---"]
        return "\n".join(lines) + "\nbody\n"

    def _assert_cases(self, cases):
        """Writes every (path, extra_lines, expected_errors) case, validates
        once against exactly that path set, then checks each document's
        complete error multiset."""
        for path, extra_lines, _expected in cases:
            self.write(path, self._doc(extra_lines))
        _proc, report = self.validate(expect_paths={path for path, _lines, _expected in cases})
        for path, _lines, expected in cases:
            support.assert_errors(self, support.doc_by_path(report, path), expected, path)

    # Accepted: a merge-bearing source reached more than once.

    def test_reused_source_with_override_merged_again(self):
        self._assert_cases(
            [
                # stock: status == "draft"
                (
                    "documents/reuse_flow.md",
                    [
                        'title: "T"',
                        'base: &b {status: "bogus"}',
                        'mid: &m {<<: *b, status: "draft"}',
                        "<<: *m",
                    ],
                    [],
                ),
                # stock: status == "draft"
                (
                    "documents/reuse_block.md",
                    [
                        'title: "T"',
                        "base: &b",
                        '  status: "bogus"',
                        "mid: &m",
                        "  <<: *b",
                        '  status: "draft"',
                        "<<: *m",
                    ],
                    [],
                ),
            ]
        )

    def test_reused_source_with_invalid_override_merged_again(self):
        self._assert_cases(
            [
                # stock: status == "bogus" (the source's explicit key wins)
                (
                    "documents/reuse_reverse.md",
                    [
                        'title: "T"',
                        'base: &b {status: "draft"}',
                        'mid: &m {<<: *b, status: "bogus"}',
                        "<<: *m",
                    ],
                    [("status", "not_allowed")],
                ),
            ]
        )

    def test_source_with_merge_merged_into_one_and_into_two_mappings(self):
        self._assert_cases(
            [
                # stock: status == "draft"; x == y == {"status": "draft"}
                (
                    "documents/merged_into_two.md",
                    [
                        'title: "T"',
                        'base: &b {status: "bogus"}',
                        'mid: &m {<<: *b, status: "draft"}',
                        "x: &x {<<: *m}",
                        "y: {<<: *m}",
                        "<<: *x",
                    ],
                    [],
                ),
                # stock: status == "draft"; x == {"status": "draft"}
                (
                    "documents/merged_into_one.md",
                    [
                        'title: "T"',
                        'base: &b {status: "bogus"}',
                        'mid: &m {<<: *b, status: "draft"}',
                        "x: &x {<<: *m}",
                        "<<: *x",
                    ],
                    [],
                ),
            ]
        )

    def test_reused_multi_source_merge_with_shared_key_first_source_wins(self):
        self._assert_cases(
            [
                # stock: status == "bogus"
                (
                    "documents/multi_source_first_bogus.md",
                    [
                        'title: "T"',
                        'b1: &b1 {status: "bogus"}',
                        'b2: &b2 {status: "draft"}',
                        "mid: &m {<<: [*b1, *b2]}",
                        "<<: *m",
                    ],
                    [("status", "not_allowed")],
                ),
                # stock: status == "draft"
                (
                    "documents/multi_source_first_draft.md",
                    [
                        'title: "T"',
                        'b1: &b1 {status: "bogus"}',
                        'b2: &b2 {status: "draft"}',
                        "mid: &m {<<: [*b2, *b1]}",
                        "<<: *m",
                    ],
                    [],
                ),
            ]
        )

    def test_reuse_through_aliased_sequence_of_mappings(self):
        self._assert_cases(
            [
                # stock: status == "draft"; x == [{"status": "draft"}]
                (
                    "documents/aliased_sequence_direct.md",
                    [
                        'title: "T"',
                        'base: &b {status: "bogus"}',
                        'x: &x [{<<: *b, status: "draft"}]',
                        "<<: *x",
                    ],
                    [],
                ),
                # stock: status == "draft"; y == {"status": "draft"}
                (
                    "documents/aliased_sequence_via_mapping.md",
                    [
                        'title: "T"',
                        'base: &b {status: "bogus"}',
                        'x: &x [{<<: *b, status: "draft"}]',
                        "y: &y {<<: *x}",
                        "<<: *y",
                    ],
                    [],
                ),
            ]
        )

    def test_same_merge_bearing_source_listed_twice(self):
        self._assert_cases(
            [
                # stock: status == "draft"
                (
                    "documents/merge_list_repeats_source.md",
                    [
                        'title: "T"',
                        'base: &b {status: "bogus"}',
                        'mid: &m {<<: *b, status: "draft"}',
                        "<<: [*m, *m]",
                    ],
                    [],
                ),
            ]
        )

    def test_inline_source_nested_in_inline_source_merged_again(self):
        self._assert_cases(
            [
                # stock: status == "draft"; x == {"status": "draft"}
                (
                    "documents/nested_inline_reused.md",
                    [
                        'title: "T"',
                        'x: {<<: &m {<<: {status: "bogus"}, status: "draft"}}',
                        "<<: *m",
                    ],
                    [],
                ),
            ]
        )

    def test_aliased_keys_in_and_over_a_reused_source(self):
        self._assert_cases(
            [
                # stock: status == "draft"
                (
                    "documents/aliased_keys_in_reused_source.md",
                    [
                        'title: "T"',
                        "sk: &sk status",
                        'base: &b {*sk : "bogus"}',
                        'mid: &m {<<: *b, *sk : "draft"}',
                        "<<: *m",
                    ],
                    [],
                ),
                # stock: status == "draft"; x == {"status": "bogus"}
                (
                    "documents/aliased_key_overrides_reused_source.md",
                    [
                        'title: "T"',
                        "sk: &sk status",
                        'base: &b {status: "draft"}',
                        'mid: &m {<<: *b, status: "bogus"}',
                        "x: {<<: *m}",
                        '*sk : "draft"',
                        "<<: *m",
                    ],
                    [],
                ),
            ]
        )

    def test_self_recursive_merge(self):
        self._assert_cases(
            [
                # stock: status == "draft"
                (
                    "documents/self_recursive_merge.md",
                    [
                        'title: "T"',
                        'x: &a {<<: [*a, {status: "bogus"}], status: "draft"}',
                        "<<: *a",
                    ],
                    [],
                ),
            ]
        )

    def test_value_key_in_document_and_in_reused_source(self):
        self._assert_cases(
            [
                # stock: {"=": 1, "status": "draft", ...}
                (
                    "documents/value_key_in_document.md",
                    ['title: "T"', 'status: "draft"', "=: 1"],
                    [],
                ),
                # stock: {"=": 1, "status": "draft", ...}; x == {"=": 1, "status": "draft"}
                (
                    "documents/value_key_in_reused_source.md",
                    [
                        'title: "T"',
                        'b: &b {=: 1, status: "draft"}',
                        "x: {<<: *b}",
                        "<<: *b",
                    ],
                    [],
                ),
            ]
        )

    def test_repeated_merge_keys_later_one_wins(self):
        self._assert_cases(
            [
                # stock: status == "draft"
                (
                    "documents/repeated_merge_later_draft.md",
                    [
                        'title: "T"',
                        'b1: &b1 {status: "bogus"}',
                        'b2: &b2 {status: "draft"}',
                        "<<: *b1",
                        "<<: *b2",
                    ],
                    [],
                ),
                # stock: status == "bogus"
                (
                    "documents/repeated_merge_later_bogus.md",
                    [
                        'title: "T"',
                        'b1: &b1 {status: "draft"}',
                        'b2: &b2 {status: "bogus"}',
                        "<<: *b1",
                        "<<: *b2",
                    ],
                    [("status", "not_allowed")],
                ),
            ]
        )

    # Rejected: duplicate explicit keys, wherever the mapping is reached.
    # Stock loads every fixture below last-wins.

    def test_duplicate_keys_rejected_in_every_mapping(self):
        self._assert_cases(
            [
                # stock: status == "draft" (last wins)
                (
                    "documents/dup_top_level_beside_merge.md",
                    [
                        'title: "T"',
                        "b: &b {c: 1}",
                        'status: "bogus"',
                        'status: "draft"',
                        "<<: *b",
                    ],
                    [(None, "malformed")],
                ),
                # stock: status == "draft" (last wins)
                (
                    "documents/dup_inline_source.md",
                    ['title: "T"', '<<: {status: "bogus", status: "draft"}'],
                    [(None, "malformed")],
                ),
                # stock: status == "draft" (last wins)
                (
                    "documents/dup_anchored_source_reused.md",
                    [
                        'title: "T"',
                        'd: &d {status: "bogus", status: "draft"}',
                        "x: {<<: *d}",
                        "<<: *d",
                    ],
                    [(None, "malformed")],
                ),
                # stock: status == "draft" (last wins)
                (
                    "documents/dup_nested_inline_source.md",
                    ['title: "T"', '<<: {<<: {status: "bogus", status: "draft"}}'],
                    [(None, "malformed")],
                ),
                # stock: status == "draft" (last wins)
                (
                    "documents/dup_reused_source_with_merge.md",
                    [
                        'title: "T"',
                        "b: &b {c: 1}",
                        'm: &m {<<: *b, status: "bogus", status: "draft"}',
                        "x: {<<: *m}",
                        "<<: *m",
                    ],
                    [(None, "malformed")],
                ),
                # A quoted "<<" is an ordinary explicit key.
                # stock: "<<" == {"c": 2} (last wins)
                (
                    "documents/dup_quoted_merge_key.md",
                    ['title: "T"', 'status: "draft"', '"<<": {c: 1}', '"<<": {c: 2}'],
                    [(None, "malformed")],
                ),
            ]
        )

    def test_duplicate_keys_rejected_in_either_visit_order(self):
        self._assert_cases(
            [
                # Merged by the top-level mapping first, constructed as d later.
                # stock: c == 2 (last wins)
                (
                    "documents/dup_merged_then_constructed.md",
                    ['title: "T"', 'status: "draft"', "d: &d {c: 1, c: 2}", "<<: *d"],
                    [(None, "malformed")],
                ),
                # Constructed as d first, merged into x later.
                # stock: d == x == {"c": 2} (last wins)
                (
                    "documents/dup_constructed_then_merged.md",
                    ['title: "T"', 'status: "draft"', "d: &d {c: 1, c: 2}", "x: {<<: *d}"],
                    [(None, "malformed")],
                ),
            ]
        )


class TestUnreadablePaths(TempBrainTestCase):
    def _locked(self, paths):
        original = [(path, os.stat(path).st_mode) for path in paths]

        def restrict():
            for path, _mode in original:
                os.chmod(path, 0)
            return [(path, os.stat(path).st_mode) for path, _mode in original]

        def restore():
            for path, mode in original:
                os.chmod(path, mode)

        return restrict, restore

    def _assert_counts(self, report):
        self.assertEqual(sum(report["counts"].values()), len(report["documents"]))

    def test_locked_nested_folder_is_skipped(self):
        self.write("documents/visible.md", note_doc())
        locked = self.write("documents/locked/bad.md", "---\nkb: 1\n---\n")
        folder = os.path.dirname(locked)
        restrict, restore = self._locked([folder])
        proc, report = self.validate({"documents/visible.md"}, restrict, restore)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(report["outcome"], "invalid")
        self.assertEqual(report["skipped"], [{"path": "documents/locked", "reason": "unreadable"}])
        self._assert_counts(report)

    def test_locked_markdown_file_is_skipped(self):
        self.write("documents/ok.md", note_doc())
        locked = self.write("documents/locked.md", note_doc())
        restrict, restore = self._locked([locked])
        proc, report = self.validate({"documents/ok.md"}, restrict, restore)
        self.assertEqual(proc.returncode, 3)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(report["skipped"], [{"path": "documents/locked.md", "reason": "unreadable"}])
        self._assert_counts(report)

    def test_locked_zone_and_module_roots_are_skipped(self):
        self.write("documents/ok.md", note_doc())
        wiki = os.path.join(self.tmpdir, "wiki")
        os.makedirs(wiki)
        self.write("wiki/bad.md", note_doc())
        self.write(".brain/modules/projects/module.json", json.dumps({"module": "projects", "folder": "projects"}))
        projects = os.path.join(self.tmpdir, "projects")
        os.makedirs(projects)
        restrict, restore = self._locked([wiki, projects])
        proc, report = self.validate({"documents/ok.md"}, restrict, restore)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(
            report["skipped"],
            [{"path": "projects", "reason": "unreadable"}, {"path": "wiki", "reason": "unreadable"}],
        )
        self._assert_counts(report)

    def test_silent_rules_and_symlinks_precede_readability(self):
        self.write("documents/ok.md", note_doc())
        hidden = self.write("documents/.hidden.md", note_doc())
        inbox = self.write("inbox/locked.md", note_doc())
        raw = self.write("raw/locked/item.md", note_doc())
        text = self.write("documents/n.txt", "text\n")
        locked = self.write("documents/aa/bad.md", note_doc())
        target = self.write("elsewhere/locked/target.md", note_doc())
        os.symlink(os.path.dirname(target), os.path.join(self.tmpdir, "documents", "zz.md"))
        restrict, restore = self._locked([hidden, inbox, os.path.dirname(raw), text, os.path.dirname(locked), os.path.dirname(target)])
        proc, report = self.validate({"documents/ok.md"}, restrict, restore)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(
            report["skipped"],
            [{"path": "documents/aa", "reason": "unreadable"}, {"path": "documents/zz.md", "reason": "symlink"}],
        )
        self._assert_counts(report)

    def test_permission_fixture_controls_are_valid_when_accessible(self):
        self.write("documents/visible.md", note_doc())
        self.write("documents/locked/bad.md", note_doc())
        self.write("documents/locked.md", note_doc())
        self.write("wiki/page.md", note_doc())
        _proc, report = self.validate(
            {"documents/visible.md", "documents/locked/bad.md", "documents/locked.md", "wiki/page.md"}
        )
        self.assertEqual(report["outcome"], "valid")
        self.assertEqual(report["skipped"], [])
        self._assert_counts(report)

    def test_listable_unsearchable_folder_reports_entries_by_rule(self):
        self.write("documents/ok.md", note_doc())
        folder = os.path.join(self.tmpdir, "documents", "ro")
        x_md = self.write("documents/ro/x.md", note_doc())
        sub = self.write("documents/ro/sub/y.md", note_doc())
        self.write("documents/ro/n.txt", "text\n")
        self.write("documents/ro/.h.md", note_doc())
        os.symlink(x_md, os.path.join(folder, "link.md"))
        original = [(folder, os.stat(folder).st_mode)]

        def restrict():
            os.chmod(folder, 0o644)
            return [(folder, os.stat(folder).st_mode)]

        def restore():
            os.chmod(folder, original[0][1])

        # Listing the parent is permitted, but traversing an entry is not.
        restrict()
        self.addCleanup(restore)
        try:
            names = os.listdir(folder)
            with self.assertRaises(PermissionError):
                open(x_md, "rb").close()
            with self.assertRaises(PermissionError):
                os.listdir(os.path.dirname(sub))
        except PermissionError:
            self.skipTest("0o644 does not provide the required listable-but-unsearchable condition")
        self.assertEqual(set(names), {"x.md", "sub", "link.md", "n.txt", ".h.md"})
        restore()

        def prove_denial():
            os.listdir(folder)
            with open(x_md, "rb"):
                pass

        proc, report = self.validate({"documents/ok.md"}, restrict, restore, prove_denial)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(report["outcome"], "invalid")
        self.assertEqual(
            report["skipped"],
            [
                {"path": "documents/ro/link.md", "reason": "symlink"},
                {"path": "documents/ro/sub", "reason": "unreadable"},
                {"path": "documents/ro/x.md", "reason": "unreadable"},
            ],
        )
        self._assert_counts(report)


class TestOptionalDates(TempBrainTestCase):
    def test_invalid_optional_dates(self):
        values = ['"not-a-date"', '"2026-09-14T10:00:00Z"', "2026-09-14T10:00:00Z", "0", "20260914", "false", "yes", "[]", "[2026-01-01]", '" "', '"2026-02-30"']
        paths = set()
        for field in ("fresh_until", "first_seen"):
            for number, value in enumerate(values):
                path = f"documents/{field}-{number}.md"
                paths.add(path)
                self.write(path, note_doc(**{field: value}))
        self.write("documents/both.md", note_doc(fresh_until='"bad"', first_seen="false"))
        paths.add("documents/both.md")
        _proc, report = self.validate(paths)
        for path in paths:
            expected = [("fresh_until", "bad_format"), ("first_seen", "bad_format")] if path.endswith("both.md") else [(("fresh_until" if "/fresh_until-" in path else "first_seen"), "bad_format")]
            support.assert_errors(self, support.doc_by_path(report, path), expected, path)

    def test_optional_date_null_empty_and_valid_controls(self):
        paths = set()
        for field in ("fresh_until", "first_seen"):
            for number, value in enumerate((None, "~", "null", '\"\"', "2026-01-01", '\"2026-01-01\"')):
                path = f"documents/{field}-control-{number}.md"
                paths.add(path)
                self.write(path, note_doc(**{field: value}))
            mismatch = f"documents/{field}-mismatch.md"
            paths.add(mismatch)
            self.write(mismatch, note_doc(**{field: None}, needs_review=("created", "reviewed", "summary", "kind", "origin", field)))
        _proc, report = self.validate(paths)
        for path in paths:
            expected = [("needs_review", "needs_review_mismatch")] if path.endswith("mismatch.md") else []
            support.assert_errors(self, support.doc_by_path(report, path), expected, path)


class TestCustomAllowedValuesShape(TempBrainTestCase):
    def _definition(self, values, name="recipe"):
        return json.dumps({"type": name, "zones": ["*"], "required": [], "allowed_values": {"cuisine": values}, "display_fields": [], "search_fields": []})

    def test_non_string_members_invalidate_definition_and_type(self):
        for number, values in enumerate(([123], ["thai", 1], [None], [["thai"]], [True], [{"a": "b"}])):
            with self.subTest(values=values):
                self.write(".brain/types/custom/recipe.json", self._definition(values))
                self.write("documents/recipe.md", note_doc(type='"recipe"', cuisine="123"))
                _proc, report = self.validate({"documents/recipe.md"})
                self.assertEqual(report["definitions"], [{"path": ".brain/types/custom/recipe.json", "code": "invalid_definition", "message": "missing or wrongly typed keys"}])
                support.assert_errors(self, support.doc_by_path(report, "documents/recipe.md"), [("type", "unknown_type")])
                os.unlink(os.path.join(self.tmpdir, ".brain/types/custom/recipe.json"))

    def test_empty_and_string_allowed_values_keep_existing_behavior(self):
        self.write(".brain/types/custom/recipe.json", self._definition([]))
        self.write("documents/empty.md", note_doc(type='"recipe"'))
        _proc, report = self.validate({"documents/empty.md"})
        self.assertEqual(report["definitions"], [])
        support.assert_errors(self, support.doc_by_path(report, "documents/empty.md"), [])
        self.write(".brain/types/custom/recipe.json", self._definition(["thai"]))
        self.write("documents/thai.md", note_doc(type='"recipe"', cuisine='"thai"'))
        self.write("documents/martian.md", note_doc(type='"recipe"', cuisine='"martian"'))
        _proc, report = self.validate({"documents/empty.md", "documents/thai.md", "documents/martian.md"})
        self.assertEqual(report["definitions"], [])
        support.assert_errors(self, support.doc_by_path(report, "documents/thai.md"), [])
        support.assert_errors(self, support.doc_by_path(report, "documents/martian.md"), [("cuisine", "not_allowed")])

    def test_failed_note_definition_leaves_the_base_type_in_force(self):
        self.write(".brain/types/custom/note.json", self._definition([1], name="note"))
        self.write("documents/note.md", note_doc())
        _proc, report = self.validate({"documents/note.md"})
        self.assertEqual(len(report["definitions"]), 1)
        self.assertEqual(report["definitions"][0]["code"], "invalid_definition")
        support.assert_errors(self, support.doc_by_path(report, "documents/note.md"), [])



if __name__ == "__main__":
    unittest.main()
