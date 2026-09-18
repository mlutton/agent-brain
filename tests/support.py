"""Shared test helpers. Not a test module itself (unittest discover ignores it
because it does not match the test*.py pattern)."""

import collections
import hashlib
import json
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN = os.path.join(REPO_ROOT, "bin", "brain")


def run_brain(args, isolated=True, env=None, input_data=None, timeout=None):
    if isolated:
        cmd = [sys.executable, "-B", "-S", "-E", BRAIN] + list(args)
    else:
        cmd = [sys.executable, "-B", BRAIN] + list(args)
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        input=input_data,
        check=False,
        timeout=timeout,
    )
    return proc


def disposable_runtime(owned_dir, include_brain_core=True, break_module=None):
    """Copies the runtime tree (`bin/`, `brain_core/`, `types/`, `vendor/`) into
    `owned_dir/runtime` and returns that path. `include_brain_core=False` leaves
    the package out; `break_module="validate"` makes `brain_core/validate.py`
    raise when it is imported. Never touches this checkout."""
    runtime = os.path.join(owned_dir, "runtime")
    os.makedirs(runtime)
    names = ["bin", "vendor", "types"]
    if include_brain_core:
        names.append("brain_core")
    for name in names:
        shutil.copytree(
            os.path.join(REPO_ROOT, name),
            os.path.join(runtime, name),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    if break_module is not None:
        module_path = os.path.join(runtime, "brain_core", break_module + ".py")
        with open(module_path, encoding="utf-8") as fh:
            original = fh.read()
        with open(module_path, "w", encoding="utf-8") as fh:
            fh.write('raise RuntimeError("disposable fixture: unloadable module")\n' + original)
    return runtime


def run_runtime_brain(runtime, args):
    """Runs the disposable runtime's own `bin/brain`, isolated like run_brain."""
    cmd = [sys.executable, "-B", "-S", "-E", os.path.join(runtime, "bin", "brain")] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def build_deep_chain(top, depth, leaf_name, leaf_content, level_name="d"):
    """Creates `depth` nested folders under `top`, each holding the next, with a
    file at the bottom. Descends one level at a time through a directory file
    descriptor, holding only one open at once, so neither the depth nor the
    length of the absolute path is limited by the filesystem."""
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            os.mkdir(level_name, dir_fd=fd)
            child = os.open(level_name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(leaf_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=fd)
        with os.fdopen(leaf, "w", encoding="utf-8", newline="") as fh:
            fh.write(leaf_content)
    finally:
        os.close(fd)


def remove_deep_chain(top, depth, leaf_name, level_name="d"):
    """Undoes build_deep_chain level by level. `shutil.rmtree` recurses, so on a
    chain this deep it raises and leaves the tree behind."""
    fd = os.open(top, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(depth):
            child = os.open(level_name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        os.unlink(leaf_name, dir_fd=fd)
        for _ in range(depth):
            parent = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            os.rmdir(level_name, dir_fd=parent)
            fd = parent
    finally:
        os.close(fd)


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


_EXCLUDED_DIR_NAMES = {".venv", ".git", ".dispatch"}


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
