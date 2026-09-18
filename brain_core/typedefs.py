"""Loads and validates document type definitions per specification C5 and C5a."""

import json
import os

from . import scan

_REQUIRED_KEYS = {
    "type": str,
    "zones": list,
    "required": list,
    "allowed_values": dict,
    "display_fields": list,
    "search_fields": list,
}

_UNIVERSAL_ALLOWED_VALUE_FIELDS = {"status", "kind", "authored_by", "retention"}


class TypeDef:
    def __init__(self, data):
        self.type = data["type"]
        self.zones = data["zones"]
        self.required = data["required"]
        self.allowed_values = data["allowed_values"]
        self.uncertainty_values = data.get("uncertainty_values") or {}
        self.display_fields = data["display_fields"]
        self.search_fields = data["search_fields"]

    def allows_zone(self, zone):
        return "*" in self.zones or zone in self.zones


class TypeRegistry:
    def __init__(self):
        self.types = {}
        self.definitions_errors = []
        self.base_allowed_values = {}

    def get(self, name):
        return self.types.get(name)

    def add_definition_error(self, path, code, message):
        self.definitions_errors.append({"path": path, "code": code, "message": message})

    def definitions_sorted(self):
        return sorted(self.definitions_errors, key=lambda e: e["path"])


def _validate_shape(data):
    if not isinstance(data, dict):
        return False
    for key, expected_type in _REQUIRED_KEYS.items():
        if key not in data:
            return False
        if not isinstance(data[key], expected_type):
            return False
    if "uncertainty_values" in data and not isinstance(data["uncertainty_values"], dict):
        return False
    for key in ("zones", "required", "display_fields", "search_fields"):
        if not all(isinstance(item, str) for item in data[key]):
            return False
    return all(
        isinstance(values, list) and all(isinstance(value, str) for value in values)
        for values in data["allowed_values"].values()
    )


def load_base_types(base_dir):
    """Loads the starter's own base type definitions (never from the brain)."""
    registry = TypeRegistry()
    base_types_dir = os.path.join(base_dir, "types", "base")
    for name in sorted(os.listdir(base_types_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(base_types_dir, name)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        type_def = TypeDef(data)
        registry.types[data["type"]] = type_def
        for field in _UNIVERSAL_ALLOWED_VALUE_FIELDS:
            if field in type_def.allowed_values:
                union = registry.base_allowed_values.setdefault(field, [])
                for value in type_def.allowed_values[field]:
                    if value not in union:
                        union.append(value)
    return registry


def _list_folder(root, folder):
    """Returns (sorted names, skipped entries) for a folder under `.brain`.

    A folder that is absent is not an error and reports nothing; a folder
    that exists but cannot be listed is `unreadable` (C5a).
    """
    try:
        return sorted(os.listdir(folder)), []
    except (FileNotFoundError, NotADirectoryError):
        return [], []
    except OSError:
        return [], [scan.unreadable_entry(root, folder)]


def load_custom_types(registry, root):
    """Loads custom type definitions from <root>/.brain/types/custom/*.json.

    Returns the `skipped` entries for what could not be listed.
    """
    custom_dir = os.path.join(root, ".brain", "types", "custom")
    names, skipped = _list_folder(root, custom_dir)
    for name in names:
        if not name.endswith(".json"):
            continue
        rel_path = os.path.relpath(os.path.join(custom_dir, name), root).replace(os.sep, "/")
        path = os.path.join(custom_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw_text = fh.read()
            data = json.loads(raw_text)
        except (OSError, ValueError):
            registry.add_definition_error(rel_path, "malformed_definition", "could not parse as JSON")
            continue
        if not _validate_shape(data):
            registry.add_definition_error(rel_path, "invalid_definition", "missing or wrongly typed keys")
            continue
        declared_type = data["type"]
        file_stem = name[: -len(".json")]
        if declared_type != file_stem:
            registry.add_definition_error(
                rel_path, "name_mismatch", f"file name {name!r} does not match declared type {declared_type!r}"
            )
            continue
        if declared_type in registry.types:
            registry.add_definition_error(
                rel_path, "shadows_type", f"type {declared_type!r} is already defined"
            )
            continue
        registry.types[declared_type] = TypeDef(data)
    return skipped


_RESERVED_ZONE_FOLDERS = {"documents", "wiki", "inbox", "raw"}


def _valid_module_folder(folder):
    if not isinstance(folder, str) or not folder:
        return False
    if folder in (".", "..") or folder.startswith("."):
        return False
    if os.sep in folder or "/" in folder:
        return False
    return folder not in _RESERVED_ZONE_FOLDERS


def detect_installed_modules(root):
    """Returns ({module_name: folder_name}, skipped entries) for modules with a valid module.json (C15).

    A module.json naming a reserved, hidden, parent-escaping or multi-component
    folder is ignored: that module is treated as not installed.
    """
    modules_dir = os.path.join(root, ".brain", "modules")
    installed = {}
    names, skipped = _list_folder(root, modules_dir)
    used_folders = set()
    for name in names:
        module_json = os.path.join(modules_dir, name, "module.json")
        try:
            with open(module_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            continue
        except OSError:
            # The manifest names the module's folder, so an unreadable one
            # leaves nothing to scan: report it rather than count it absent.
            skipped.append(scan.unreadable_entry(root, module_json))
            continue
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        folder = data.get("folder")
        module = data.get("module")
        if not isinstance(module, str) or not module:
            continue
        if not _valid_module_folder(folder):
            continue
        if folder in used_folders:
            continue
        used_folders.add(folder)
        installed[module] = folder
    return installed, skipped
