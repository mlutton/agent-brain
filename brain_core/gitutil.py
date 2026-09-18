"""Small primitives shared by persist and (in a later story) other write
operations: a `git` subprocess wrapper, byte hashing and tiny JSON journal
helpers. See brain_core/persist.py's module docstring for why this is a
separate module rather than reaching into brain_core/setup.py's private
helpers or duplicating them silently."""

import hashlib
import json
import os
import subprocess

COMMIT_AUTHOR_NAME = "agent-brain"
COMMIT_AUTHOR_EMAIL = "agent@agent-brain.invalid"


def run_git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=False
    )


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hash_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.write("\n")


def commit_paths(root, paths, trailer_text):
    """Stages exactly `paths` (relative to root) and commits exactly those
    paths, via a pathspec-limited `git commit`, so any other uncommitted or
    staged working-tree change is never swept into the commit (C7, P12)."""
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME=COMMIT_AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=COMMIT_AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=COMMIT_AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=COMMIT_AUTHOR_EMAIL,
    )
    subprocess.run(
        ["git", "add", "--"] + list(paths), cwd=root, check=True, capture_output=True, text=True, env=env
    )
    message = "Brain write\n\n" + trailer_text + "\n"
    subprocess.run(
        ["git", "commit", "-q", "-m", message, "--"] + list(paths),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
