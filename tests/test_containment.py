"""Containment: tests act only under a temporary root.

`brain recover` and `brain persist` mutate the root they are given, and nothing
else in the harness stops a test from pointing them at a real brain or at this
checkout. `support.run_brain` therefore refuses any `--root` that is not a
directory strictly inside the system temp directory and outside this repository,
before it starts a process. These tests hold that guard itself.
"""

import os
import shutil
import tempfile
import unittest

from tests import support


class TempRootGuardTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.mkdtemp(prefix="brain-containment-")
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def test_a_directory_made_under_the_temp_directory_is_accepted(self):
        support.assert_temp_root(self.scratch)
        support.assert_temp_root(os.path.join(self.scratch, "not-yet-created"))

    def test_roots_outside_the_temp_directory_are_refused(self):
        outside = {
            "this repository": support.REPO_ROOT,
            "a folder inside this repository": os.path.join(support.REPO_ROOT, "docs"),
            "the home directory": os.path.expanduser("~"),
            "the filesystem root": os.path.abspath(os.sep),
            "the temp directory itself": tempfile.gettempdir(),
        }
        for label, root in outside.items():
            with self.subTest(label):
                with self.assertRaises(AssertionError):
                    support.assert_temp_root(root)

    def test_a_temp_path_that_resolves_outside_the_temp_directory_is_refused(self):
        link = os.path.join(self.scratch, "looks-temporary")
        os.symlink(support.REPO_ROOT, link)
        with self.assertRaises(AssertionError):
            support.assert_temp_root(link)

    def test_a_temp_directory_configured_inside_the_repository_does_not_make_it_safe(self):
        # A TMPDIR pointing into the checkout would make every repository path
        # look temporary; the repository exclusion is what still refuses them.
        self.addCleanup(setattr, tempfile, "tempdir", tempfile.tempdir)
        tempfile.tempdir = support.REPO_ROOT
        with self.assertRaises(AssertionError):
            support.assert_temp_root(os.path.join(support.REPO_ROOT, "docs"))

    def test_run_brain_refuses_a_non_temp_root_before_starting_a_process(self):
        # The path does not exist, so even a guard that failed to fire would
        # only get `journal_missing` back from `recover`; nothing real is at risk.
        missing = os.path.join(support.REPO_ROOT, "no-such-brain-for-the-guard-test")
        for args in (["recover", "--root", missing], ["persist", "--root", missing]):
            with self.subTest(args=args):
                with self.assertRaises(AssertionError):
                    support.run_brain(args)
        self.assertFalse(os.path.exists(missing))

    def test_run_brain_still_runs_a_temp_root(self):
        proc = support.run_brain(["validate", "--root", self.scratch])
        self.assertEqual(proc.returncode, 0, proc)


if __name__ == "__main__":
    unittest.main()
