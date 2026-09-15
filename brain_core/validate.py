"""Implements `brain validate` per specification C5, C5a and C15."""

import hashlib
import os

from . import fields as fields_mod
from . import frontmatter, scan, typedefs


def _classify_file(raw_bytes, yaml_module, duplicate_loader, type_registry, zone):
    version = hashlib.sha256(raw_bytes).hexdigest()

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "status": "invalid",
            "frontmatter": "malformed",
            "frontmatter_features": [],
            "id": None,
            "type": None,
            "version": version,
            "errors": [{"field": None, "code": "malformed", "message": "file is not valid UTF-8"}],
        }

    fm = frontmatter.split_frontmatter(text)
    if fm is None:
        return {
            "status": "unmanaged",
            "frontmatter": "none",
            "frontmatter_features": [],
            "id": None,
            "type": None,
            "version": version,
            "errors": [],
        }
    if fm is frontmatter.UNCLOSED:
        return _malformed(version, "frontmatter block is not closed")

    try:
        features, _doc_count = frontmatter.scan_features(fm.block_text, yaml_module)
    except RecursionError:
        return _malformed(version, "frontmatter is too deeply nested")
    if features is None:
        return _malformed(version, "frontmatter is not valid YAML")

    try:
        documents = list(yaml_module.load_all(fm.block_text, Loader=duplicate_loader))
    except yaml_module.YAMLError:
        return _malformed(version, "frontmatter is not valid YAML")
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, RecursionError) as exc:
        return _malformed(version, f"frontmatter holds a value that cannot be constructed: {exc}")

    mapping = documents[0] if documents else None
    if mapping is None:
        mapping = {}
    if not isinstance(mapping, dict):
        return _malformed(version, "frontmatter top-level value is not a mapping")

    fm_field = "unsupported_for_editing" if features else "supported"
    fm_features = sorted(features)

    if "kb" not in mapping:
        return {
            "status": "unmanaged",
            "frontmatter": fm_field,
            "frontmatter_features": fm_features,
            "id": None,
            "type": None,
            "version": version,
            "errors": [],
        }

    kb_value = mapping.get("kb")
    if isinstance(kb_value, bool) or not isinstance(kb_value, int):
        return _malformed(version, "kb must be an unquoted integer")

    id_value = mapping.get("id") if isinstance(mapping.get("id"), str) else None
    type_value = mapping.get("type") if isinstance(mapping.get("type"), str) else None

    if kb_value != 1:
        return {
            "status": "unsupported_version",
            "frontmatter": fm_field,
            "frontmatter_features": fm_features,
            "id": id_value,
            "type": type_value,
            "version": version,
            "errors": [],
        }

    errors = fields_mod.validate_mapping(mapping, type_registry, zone)
    status = "invalid" if errors else "valid"
    error_dicts = [{"field": f, "code": c, "message": _message(f, c)} for f, c in errors]

    return {
        "status": status,
        "frontmatter": fm_field,
        "frontmatter_features": fm_features,
        "id": id_value,
        "type": type_value,
        "version": version,
        "errors": error_dicts,
    }


def _malformed(version, message):
    return {
        "status": "invalid",
        "frontmatter": "malformed",
        "frontmatter_features": [],
        "id": None,
        "type": None,
        "version": version,
        "errors": [{"field": None, "code": "malformed", "message": message}],
    }


def _message(field, code):
    return f"{field}: {code}"


def run_validate(root, base_dir, yaml_module, duplicate_loader):
    type_registry = typedefs.load_base_types(base_dir)
    typedefs.load_custom_types(type_registry, root)
    module_folders = typedefs.detect_installed_modules(root)

    entries, skipped = scan.scan_zones(root, module_folders)

    documents = []
    counts = {"valid": 0, "invalid": 0, "unmanaged": 0, "unsupported_version": 0, "retained": 0}

    for rel_path, zone in entries:
        full_path = os.path.join(root, rel_path)
        with open(full_path, "rb") as fh:
            raw_bytes = fh.read()
        result = _classify_file(raw_bytes, yaml_module, duplicate_loader, type_registry, zone)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        documents.append(
            {
                "path": rel_path,
                "status": result["status"],
                "frontmatter": result["frontmatter"],
                "frontmatter_features": result["frontmatter_features"],
                "id": result["id"],
                "type": result["type"],
                "version": result["version"],
                "errors": result["errors"],
            }
        )

    documents.sort(key=lambda d: d["path"])

    definitions = type_registry.definitions_sorted()

    outcome = "invalid" if (counts["invalid"] > 0 or definitions) else "valid"

    return {
        "outcome": outcome,
        "counts": counts,
        "definitions": definitions,
        "skipped": skipped,
        "documents": documents,
    }
