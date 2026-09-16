"""`brain setup` (see docs/specification/beta.md, C3)."""

import datetime
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid

from . import typedefs

DATA_ZONES = ("inbox", "raw", "documents", "wiki")
CODE_DIRS = ("bin", "brain_core", "vendor")
TYPES_DIR = "types"
MIN_PYTHON = (3, 11)
MIN_GIT = (2, 30)


def _git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, check=False
    )


def _refuse(reason):
    return {"outcome": "refused", "reason": reason}, 2


def _starter_head(starter):
    proc = _git(["rev-parse", "HEAD"], starter)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _starter_clean(starter):
    proc = _git(["status", "--porcelain"], starter)
    return proc.returncode == 0 and proc.stdout.strip() == ""


def _is_inside_repo_worktree(path):
    """True when `path` (which may not exist yet) sits inside another
    repository's working tree, per C3.1."""
    probe = path
    while True:
        parent = os.path.dirname(probe.rstrip(os.sep)) or os.sep
        if os.path.isdir(probe):
            proc = _git(["rev-parse", "--is-inside-work-tree"], probe)
            if proc.returncode == 0 and proc.stdout.strip() == "true":
                toplevel = _git(["rev-parse", "--show-toplevel"], probe).stdout.strip()
                if os.path.realpath(toplevel) != os.path.realpath(path):
                    return True
                return False
        if parent == probe:
            return False
        probe = parent


def validate_request(options):
    starter = options.get("--starter")
    target = options.get("--target")
    if not isinstance(starter, str):
        return None, ("missing_starter",)
    if not isinstance(target, str):
        return None, ("missing_target",)
    if not os.path.isdir(starter):
        return None, ("invalid_starter",)

    head = _starter_head(starter)
    if head is None:
        return None, ("starter_unresolvable",)
    if not _starter_clean(starter):
        return None, ("starter_dirty",)

    if os.path.exists(target):
        if not os.path.isdir(target):
            return None, ("invalid_target",)
        if os.listdir(target):
            return None, ("target_not_empty",)

    if _is_inside_repo_worktree(target):
        return None, ("target_nested_in_repository",)

    return {"starter": starter, "target": target, "commit": head}, None


def _hash_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _copy_file(src, dst, copied, brain_rel):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    copied.append({"path": brain_rel, "sha256": _hash_file(dst)})


def _tracked_paths(starter, subpath):
    """Paths under `subpath`, relative to it, tracked in the starter's HEAD
    commit (spec C2's "system shipped: tracked source" row) — not merely
    present and not gitignored in the working copy."""
    proc = _git(["ls-tree", "-r", "--name-only", "-z", "HEAD", "--", subpath], starter)
    if proc.returncode != 0:
        return set()
    prefix = subpath.rstrip("/") + "/"
    result = set()
    for entry in proc.stdout.split("\0"):
        if entry.startswith(prefix):
            result.add(entry[len(prefix):])
    return result


def _copy_tree(src_root, dst_root, copied, brain_prefix, allowed):
    """Copies only files in `allowed` (paths relative to src_root)."""
    for rel in sorted(allowed):
        src = os.path.join(src_root, *rel.split("/"))
        if not os.path.isfile(src):
            continue
        dst = os.path.join(dst_root, *rel.split("/"))
        brain_rel = "/".join([brain_prefix, rel])
        _copy_file(src, dst, copied, brain_rel)


def _copy_code_and_types(starter, target, copied):
    brain_dir = os.path.join(target, ".brain")
    for name in CODE_DIRS:
        src = os.path.join(starter, name)
        if os.path.isdir(src):
            allowed = _tracked_paths(starter, name)
            _copy_tree(src, os.path.join(brain_dir, name), copied, f".brain/{name}", allowed)
    types_src = os.path.join(starter, TYPES_DIR)
    if os.path.isdir(types_src):
        allowed = _tracked_paths(starter, TYPES_DIR)
        _copy_tree(types_src, os.path.join(brain_dir, TYPES_DIR), copied, ".brain/types", allowed)


def _copy_skills(starter, target, copied):
    # Both skill folders and the custom-types folder are required C2 layout,
    # independently of whether the starter has any payload for them.
    for dest_root in (".claude/skills", ".agents/skills"):
        os.makedirs(os.path.join(target, dest_root), exist_ok=True)

    skills_src = os.path.join(starter, "skills")
    if not os.path.isdir(skills_src):
        return
    for skill_name in sorted(os.listdir(skills_src)):
        skill_src = os.path.join(skills_src, skill_name)
        if not os.path.isdir(skill_src):
            continue
        allowed = _tracked_paths(starter, f"skills/{skill_name}")
        for dest_root in (".claude/skills", ".agents/skills"):
            _copy_tree(
                skill_src,
                os.path.join(target, dest_root, skill_name),
                copied,
                f"{dest_root}/{skill_name}",
                allowed,
            )


def _check_module(starter, module_name):
    """Read-only: locates and validates the module manifest before any
    target mutation (spec C3). Returns (manifest, None) or (None, reason)."""
    module_subpath = f"modules/{module_name}"
    tracked = _tracked_paths(starter, module_subpath)
    manifest_src = os.path.join(starter, "modules", module_name, "module.json")
    if "module.json" not in tracked or not os.path.isfile(manifest_src):
        return None, "module_not_found"
    with open(manifest_src, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not typedefs._valid_module_folder(manifest.get("folder")):
        return None, "invalid_module_folder"
    return manifest, None


def _install_module(starter, target, module_name, manifest, copied):
    manifest_src = os.path.join(starter, "modules", module_name, "module.json")
    module_dst = os.path.join(target, ".brain", "modules", module_name)
    _copy_file(manifest_src, os.path.join(module_dst, "module.json"), copied, f".brain/modules/{module_name}/module.json")

    types_src = os.path.join(starter, "modules", module_name, "types")
    if os.path.isdir(types_src):
        tracked = _tracked_paths(starter, f"modules/{module_name}")
        allowed = {p[len("types/"):] for p in tracked if p.startswith("types/")}
        _copy_tree(types_src, os.path.join(module_dst, "types"), copied, f".brain/modules/{module_name}/types", allowed)

    folder = manifest["folder"]
    os.makedirs(os.path.join(target, folder), exist_ok=True)
    return folder


def _write_layout(target):
    for zone in DATA_ZONES:
        os.makedirs(os.path.join(target, zone), exist_ok=True)
    os.makedirs(os.path.join(target, ".brain", "types", "custom"), exist_ok=True)
    # The projects module folder is part of C2's fixed layout tree; it
    # exists empty even when --module isn't used to populate it (C15).
    os.makedirs(os.path.join(target, "projects"), exist_ok=True)


def _write_gitignore(target):
    with open(os.path.join(target, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write(".brain/journal/\n.brain/state/\n")


def _write_journal(target, brain_id):
    journal_dir = os.path.join(target, ".brain", "journal")
    os.makedirs(journal_dir, exist_ok=True)
    with open(os.path.join(journal_dir, "epoch.json"), "w", encoding="utf-8") as fh:
        json.dump({"brain_id": brain_id}, fh)
        fh.write("\n")


def _starter_url(starter):
    # `git config --get-all` returns the literal configured values, in
    # configured order, with no insteadOf rewriting; the first configured
    # origin URL wins. Other remote names never affect this (spec C3).
    proc = _git(["config", "--get-all", "remote.origin.url"], starter)
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            if line.strip():
                return line.strip()
    return pathlib.Path(os.path.realpath(starter)).as_uri()


def _write_setup_json(target, brain_id, starter_url, commit, modules, copied):
    record = {
        "brain_id": brain_id,
        "starter_url": starter_url,
        "commit": commit,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modules": modules,
        "copied": sorted(copied, key=lambda e: e["path"]),
    }
    with open(os.path.join(target, ".brain", "setup.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return record


def run_setup(options):
    request, refusal = validate_request(options)
    if refusal is not None:
        return _refuse(refusal[0])

    starter = request["starter"]
    target = request["target"]
    commit = request["commit"]
    brain_id = uuid.uuid4().hex

    modules = []
    module_option = options.get("--module")
    copied = []
    manifest = None

    if isinstance(module_option, str):
        manifest, error = _check_module(starter, module_option)
        if error is not None:
            return _refuse(error)

    _write_layout(target)
    _copy_code_and_types(starter, target, copied)
    _copy_skills(starter, target, copied)

    if manifest is not None:
        _install_module(starter, target, module_option, manifest, copied)
        modules.append(module_option)
    _write_journal(target, brain_id)
    _write_gitignore(target)
    starter_url = _starter_url(starter)
    _write_setup_json(target, brain_id, starter_url, commit, modules, copied)

    # The --git checkpoint commit precedes the smoke check (spec C3 step 4
    # then step 5): it must exist even when the smoke check then fails.
    if options.get("--git"):
        _init_git(target)

    failed_check = _run_smoke_checks(target)
    if failed_check == "vendored_dependency":
        # C1's own error/vendored_dependency outcome and exit code are
        # preserved, not collapsed into setup_failed (spec C3).
        return {"outcome": "error", "reason": "vendored_dependency"}, 1
    if failed_check is not None:
        return {"outcome": "setup_failed", "check": failed_check}, 3

    return {"outcome": "setup_complete"}, 0


def _hardlink_probe(zone_dir, check_id):
    """Creates a probe file, hard-links it (proving link support), then
    proves the filesystem refuses a second link onto the same path. Returns
    True on success, False on an unsupported-hard-link failure. A gated fault
    raises at the same os.link call the check makes, so it is handled by the
    same `except OSError` path as a real failure (spec C6's fault-injection
    rule: "exercises the same error-handling path")."""
    faults_enabled = os.environ.get("BRAIN_TEST_FAULTS") == "1"
    fault = os.environ.get("BRAIN_FAULT")
    injected_failure = faults_enabled and fault == f"setup_{check_id}:fail"

    probe_dir = os.path.join(zone_dir, ".setup-smoke")
    os.makedirs(probe_dir, exist_ok=True)
    src = os.path.join(probe_dir, "source")
    link = os.path.join(probe_dir, "link")
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("setup smoke probe\n")
        try:
            if injected_failure:
                raise OSError("simulated unsupported hard link (BRAIN_FAULT)")
            os.link(src, link)
        except OSError:
            return False
        try:
            os.link(src, link)
        except FileExistsError:
            pass
        else:
            return False
        return True
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def _version_tuple(text):
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return None
    return tuple(int(g) if g is not None else 0 for g in match.groups())


def _check_versions():
    if sys.version_info[:2] < MIN_PYTHON:
        return "python_version"
    proc = _git(["--version"], os.getcwd())
    version = _version_tuple(proc.stdout) if proc.returncode == 0 else None
    if version is None or version[:2] < MIN_GIT:
        return "git_version"
    return None


def _validate_empty_brain(target):
    """Runs the just-copied runtime against its own new brain, isolated from
    this process's own module path, so setup certifies what a user's copy
    will actually see (spec Behaviour 4, C1's -S -E convention). Returns
    True (valid), "vendored_dependency" (C1's own error outcome, which must
    propagate unmasked), or False (any other failure)."""
    brain_cli = os.path.join(target, ".brain", "bin", "brain")
    proc = subprocess.run(
        ["python3", "-B", "-S", "-E", brain_cli, "validate", "--root", target],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        doc = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        doc = None
    if proc.returncode == 1 and isinstance(doc, dict) and doc.get("reason") == "vendored_dependency":
        return "vendored_dependency"
    if proc.returncode == 0 and isinstance(doc, dict) and doc.get("outcome") == "valid":
        return True
    return False


def _run_smoke_checks(target):
    version_check = _check_versions()
    if version_check is not None:
        return version_check
    if not _hardlink_probe(os.path.join(target, "documents"), "hardlink_data"):
        return "hardlink_data"
    if not _hardlink_probe(os.path.join(target, ".brain", "state"), "hardlink_state"):
        return "hardlink_state"
    validate_result = _validate_empty_brain(target)
    if validate_result == "vendored_dependency":
        return "vendored_dependency"
    if not validate_result:
        return "empty_brain_validate"
    return None


def _init_git(target):
    run_id = uuid.uuid4().hex
    _git(["init", "-q"], target)
    env_kwargs = dict(
        os.environ,
        GIT_AUTHOR_NAME="agent-brain setup",
        GIT_AUTHOR_EMAIL="setup@agent-brain.invalid",
        GIT_COMMITTER_NAME="agent-brain setup",
        GIT_COMMITTER_EMAIL="setup@agent-brain.invalid",
    )
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True, text=True, env=env_kwargs)
    message = f"Brain setup\n\nBrain-Op: setup\nBrain-Run: {run_id}\n"
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=target, check=True, capture_output=True, text=True, env=env_kwargs
    )
