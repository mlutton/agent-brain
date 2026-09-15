"""Loads and validates document type definitions per specification C5 and C5a."""

import json
import os

_REQUIRED_KEYS = {
    "type": str,
    "zones": list,
    "required": list,
    "allowed_values": dict,
    "uncertainty_values": dict,
    "display_fields": list,
    "search_fields": list,
}


class TypeDef:
    def __init__(self, data):
        self.type = data["type"]
        self.zones = data["zones"]
        self.required = data["required"]
        self.allowed_values = data["allowed_values"]
        self.uncertainty_values = data["uncertainty_values"]
        self.display_fields = data["display_fields"]
        self.search_fields = data["search_fields"]

    def allows_zone(self, zone):
        return "*" in self.zones or zone in self.zones


class TypeRegistry:
    def __init__(self):
        self.types = {}
        self.definitions_errors = []

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
    for key in ("zones", "required", "display_fields", "search_fields"):
        if not all(isinstance(item, str) for item in data[key]):
            return False
    return all(isinstance(v, list) for v in data["allowed_values"].values())


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
        registry.types[data["type"]] = TypeDef(data)
    return registry


def load_custom_types(registry, root):
    """Loads custom type definitions from <root>/.brain/types/custom/*.json."""
    custom_dir = os.path.join(root, ".brain", "types", "custom")
    if not os.path.isdir(custom_dir):
        return
    for name in sorted(os.listdir(custom_dir)):
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


def detect_installed_modules(root):
    """Returns {module_name: folder_name} for modules with a module.json (C15)."""
    modules_dir = os.path.join(root, ".brain", "modules")
    installed = {}
    if not os.path.isdir(modules_dir):
        return installed
    for name in sorted(os.listdir(modules_dir)):
        module_json = os.path.join(modules_dir, name, "module.json")
        if not os.path.isfile(module_json):
            continue
        try:
            with open(module_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        folder = data.get("folder")
        module = data.get("module")
        if isinstance(folder, str) and isinstance(module, str):
            installed[module] = folder
    return installed
