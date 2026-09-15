"""Shared test helpers. Not a test module itself (unittest discover ignores it
because it does not match the test*.py pattern)."""

import collections
import hashlib
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN = os.path.join(REPO_ROOT, "bin", "brain")


def run_brain(args, isolated=True, env=None):
    if isolated:
        cmd = [sys.executable, "-B", "-S", "-E", BRAIN] + list(args)
    else:
        cmd = [sys.executable, "-B", BRAIN] + list(args)
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env, check=False)
    return proc


def parse_single_json(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(f"expected exactly one JSON line, got: {stdout!r}")
    return json.loads(lines[0])


def write_file(root, rel_path, content, binary=False, newline=""):
    full = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    if binary:
        with open(full, "wb") as fh:
            fh.write(content)
    else:
        with open(full, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(content)
    return full


def hash_tree(root):
    hashes = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                hashes[rel] = "symlink:" + os.readlink(full)
                continue
            with open(full, "rb") as fh:
                hashes[rel] = hashlib.sha256(fh.read()).hexdigest()
    return hashes


def list_paths(root):
    """Every file and directory under root, so a validate run that creates or
    removes an empty directory is caught even though it holds no file to hash."""
    paths = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for f in filenames:
            full = os.path.join(dirpath, f)
            paths.append(os.path.relpath(full, root).replace(os.sep, "/"))
        for d in dirnames:
            full = os.path.join(dirpath, d)
            rel = os.path.relpath(full, root).replace(os.sep, "/") + "/"
            paths.append(rel + " (symlink)" if os.path.islink(full) else rel)
    return sorted(paths)


_EXCLUDED_DIR_NAMES = {".venv", ".git"}


def find_bytecode(root):
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIR_NAMES]
        if "__pycache__" in dirnames:
            hits.append(os.path.join(dirpath, "__pycache__"))
        for f in filenames:
            if f.endswith(".pyc"):
                hits.append(os.path.join(dirpath, f))
    return hits


def errors_multiset(doc):
    """A true multiset: repeated (field, code) pairs are distinct, not collapsed."""
    return collections.Counter((e["field"], e["code"]) for e in doc["errors"])


def assert_errors(testcase, doc, expected, msg=None):
    """Exact multiset equality between doc's errors and expected [(field, code), ...]."""
    testcase.assertEqual(errors_multiset(doc), collections.Counter(expected), msg or doc.get("path"))


def doc_by_path(report, path):
    for d in report["documents"]:
        if d["path"] == path:
            return d
    return None


def document_paths(report):
    return {d["path"] for d in report["documents"]}
